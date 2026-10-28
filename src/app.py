"""Streamlit UI for the structured data extractor.

Run with: uv run streamlit run src/app.py
"""

import json
import sys
from pathlib import Path

import streamlit as st

# Streamlit runs this file from src/, so add the project folder to the import path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.extractor import OllamaNotRunningError, extract_with_retry, load_document  # noqa: E402

SAMPLES = {
    "invoice_messy.pdf": ROOT / "samples" / "invoice_messy.pdf",
    "vendor_contract.html": ROOT / "samples" / "vendor_contract.html",
}


def broken_llm(messages):
    """A fake LLM that always answers with broken data. Used to test the DLQ path."""
    return '{"invoice_number": "???", "quantity": -5, "total_amount": "lots of money", "currency": "bitcoin"'


def show_badge(invoice):
    """Green badge if line items + tax equal the total, red if not."""
    items_sum = sum(item.line_total for item in invoice.line_items)
    expected = items_sum + invoice.tax_amount
    text = f"Line items {items_sum:,.2f} + tax {invoice.tax_amount:,.2f} = {expected:,.2f} | total {invoice.total_amount:,.2f}"
    if abs(expected - invoice.total_amount) <= 0.02:
        st.success(f"MATCH: {text}")
    else:
        st.error(f"MISMATCH: {text}")


def show_audit_log(result):
    st.subheader("Audit log")
    st.write(f"Status: **{result.execution_status}** | attempts used: **{result.attempts_used}**")
    for i, response in enumerate(result.raw_responses, start=1):
        with st.expander(f"Attempt {i}", expanded=result.execution_status != "SUCCESS"):
            st.markdown("**Raw LLM output**")
            st.code(response or "(empty)", language="json")
            errors = [e for e in result.error_log if e.startswith(f"attempt {i} |")]
            st.markdown("**Validation errors**")
            st.code("\n".join(errors) if errors else "none")
            if i <= len(result.corrective_prompts):
                st.markdown("**Corrective prompt sent for the next attempt**")
                st.code(result.corrective_prompts[i - 1])
    other = [e for e in result.error_log if not e.startswith("attempt ")]
    if other:
        st.markdown("**Final steps (json_repair / DLQ)**")
        st.code("\n".join(other))


st.set_page_config(page_title="Invoice Extractor", layout="wide")
st.title("Structured Data Extractor")
st.caption("Messy PDF or HTML in, validated invoice JSON out. Runs on a local Ollama model.")

# ----- Sidebar settings -----
with st.sidebar:
    st.header("Settings")
    reflection = st.toggle("Reflection loop (retry with error feedback)", value=True)
    max_retries = st.slider("Max retries", 1, 5, 3)
    st.divider()
    simulate = st.button("Simulate Edge Case Failure", type="secondary")
    st.caption("Uses a fake broken LLM answer to show the DLQ path. Ollama is not needed.")

# ----- Input -----
source = st.radio("Document source", ["Bundled sample", "Upload a PDF", "Paste HTML or text"], horizontal=True)
raw_text = ""
if source == "Bundled sample":
    name = st.selectbox("Sample", list(SAMPLES))
    raw_text = load_document(name, SAMPLES[name].read_bytes())
elif source == "Upload a PDF":
    uploaded = st.file_uploader("PDF or HTML file", type=["pdf", "html", "htm", "txt"])
    if uploaded:
        raw_text = load_document(uploaded.name, uploaded.getvalue())
else:
    pasted = st.text_area("Paste HTML or plain text", height=200)
    if pasted:
        raw_text = load_document("pasted.html" if "<" in pasted else "pasted.txt", pasted.encode())

run = st.button("Extract", type="primary", disabled=not raw_text)

# ----- Run the pipeline -----
result = None
if simulate:
    text = raw_text or "nonsense document with no invoice"
    result = extract_with_retry(text, max_retries=3, reflection=True, llm=broken_llm, base_delay=0.2)
    raw_text = text
elif run:
    try:
        with st.spinner("Asking the local model. This can take 10 to 60 seconds..."):
            result = extract_with_retry(raw_text, max_retries=max_retries, reflection=reflection)
    except OllamaNotRunningError:
        st.error(
            "Ollama is not running, so the app cannot reach the model.\n\n"
            "1. Open the Ollama app (or run `ollama serve` in a terminal).\n"
            "2. Make sure the model is downloaded: `ollama pull llama3.2:3b`.\n"
            "3. Click Extract again."
        )

# ----- Results -----
if result:
    left, right = st.columns(2)
    with left:
        st.subheader("Raw text")
        st.text_area("Extracted text", raw_text, height=450, disabled=True)
    with right:
        st.subheader("Validated JSON")
        if result.validated_data:
            st.json(json.loads(result.validated_data.model_dump_json()))
            show_badge(result.validated_data)
        else:
            st.error("DLQ_FAILED: no valid data. The document was saved to the dead-letter queue for review.")
    show_audit_log(result)
elif raw_text:
    st.text_area("Extracted text preview", raw_text, height=250, disabled=True)
