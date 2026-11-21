import os

import pytest

from backend.agents.lex import LexAgent
from backend.agents.llm_client import GeminiLLMClient
from backend.core.config import settings
from backend.core.enums import InvoiceMatchStatus
from backend.observability import list_traces
from backend.po.matching import ThreeWayMatchInput, evaluate_three_way_match
from tests.helpers.invoice_pdf import InvoiceLineSpec, build_invoice_pdf_bytes


def _should_run_live_gemini() -> bool:
    has_key = bool(os.environ.get("GEMINI_API_KEY") or settings.gemini_api_key)
    enabled = os.environ.get("RUN_LIVE_GEMINI", "").strip().lower() in {"1", "true", "yes"}
    return has_key and enabled


@pytest.mark.skipif(
    not _should_run_live_gemini(),
    reason="Set RUN_LIVE_GEMINI=1 (with GEMINI_API_KEY) to run live Gemini PDF smoke test",
)
def test_live_gemini_pdf_invoice_extraction_and_deterministic_match(seeded_session):
    """Live multimodal smoke test hitting gemini-3.5-flash-lite with a real fpdf2 PDF invoice."""
    pdf_bytes = build_invoice_pdf_bytes(
        supplier_name="Bharat Precision Components",
        invoice_number="INV-LIVE-2026-088",
        po_number="PO-1042",
        invoice_date="2026-11-20",
        lines=[
            InvoiceLineSpec(
                sku="VLV-204",
                description="Linear Guide Rail 1200mm",
                qty=6,
                unit_price=5250.0,
            )
        ],
        gst_rate=0.18,
    )

    lex = LexAgent(llm_client=GeminiLLMClient(model_name=settings.gemini_model))
    extracted = lex.extract_invoice(
        seeded_session,
        pdf_bytes,
        filename="INV-LIVE-2026-088.pdf",
        supplier_name="Bharat Precision Components",
    )

    assert extracted.invoice_number == "INV-LIVE-2026-088"
    assert extracted.po_number == "PO-1042"
    assert len(extracted.line_items) == 1
    assert extracted.line_items[0].qty == 6
    assert abs(extracted.line_items[0].unit_price - 5250.0) <= 0.01
    assert abs(extracted.subtotal - 31500.0) <= 0.50
    assert extracted.confidence >= 0.85

    # Feed extracted numbers into 3-way match against PO price ₹4,800 -> MISMATCH_PRICE
    match_res = evaluate_three_way_match(
        ThreeWayMatchInput(
            po_qty=6,
            po_unit_price=4800.0,
            grn_received_qty=6,
            invoice_qty=extracted.line_items[0].qty,
            invoice_unit_price=extracted.line_items[0].unit_price,
            invoice_line_subtotal=extracted.subtotal,
        )
    )
    assert match_res.matched is False
    assert match_res.match_status == InvoiceMatchStatus.MISMATCH_PRICE

    traces = list_traces(seeded_session, agent_name="lex", task_type="invoice_extraction")
    assert len(traces) == 1
    assert traces[0].input_tokens > 0
    assert traces[0].output_tokens > 0
