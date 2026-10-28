"""Tests for the reflection loop, json_repair and the dead-letter queue. All LLM calls are mocked."""

import json

from src.extractor import extract_with_retry
from src.fallback import build_reflection_prompt, repair_json_text
from tests.test_extraction import GOOD_INVOICE, FakeLLM

DOC_TEXT = "INVOICE No. INV-2024-0871\nIssued on: 12th of Oct, 2024\nTOTAL DUE (USD) $1,249.50\n"


def no_wait(seconds):
    pass


def test_reflection_self_heals_on_attempt_2(tmp_path):
    missing_currency = {k: v for k, v in GOOD_INVOICE.items() if k != "currency"}
    llm = FakeLLM([json.dumps(missing_currency), json.dumps(GOOD_INVOICE)])

    result = extract_with_retry(DOC_TEXT, max_retries=3, llm=llm, dlq_path=tmp_path / "dlq.jsonl", sleep=no_wait)

    assert result.execution_status == "REPAIRED_VIA_REFLECTION"
    assert result.attempts_used == 2
    assert any("currency" in e for e in result.error_log)
    # The corrective prompt has the exact error and the bad JSON
    prompt = result.corrective_prompts[0]
    assert "currency: Field required" in prompt
    assert '"invoice_number": "INV-2024-0871"' in prompt


def test_bad_date_is_fixed_by_reflection(tmp_path):
    bad_date = {**GOOD_INVOICE, "invoice_date": "sometime in autumn"}
    llm = FakeLLM([json.dumps(bad_date), json.dumps(GOOD_INVOICE)])

    result = extract_with_retry(DOC_TEXT, llm=llm, dlq_path=tmp_path / "dlq.jsonl", sleep=no_wait)

    assert result.execution_status == "REPAIRED_VIA_REFLECTION"
    assert any(e.startswith("attempt 1 | invoice_date") for e in result.error_log)


def test_nonsense_goes_to_dlq_after_3_tries(tmp_path):
    dlq_file = tmp_path / "dlq" / "failed_extractions.jsonl"
    llm = FakeLLM(["the weather is nice today, no invoice here"])

    result = extract_with_retry("lorem ipsum 42 bananas", max_retries=3, llm=llm, dlq_path=dlq_file, sleep=no_wait)

    assert result.execution_status == "DLQ_FAILED"
    assert result.attempts_used == 3
    assert result.validated_data is None
    assert len(llm.calls) == 3
    lines = dlq_file.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["raw_text"] == "lorem ipsum 42 bananas"


def test_backoff_waits_longer_each_time(tmp_path):
    waits = []
    llm = FakeLLM(["not json"])
    extract_with_retry("text", max_retries=3, llm=llm, dlq_path=tmp_path / "d.jsonl", sleep=waits.append)
    assert waits == [1.0, 2.0]


def test_json_repair_saves_broken_json(tmp_path):
    # Missing closing brackets, wrapped in a code fence
    broken = "```json\n" + json.dumps(GOOD_INVOICE)[:-1]
    llm = FakeLLM([broken])

    result = extract_with_retry(DOC_TEXT, reflection=False, llm=llm, dlq_path=tmp_path / "dlq.jsonl", sleep=no_wait)

    assert result.execution_status == "REPAIRED_VIA_REFLECTION"
    assert result.attempts_used == 1
    assert "json_repair" in result.error_log[-1]
    assert repair_json_text("") == ""


def test_retry_prompt_does_not_resend_the_whole_document():
    long_doc = DOC_TEXT + ("Terms and conditions blah blah. " * 500)
    prompt = build_reflection_prompt(long_doc, "{}", ["currency: Field required"])
    assert len(prompt) < len(long_doc) / 3
    assert "currency: Field required" in prompt


def test_invented_invoice_is_not_accepted(tmp_path):
    # The model "finds" an invoice in a grocery list. The values are not in the text, so it must fail.
    llm = FakeLLM([json.dumps(GOOD_INVOICE)])

    result = extract_with_retry("Grocery list: milk, eggs, bread.", llm=llm, dlq_path=tmp_path / "dlq.jsonl", sleep=no_wait)

    assert result.execution_status == "DLQ_FAILED"
    assert any("was not found in the document" in e for e in result.error_log)
