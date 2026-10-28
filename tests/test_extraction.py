"""Tests for the schemas and the happy path. All LLM calls are mocked, so Ollama is not needed."""

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.extractor import extract_with_retry, load_document
from src.schemas import InvoiceExtraction, LineItem

SAMPLES = Path(__file__).parent.parent / "samples"

# What a good LLM answer for samples/invoice_messy.pdf looks like
GOOD_INVOICE = {
    "invoice_number": "INV-2024-0871",
    "vendor_name": "Cascade Office Supply Co.",
    "invoice_date": "2024-10-12",
    "line_items": [
        {"description": "Ergonomic Office Chair (mesh, black)", "quantity": 2, "unit_price": 349.75, "line_total": 699.50},
        {"description": "Standing Desk Converter - 36in", "quantity": 1, "unit_price": 289.00, "line_total": 289.00},
        {"description": "USB-C Docking Station", "quantity": 3, "unit_price": 55.00, "line_total": 165.00},
    ],
    "subtotal": 1153.50,
    "tax_amount": 96.00,
    "total_amount": 1249.50,
    "currency": "USD",
}


class FakeLLM:
    """A mock LLM that returns saved answers in order and remembers the messages it got."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, messages):
        self.calls.append(messages)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def test_golden_extraction_succeeds_first_try(tmp_path):
    text = load_document("invoice_messy.pdf", (SAMPLES / "invoice_messy.pdf").read_bytes())
    assert "INV-2024-0871" in text and "12th of Oct, 2024" in text

    llm = FakeLLM([json.dumps(GOOD_INVOICE)])
    result = extract_with_retry(text, llm=llm, dlq_path=tmp_path / "dlq.jsonl")

    assert result.execution_status == "SUCCESS"
    assert result.attempts_used == 1
    assert result.error_log == []
    assert result.validated_data.total_amount == 1249.50
    assert len(llm.calls) == 1
    assert not (tmp_path / "dlq.jsonl").exists()


def test_quantity_zero_or_negative_total_is_rejected():
    with pytest.raises(ValidationError):
        LineItem(description="Chair", quantity=0, unit_price=10.0, line_total=0.0)

    bad = {**GOOD_INVOICE, "total_amount": -1249.50}
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate(bad)


def test_line_item_sum_mismatch_fails():
    bad = {**GOOD_INVOICE, "total_amount": 1300.00}
    with pytest.raises(ValidationError) as info:
        InvoiceExtraction.model_validate(bad)
    assert "total_amount is 1300.00" in str(info.value)


def test_messy_values_are_cleaned():
    messy = {
        **GOOD_INVOICE,
        "invoice_date": "12th of Oct, 2024",
        "total_amount": "$1,249.50",
        "tax_amount": "$96.00",
        "currency": "$",
    }
    invoice = InvoiceExtraction.model_validate(messy)
    assert invoice.invoice_date == date(2024, 10, 12)
    assert invoice.total_amount == 1249.50
    assert invoice.currency == "USD"


def test_bad_money_text_is_an_error_not_zero():
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate({**GOOD_INVOICE, "total_amount": "a lot"})


def test_html_sample_is_cleaned():
    text = load_document("vendor_contract.html", (SAMPLES / "vendor_contract.html").read_bytes())
    assert "NW-CTR-2025-014" in text
    assert "<td>" not in text and "dataLayer" not in text
