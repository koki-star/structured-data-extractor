"""Read documents and extract invoice data with a validate, reflect and fallback loop."""

import io
import json
import os
import re
import time
from typing import Callable

import instructor
from instructor.core import InstructorRetryException
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import APIConnectionError, OpenAI
from pydantic import ValidationError
from pypdf import PdfReader

from src.fallback import DEFAULT_DLQ_PATH, build_reflection_prompt, repair_json_text, write_to_dlq
from src.schemas import ExtractionResult, InvoiceExtraction

load_dotenv()

# An LLM function takes chat messages and returns the raw JSON text it produced
LLMCall = Callable[[list[dict]], str]

SYSTEM_PROMPT = (
    "You extract invoice data from messy documents. "
    "Return one JSON object that matches the schema. "
    "Use YYYY-MM-DD for dates, plain numbers for money (1249.50, not $1,249.50), "
    "and USD, EUR or GBP for currency. Do not invent values that are not in the document."
)


class OllamaNotRunningError(Exception):
    """Raised when the local Ollama server cannot be reached."""


# ---------- Stage 0: text ingestion ----------

def clean_text(text: str) -> str:
    """Remove extra spaces and empty lines."""
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def read_pdf(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return clean_text("\n".join(pages))


def read_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return clean_text(soup.get_text(separator="\n"))


def load_document(filename: str, data: bytes) -> str:
    """Pick the right reader based on the file extension."""
    name = filename.lower()
    if name.endswith(".pdf"):
        return read_pdf(data)
    text = data.decode("utf-8", errors="replace")
    if name.endswith((".html", ".htm")) or "<html" in text.lower():
        return read_html(text)
    return clean_text(text)


# ---------- The real LLM (local Ollama) ----------

def make_ollama_llm(model: str | None = None, base_url: str | None = None) -> LLMCall:
    """Create an LLM function that talks to Ollama through its OpenAI-compatible API."""
    model = model or os.getenv("OLLAMA_MODEL", "llama3.2:3b")
    base_url = base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    # Ollama does not check the key, "ollama" is only a placeholder
    openai_client = OpenAI(base_url=base_url, api_key="ollama", timeout=300)
    client = instructor.from_openai(openai_client, mode=instructor.Mode.JSON)

    def call(messages: list[dict]) -> str:
        try:
            # Constrained decoding: instructor sends the JSON schema and asks for JSON only.
            # max_retries=1 means one try. Our own loop below handles the retries.
            invoice = client.chat.completions.create(
                model=model,
                response_model=InvoiceExtraction,
                messages=messages,
                max_retries=1,
                temperature=0,
            )
            return invoice.model_dump_json()
        except APIConnectionError as e:
            raise OllamaNotRunningError(f"Cannot reach Ollama at {base_url}") from e
        except InstructorRetryException as e:
            # Validation failed: give the raw text back so our gate can log the errors
            if isinstance(e.__cause__, APIConnectionError):
                raise OllamaNotRunningError(f"Cannot reach Ollama at {base_url}") from e
            completion = e.last_completion
            try:
                return completion.choices[0].message.content or ""
            except (AttributeError, IndexError):
                return str(completion or "")

    return call


# ---------- Validation gate ----------

def validate_output(text: str) -> tuple[InvoiceExtraction | None, list[str]]:
    """Check the LLM text. Return (invoice, []) or (None, list of 'field: message')."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        return None, [f"(json): not valid JSON ({e})"]
    if not isinstance(data, dict):
        return None, ["(json): expected a JSON object"]

    try:
        return InvoiceExtraction.model_validate(data), []
    except ValidationError as e:
        errors = []
        for err in e.errors():
            location = ".".join(str(part) for part in err["loc"]) or "(model)"
            errors.append(f"{location}: {err['msg']}")
        return None, errors


def check_grounding(invoice: InvoiceExtraction, raw_text: str) -> list[str]:
    """Small models sometimes invent an invoice. Check that key values really appear in the document."""
    errors = []
    text = raw_text.lower()
    if invoice.invoice_number.lower() not in text:
        errors.append(f"invoice_number: '{invoice.invoice_number}' was not found in the document")
    total = invoice.total_amount
    if f"{total:,.2f}" not in raw_text and f"{total:.2f}" not in raw_text:
        errors.append(f"total_amount: {total:,.2f} was not found in the document")
    return errors


# ---------- Main pipeline ----------

def extract_with_retry(
    raw_text: str,
    max_retries: int = 3,
    reflection: bool = True,
    llm: LLMCall | None = None,
    dlq_path=DEFAULT_DLQ_PATH,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> ExtractionResult:
    """Extract an invoice. Never crashes on bad output: returns SUCCESS, REPAIRED_VIA_REFLECTION or DLQ_FAILED.

    Note: OllamaNotRunningError is NOT caught here, so the UI can show a clear message.
    """
    llm = llm or make_ollama_llm()
    error_log: list[str] = []
    raw_responses: list[str] = []
    corrective_prompts: list[str] = []
    last_output = ""
    attempts = 0

    if not raw_text or not raw_text.strip():
        error_log.append("(input): the document has no text")
    else:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Extract the invoice from this document:\n\n{raw_text}"},
        ]
        # Without reflection we only try once
        tries = max_retries if reflection else 1

        for attempt in range(1, tries + 1):
            attempts = attempt
            # Stage 1 (first try) and Stage 2 (reflection tries)
            last_output = llm(messages)
            raw_responses.append(last_output)
            invoice, errors = validate_output(last_output)
            if invoice:
                errors = check_grounding(invoice, raw_text)
                invoice = None if errors else invoice

            if invoice:
                status = "SUCCESS" if attempt == 1 else "REPAIRED_VIA_REFLECTION"
                return ExtractionResult(
                    validated_data=invoice,
                    attempts_used=attempt,
                    error_log=error_log,
                    execution_status=status,
                    raw_responses=raw_responses,
                    corrective_prompts=corrective_prompts,
                )

            error_log.extend(f"attempt {attempt} | {e}" for e in errors)

            if attempt < tries:
                # Wait a bit longer after each failure (1s, 2s, 4s ...)
                sleep(base_delay * 2 ** (attempt - 1))
                prompt = build_reflection_prompt(raw_text, last_output, errors)
                corrective_prompts.append(prompt)
                # Short context only: system prompt + corrective prompt, not the full document
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ]

        # Stage 3: try json_repair one time on the last output
        repaired = repair_json_text(last_output)
        if repaired and repaired != last_output:
            invoice, errors = validate_output(repaired)
            if invoice:
                errors = check_grounding(invoice, raw_text)
                invoice = None if errors else invoice
            if invoice:
                error_log.append("json_repair | fixed the last output and it passed validation")
                return ExtractionResult(
                    validated_data=invoice,
                    attempts_used=attempts,
                    error_log=error_log,
                    execution_status="REPAIRED_VIA_REFLECTION",
                    raw_responses=raw_responses,
                    corrective_prompts=corrective_prompts,
                )
            error_log.extend(f"json_repair | {e}" for e in errors)

    # Everything failed: save to the dead-letter queue and return a clear failure
    saved_to = write_to_dlq(raw_text, last_output, error_log, dlq_path)
    error_log.append(f"dlq | saved to {saved_to}")
    return ExtractionResult(
        validated_data=None,
        attempts_used=attempts,
        error_log=error_log,
        execution_status="DLQ_FAILED",
        raw_responses=raw_responses,
        corrective_prompts=corrective_prompts,
    )
