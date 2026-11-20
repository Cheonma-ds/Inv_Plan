import json
from typing import TypeVar

from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select

from backend.agents.lex import (
    EmailDraftResult,
    EmailIntent,
    EmailTriageResult,
    InvoiceExtractionResult,
    InvoiceLineItem,
    LexAgent,
)
from backend.agents.llm_client import LLMResult
from backend.core.db import get_session
from backend.core.enums import (
    ActionItemStatus,
    CommStatus,
    EmailCategory,
    GRNStatus,
    InvoiceMatchStatus,
    PersonaKey,
    POStatus,
)
from backend.core.models import ActionItem, GoodsReceipt, Invoice, Part, Supplier
from backend.main import create_app
from backend.observability import list_traces
from backend.orchestrator.deps import get_workflow_engine
from backend.orchestrator.workflows import WorkflowEngine
from backend.po import create_po, transition_po
from tests.helpers.invoice_pdf import InvoiceLineSpec, build_invoice_pdf_bytes

T = TypeVar("T", bound=BaseModel)


class InvoicePipelineMockLLM:
    """Mock LLM supporting triage, draft, and multimodal PDF invoice extraction."""

    def __init__(
        self,
        *,
        triage: EmailTriageResult | None = None,
        extraction: InvoiceExtractionResult | None = None,
    ) -> None:
        self.next_triage = triage
        self.next_extraction = extraction
        self.last_pdf_bytes: bytes | None = None

    def generate_structured(
        self,
        *,
        prompt: str,
        response_schema: type[T],
        system_instruction: str | None = None,
        temperature: float = 0.0,
        pdf_bytes: bytes | None = None,
    ) -> LLMResult[T]:
        payload = json.loads(prompt)
        if response_schema is EmailTriageResult:
            assert self.next_triage is not None
            res = self.next_triage
            return LLMResult(
                parsed=res,  # type: ignore[arg-type]
                raw_text=res.model_dump_json(),
                input_tokens=320,
                output_tokens=110,
                latency_ms=20,
                cost_inr=0.004,
                model_name="mock-gemini",
            )

        if response_schema is InvoiceExtractionResult:
            assert pdf_bytes is not None and pdf_bytes.startswith(b"%PDF")
            self.last_pdf_bytes = pdf_bytes
            assert self.next_extraction is not None
            res = self.next_extraction
            return LLMResult(
                parsed=res,  # type: ignore[arg-type]
                raw_text=res.model_dump_json(),
                input_tokens=640,
                output_tokens=150,
                latency_ms=45,
                cost_inr=0.008,
                model_name="mock-gemini",
            )

        if response_schema is EmailDraftResult:
            po_num = payload["po_number"]
            cat = payload["category"]
            recipient = payload["recipient_persona"]
            ctx = payload.get("context_facts") or {}
            res = EmailDraftResult(
                to_personas=[recipient],
                cc_personas=["inventory_manager"],
                subject=f"[{po_num}] {cat.replace('_', ' ').title()}",
                body=(
                    f"Hello {payload['recipient_name']}, regarding {po_num}: "
                    f"{ctx.get('discrepancy_summary', cat)}. Please issue a corrected invoice."
                ),
            )
            return LLMResult(
                parsed=res,  # type: ignore[arg-type]
                raw_text=res.model_dump_json(),
                input_tokens=300,
                output_tokens=95,
                latency_ms=20,
                cost_inr=0.004,
                model_name="mock-gemini",
            )

        raise AssertionError(f"Unexpected schema: {response_schema}")


def _setup_delivered_po(
    seeded_session, *, sku: str = "SKU-002", qty: int = 6, price: float = 4800.0
):
    part = seeded_session.scalar(select(Part).where(Part.sku == sku))
    sup = seeded_session.scalar(
        select(Supplier).where(Supplier.persona_key == PersonaKey.SUP2.value)
    )
    assert part and sup
    po = create_po(seeded_session, part_id=part.id, supplier_id=sup.id, qty=qty, unit_price=price)
    transition_po(seeded_session, po, POStatus.SENT_AWAITING_ACK)
    transition_po(seeded_session, po, POStatus.ACKNOWLEDGED)
    seeded_session.flush()
    return part, sup, po


def _make_client(seeded_session, engine: WorkflowEngine) -> TestClient:
    app = create_app()

    def override_session():
        yield seeded_session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_workflow_engine] = lambda: engine
    return TestClient(app)


def test_lex_extract_invoice_logs_trace_and_enforces_empty_lines_guardrail(seeded_session):
    pdf_bytes = build_invoice_pdf_bytes(
        supplier_name="Bharat Precision Components",
        invoice_number="INV-TEST-01",
        po_number="PO-2001",
        invoice_date="2026-11-20",
        lines=[
            InvoiceLineSpec(
                sku="SKU-002", description="Linear Guide Rail", qty=6, unit_price=4800.0
            )
        ],
    )
    canned = InvoiceExtractionResult(
        invoice_number="INV-TEST-01",
        po_number="PO-2001",
        supplier_name="Bharat Precision Components",
        invoice_date="2026-11-20",
        line_items=[],  # missing line items should cap confidence below 0.85
        subtotal=28800.0,
        tax_amount=5184.0,
        total_amount=33984.0,
        confidence=0.95,
    )
    lex = LexAgent(llm_client=InvoicePipelineMockLLM(extraction=canned))
    res = lex.extract_invoice(seeded_session, pdf_bytes, filename="inv.pdf", linked_comm_id=42)

    assert res.confidence < 0.85
    assert "Guardrail" in (res.notes or "")
    traces = list_traces(seeded_session, agent_name="lex", task_type="invoice_extraction")
    assert len(traces) == 1
    assert traces[0].linked_entity_id == 42
    assert traces[0].input_tokens == 640


def test_scenario_1c_price_mismatch_quarantine_draft_then_corrected_v2_release(seeded_session):
    part, sup, po = _setup_delivered_po(seeded_session, sku="SKU-002", qty=6, price=4800.0)
    starting_on_hand = part.on_hand_stock
    llm = InvoicePipelineMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    client = _make_client(seeded_session, engine)

    # 1. Log GRN for 6 units
    grn_resp = client.post(f"/api/pos/{po.id}/grn", json={"received_qty": 6, "condition": "GOOD"})
    assert grn_resp.status_code == 200
    assert grn_resp.json()["po_status"] == POStatus.RECEIVED_PENDING_INVOICE.value

    # 2. Sup2 emails inflated PDF invoice v1 (₹5,400 instead of ₹4,800)
    bad_pdf = build_invoice_pdf_bytes(
        supplier_name=sup.name,
        invoice_number="INV-BP-101",
        po_number=po.po_number,
        invoice_date="2026-11-20",
        lines=[InvoiceLineSpec(sku=part.sku, description=part.name, qty=6, unit_price=5400.0)],
    )
    llm.next_triage = EmailTriageResult(
        matched_po_number=po.po_number,
        intent=EmailIntent.INVOICE_SUBMISSION,
        confidence=0.97,
        summary_for_manager=f"{sup.name} sent invoice INV-BP-101 for {po.po_number}.",
        suggested_action="Run 3-way match on attached PDF.",
        reasoning="PDF invoice attached.",
    )
    llm.next_extraction = InvoiceExtractionResult(
        invoice_number="INV-BP-101",
        po_number=po.po_number,
        supplier_name=sup.name,
        invoice_date="2026-11-20",
        line_items=[
            InvoiceLineItem(
                description=part.name,
                part_sku=part.sku,
                qty=6,
                unit_price=5400.0,
                line_total=32400.0,
            )
        ],
        subtotal=32400.0,
        tax_amount=5832.0,
        total_amount=38232.0,
        confidence=0.98,
    )

    send_resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": PersonaKey.SUP2.value,
            "to_personas": PersonaKey.LEX.value,
            "subject": f"Invoice INV-BP-101 for {po.po_number}",
            "body": f"Please find attached invoice for {po.po_number}.",
        },
        files={"attachment": ("INV-BP-101.pdf", bad_pdf, "application/pdf")},
    )
    assert send_resp.status_code == 200
    body = send_resp.json()
    triage_out = body["triage"]
    assert triage_out["match_result"]["matched"] is False
    assert triage_out["match_result"]["match_status"] == InvoiceMatchStatus.MISMATCH_PRICE.value
    assert triage_out["draft_comm_id"] is not None
    assert triage_out["alert_id"] is not None

    # Verify Quarantine Lock: stock quarantined, on_hand untouched, PO quarantined
    seeded_session.refresh(part)
    seeded_session.refresh(po)
    assert po.status == POStatus.QUARANTINED_INVOICE_MISMATCH.value
    assert part.quarantined_stock == 6
    assert part.on_hand_stock == starting_on_hand

    # Verify Lex created DRAFT_PENDING_APPROVAL INVOICE_DISCREPANCY draft & quarantine alert
    draft_id = triage_out["draft_comm_id"]
    alert = seeded_session.get(ActionItem, triage_out["alert_id"])
    assert alert is not None
    assert alert.type == "INVOICE_MISMATCH_QUARANTINE"
    assert alert.draft_comm_id == draft_id

    # 3. Manager approves & dispatches Lex's discrepancy email draft
    approve_resp = client.post(f"/api/mailroom/drafts/{draft_id}/approve", json={})
    assert approve_resp.status_code == 200
    assert approve_resp.json()["email"]["status"] == CommStatus.SENT.value
    assert approve_resp.json()["email"]["category"] == EmailCategory.INVOICE_DISCREPANCY.value

    # 4. Sup2 sends corrected PDF invoice v2 matching ₹4,800
    good_pdf = build_invoice_pdf_bytes(
        supplier_name=sup.name,
        invoice_number="INV-BP-101-R1",
        po_number=po.po_number,
        invoice_date="2026-11-21",
        lines=[InvoiceLineSpec(sku=part.sku, description=part.name, qty=6, unit_price=4800.0)],
    )
    llm.next_extraction = InvoiceExtractionResult(
        invoice_number="INV-BP-101-R1",
        po_number=po.po_number,
        supplier_name=sup.name,
        invoice_date="2026-11-21",
        line_items=[
            InvoiceLineItem(
                description=part.name,
                part_sku=part.sku,
                qty=6,
                unit_price=4800.0,
                line_total=28800.0,
            )
        ],
        subtotal=28800.0,
        tax_amount=5184.0,
        total_amount=33984.0,
        confidence=0.99,
    )
    send_v2 = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": PersonaKey.SUP2.value,
            "to_personas": PersonaKey.LEX.value,
            "subject": f"Revised Invoice INV-BP-101-R1 for {po.po_number}",
            "body": "Attached corrected invoice matching PO unit price.",
        },
        files={"attachment": ("INV-BP-101-R1.pdf", good_pdf, "application/pdf")},
    )
    assert send_v2.status_code == 200
    assert send_v2.json()["triage"]["match_result"]["matched"] is True

    # Verify stock released, quarantine cleared, PO CLOSED, quarantine alert auto-resolved
    seeded_session.refresh(part)
    seeded_session.refresh(po)
    seeded_session.refresh(alert)
    grn = seeded_session.scalar(select(GoodsReceipt).where(GoodsReceipt.po_id == po.id))
    assert grn is not None and grn.status == GRNStatus.RELEASED.value
    assert po.status == POStatus.CLOSED.value
    assert part.quarantined_stock == 0
    assert part.on_hand_stock == starting_on_hand + 6
    assert alert.status == ActionItemStatus.RESOLVED.value


def test_qty_mismatch_and_invoice_before_grn_triggers_quarantine_on_grn(seeded_session):
    part, sup, po = _setup_delivered_po(seeded_session, sku="SKU-004", qty=10, price=2200.0)
    llm = InvoicePipelineMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    client = _make_client(seeded_session, engine)

    # Supplier sends invoice BEFORE physical GRN arrives, billing 12 units instead of 10
    qty_mismatch_pdf = build_invoice_pdf_bytes(
        supplier_name=sup.name,
        invoice_number="INV-QTY-01",
        po_number=po.po_number,
        invoice_date="2026-11-20",
        lines=[InvoiceLineSpec(sku=part.sku, description=part.name, qty=12, unit_price=2200.0)],
    )
    llm.next_triage = EmailTriageResult(
        matched_po_number=None,  # Even when email body omits PO#, PDF PO# resolves it!
        intent=EmailIntent.AMBIGUOUS,
        confidence=0.60,
        summary_for_manager="Supplier attached an invoice without PO number in text.",
        suggested_action="Extract PDF to locate PO number.",
        reasoning="PO number not in email body.",
    )
    llm.next_extraction = InvoiceExtractionResult(
        invoice_number="INV-QTY-01",
        po_number=po.po_number,
        supplier_name=sup.name,
        invoice_date="2026-11-20",
        line_items=[
            InvoiceLineItem(
                description=part.name,
                part_sku=part.sku,
                qty=12,
                unit_price=2200.0,
                line_total=26400.0,
            )
        ],
        subtotal=26400.0,
        tax_amount=4752.0,
        total_amount=31152.0,
        confidence=0.96,
    )

    resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": PersonaKey.SUP2.value,
            "to_personas": PersonaKey.LEX.value,
            "subject": "Invoice attached",
            "body": "Please find our invoice attached.",
        },
        files={"attachment": ("INV-QTY-01.pdf", qty_mismatch_pdf, "application/pdf")},
    )
    assert resp.status_code == 200
    assert resp.json()["triage"]["matched_po_id"] == po.id
    assert resp.json()["triage"]["match_result"] is None  # held PENDING_GRN

    inv = seeded_session.scalar(select(Invoice).where(Invoice.po_id == po.id))
    assert inv is not None and inv.match_status == InvoiceMatchStatus.PENDING_GRN.value

    # Now physical GRN arrives for 10 units -> triggers automatic 3-way match failure & Lex draft!
    grn_resp = client.post(f"/api/pos/{po.id}/grn", json={"received_qty": 10, "condition": "GOOD"})
    assert grn_resp.status_code == 200
    grn_body = grn_resp.json()
    assert grn_body["po_status"] == POStatus.QUARANTINED_INVOICE_MISMATCH.value
    assert grn_body["match_result"]["match_status"] == InvoiceMatchStatus.MISMATCH_QTY.value
    assert grn_body["draft_comm_id"] is not None
    assert grn_body["alert_id"] is not None

    seeded_session.refresh(part)
    assert part.quarantined_stock == 10


def test_low_confidence_pdf_extraction_escalates_with_zero_po_mutation(seeded_session):
    part, sup, po = _setup_delivered_po(seeded_session, sku="SKU-007", qty=8, price=950.0)
    starting_on_hand = part.on_hand_stock
    llm = InvoicePipelineMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    client = _make_client(seeded_session, engine)

    client.post(f"/api/pos/{po.id}/grn", json={"received_qty": 8, "condition": "GOOD"})

    blurry_pdf = build_invoice_pdf_bytes(
        supplier_name=sup.name,
        invoice_number="INV-BLUR-1",
        po_number=po.po_number,
        invoice_date="2026-11-20",
        lines=[InvoiceLineSpec(sku=part.sku, description=part.name, qty=8, unit_price=950.0)],
    )
    llm.next_triage = EmailTriageResult(
        matched_po_number=po.po_number,
        intent=EmailIntent.INVOICE_SUBMISSION,
        confidence=0.95,
        summary_for_manager="Invoice attached.",
        suggested_action="Extract PDF.",
        reasoning="Attachment present.",
    )
    llm.next_extraction = InvoiceExtractionResult(
        invoice_number="INV-BLUR-1",
        po_number=po.po_number,
        supplier_name=sup.name,
        invoice_date="2026-11-20",
        line_items=[
            InvoiceLineItem(
                description=part.name, part_sku=part.sku, qty=8, unit_price=950.0, line_total=7600.0
            )
        ],
        subtotal=7600.0,
        tax_amount=1368.0,
        total_amount=8968.0,
        confidence=0.62,  # Below 0.85 threshold!
        notes="Scan smeared around unit price column.",
    )

    resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": PersonaKey.SUP2.value,
            "to_personas": PersonaKey.LEX.value,
            "subject": f"Invoice for {po.po_number}",
            "body": "Attached scan.",
        },
        files={"attachment": ("blur.pdf", blurry_pdf, "application/pdf")},
    )
    assert resp.status_code == 200
    triage_out = resp.json()["triage"]
    assert triage_out["invoice_id"] is None
    assert triage_out["match_result"] is None
    assert triage_out["alert_id"] is not None

    # Zero PO state mutation and zero stock mutation
    seeded_session.refresh(po)
    seeded_session.refresh(part)
    assert po.status == POStatus.RECEIVED_PENDING_INVOICE.value
    assert part.on_hand_stock == starting_on_hand
    assert part.quarantined_stock == 0
    alert = seeded_session.get(ActionItem, triage_out["alert_id"])
    assert alert is not None and alert.type == "INVOICE_EXTRACTION_REVIEW"


def test_clean_match_v1_pdf_releases_stock_and_closes_po_immediately(seeded_session):
    part, sup, po = _setup_delivered_po(seeded_session, sku="SKU-002", qty=4, price=4800.0)
    starting_on_hand = part.on_hand_stock
    llm = InvoicePipelineMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    client = _make_client(seeded_session, engine)

    client.post(f"/api/pos/{po.id}/grn", json={"received_qty": 4, "condition": "GOOD"})

    clean_pdf = build_invoice_pdf_bytes(
        supplier_name=sup.name,
        invoice_number="INV-CLEAN-01",
        po_number=po.po_number,
        invoice_date="2026-11-20",
        lines=[InvoiceLineSpec(sku=part.sku, description=part.name, qty=4, unit_price=4800.0)],
    )
    llm.next_triage = EmailTriageResult(
        matched_po_number=po.po_number,
        intent=EmailIntent.INVOICE_SUBMISSION,
        confidence=0.98,
        summary_for_manager=f"Clean invoice INV-CLEAN-01 attached for {po.po_number}.",
        suggested_action="Run 3-way match.",
        reasoning="Invoice PDF attached.",
    )
    llm.next_extraction = InvoiceExtractionResult(
        invoice_number="INV-CLEAN-01",
        po_number=po.po_number,
        supplier_name=sup.name,
        invoice_date="2026-11-20",
        line_items=[
            InvoiceLineItem(
                description=part.name,
                part_sku=part.sku,
                qty=4,
                unit_price=4800.0,
                line_total=19200.0,
            )
        ],
        subtotal=19200.0,
        tax_amount=3456.0,
        total_amount=22656.0,
        confidence=0.99,
    )

    resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": PersonaKey.SUP2.value,
            "to_personas": PersonaKey.LEX.value,
            "subject": f"Invoice INV-CLEAN-01 for {po.po_number}",
            "body": "Invoice attached.",
        },
        files={"attachment": ("INV-CLEAN-01.pdf", clean_pdf, "application/pdf")},
    )
    assert resp.status_code == 200
    triage_out = resp.json()["triage"]
    assert triage_out["match_result"]["matched"] is True
    assert triage_out["draft_comm_id"] is None

    seeded_session.refresh(part)
    seeded_session.refresh(po)
    assert po.status == POStatus.CLOSED.value
    assert part.quarantined_stock == 0
    assert part.on_hand_stock == starting_on_hand + 4

