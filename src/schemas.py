"""Pydantic v2 models for invoice extraction."""

import re
from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Map currency symbols and common words to ISO codes
CURRENCY_MAP = {
    "$": "USD",
    "US$": "USD",
    "DOLLAR": "USD",
    "DOLLARS": "USD",
    "€": "EUR",
    "EURO": "EUR",
    "EUROS": "EUR",
    "£": "GBP",
    "POUND": "GBP",
    "POUNDS": "GBP",
}

# Date formats we try after cleaning a messy date string
DATE_FORMATS = [
    "%d %b %Y",  # 12 Oct 2024
    "%d %B %Y",  # 12 October 2024
    "%b %d %Y",  # Oct 12 2024
    "%B %d %Y",  # October 12 2024
    "%m/%d/%Y",  # 10/12/2024 (US style)
    "%Y/%m/%d",  # 2024/10/12
]


def clean_money(value):
    """Turn money text like "$1,249.50" into a float. Raise an error if it is not a number."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().upper()
        for code in ("USD", "EUR", "GBP"):
            text = text.replace(code, "")
        text = re.sub(r"[$€£,\s]", "", text)
        try:
            return float(text)
        except ValueError:
            raise ValueError(f"'{value}' is not a valid money amount")
    return value


def parse_messy_date(value):
    """Turn dates like "12th of Oct, 2024" into a date object."""
    if isinstance(value, (date, datetime)):
        return value if isinstance(value, date) else value.date()
    if not isinstance(value, str):
        return value

    text = value.strip()
    # Try ISO-8601 first (2024-10-12)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass

    # Remove "st/nd/rd/th", the word "of", commas and extra spaces
    text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\bof\b", " ", text, flags=re.IGNORECASE)
    text = text.replace(",", " ").replace(".", " ")
    text = " ".join(text.split())

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"could not read the date '{value}', use YYYY-MM-DD")


class LineItem(BaseModel):
    description: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    unit_price: float = Field(gt=0)
    line_total: float

    @field_validator("unit_price", "line_total", mode="before")
    @classmethod
    def money_to_float(cls, value):
        return clean_money(value)


class InvoiceExtraction(BaseModel):
    invoice_number: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9\-/#]{2,29}$")
    vendor_name: str = Field(min_length=1)
    invoice_date: date
    line_items: List[LineItem] = Field(min_length=1)
    subtotal: float
    tax_amount: float = Field(ge=0)
    total_amount: float = Field(gt=0)
    currency: Literal["USD", "EUR", "GBP"]

    @field_validator("invoice_date", mode="before")
    @classmethod
    def check_date(cls, value):
        return parse_messy_date(value)

    @field_validator("subtotal", "tax_amount", "total_amount", mode="before")
    @classmethod
    def money_to_float(cls, value):
        return clean_money(value)

    @field_validator("currency", mode="before")
    @classmethod
    def currency_to_code(cls, value):
        if isinstance(value, str):
            text = value.strip().upper()
            return CURRENCY_MAP.get(text, text)
        return value

    @model_validator(mode="after")
    def check_totals(self):
        # Line items + tax must equal the total (we allow 2 cents for rounding)
        items_sum = sum(item.line_total for item in self.line_items)
        expected = round(items_sum + self.tax_amount, 2)
        if abs(expected - self.total_amount) > 0.02:
            raise ValueError(
                f"line items ({items_sum:.2f}) + tax ({self.tax_amount:.2f}) = {expected:.2f}, "
                f"but total_amount is {self.total_amount:.2f}"
            )
        return self


class ExtractionResult(BaseModel):
    validated_data: Optional[InvoiceExtraction] = None
    attempts_used: int
    error_log: List[str] = []
    execution_status: Literal["SUCCESS", "REPAIRED_VIA_REFLECTION", "DLQ_FAILED"]
    # Extra trace fields for the audit log in the UI
    raw_responses: List[str] = []
    corrective_prompts: List[str] = []

    @model_validator(mode="after")
    def no_half_results(self):
        # A success must have data, and a failure must not pretend to have data
        if self.execution_status == "DLQ_FAILED" and self.validated_data is not None:
            raise ValueError("DLQ_FAILED result cannot have validated_data")
        if self.execution_status != "DLQ_FAILED" and self.validated_data is None:
            raise ValueError("a successful result needs validated_data")
        return self
