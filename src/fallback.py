"""Fallback helpers: reflection prompt, JSON repair and the dead-letter queue (DLQ)."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from json_repair import repair_json

DEFAULT_DLQ_PATH = Path("dlq/failed_extractions.jsonl")

# Words to look for in the document when a field fails validation
FIELD_KEYWORDS = {
    "invoice_number": ["invoice", "inv", "no."],
    "vendor_name": ["from", "vendor", "ltd", "inc", "co."],
    "invoice_date": ["date", "dated"],
    "line_items": ["qty", "quantity", "x "],
    "subtotal": ["subtotal", "sub-total"],
    "tax_amount": ["tax", "vat"],
    "total_amount": ["total", "amount due", "balance due"],
    "currency": ["$", "€", "£", "usd", "eur", "gbp", "currency"],
}


def make_context_snippet(raw_text: str, errors: list[str], max_chars: int = 1500) -> str:
    """Build a short piece of the document for the retry prompt.

    We do NOT send the whole document again. We send the start of the
    document plus the lines that mention the fields that failed.
    """
    head = raw_text[: max_chars // 2]
    failed_fields = {field for field in FIELD_KEYWORDS if any(field in e for e in errors)}
    # A total mismatch error has no field name, so look at money lines too
    if any("total_amount is" in e for e in errors):
        failed_fields.update({"line_items", "tax_amount", "total_amount"})

    keywords = [k for field in failed_fields for k in FIELD_KEYWORDS[field]]
    extra_lines = []
    for line in raw_text[max_chars // 2 :].splitlines():
        if any(k in line.lower() for k in keywords):
            extra_lines.append(line.strip())

    snippet = head
    if extra_lines:
        snippet += "\n...\n" + "\n".join(extra_lines)
    return snippet[:max_chars]


def build_reflection_prompt(raw_text: str, bad_output: str, errors: list[str]) -> str:
    """Create the corrective prompt for the next attempt."""
    snippet = make_context_snippet(raw_text, errors)
    error_lines = "\n".join(f"- {e}" for e in errors)
    return (
        "Your previous JSON did not pass validation.\n\n"
        f"Validation errors (field: message):\n{error_lines}\n\n"
        f"Your previous JSON:\n{bad_output[:2000]}\n\n"
        f"Relevant part of the document:\n{snippet}\n\n"
        "Fix only the fields with errors and keep the correct ones. "
        "Dates must be YYYY-MM-DD. Money must be plain numbers like 1249.50. "
        "currency must be USD, EUR or GBP. Return the full corrected JSON only."
    )


def repair_json_text(text: str) -> str:
    """Last-chance fix for broken JSON (missing quotes, brackets, code fences).

    Returns an empty string if nothing useful can be saved.
    """
    if not text:
        return ""
    # Remove markdown code fences like ```json ... ```
    text = re.sub(r"```(?:json)?", "", text).strip()
    fixed = repair_json(text)
    if not isinstance(fixed, str) or fixed.strip() in ("", "{}", "[]", '""'):
        return ""
    return fixed


def write_to_dlq(raw_text: str, last_output: str, errors: list[str], path=DEFAULT_DLQ_PATH) -> Path:
    """Save a failed extraction as one JSON line so a person can review it later."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "raw_text": raw_text,
        "last_llm_output": last_output,
        "errors": errors,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path
