import json
from datetime import timedelta
from pathlib import Path
from typing import TypeVar

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select

from backend.agents.lex import (
    AdditionalPOUpdate,
    EmailDraftResult,
    EmailIntent,
    EmailTriageResult,
    InvoiceExtractionResult,
    InvoiceLineItem,
    LexAgent,
    PDFDocumentType,
    StockRequestIntent,
    StockRequestItem,
    StockRequestTriageResult,
)
from backend.agents.llm_client import LLMResult
from backend.core import clock
from backend.core.config import settings
from backend.core.db import get_session
from backend.core.enums import (
    AlertSeverity,
    CommStatus,
    EmailCategory,
    GRNStatus,
    InvoiceMatchStatus,
    PersonaKey,
    POStatus,
)
from backend.core.models import ActionItem, GoodsReceipt, Invoice, Part, Supplier
from backend.main import create_app
from backend.orchestrator.deps import get_workflow_engine
from backend.orchestrator.logistics_workflow import parse_iso_date_utc
from backend.orchestrator.workflows import WorkflowEngine
from backend.po import create_po, transition_po
from tests.helpers.invoice_pdf import InvoiceLineSpec, build_invoice_pdf_bytes

T = TypeVar("T", bound=BaseModel)


class FixesMockLLM:
    """Configurable mock LLM verifying payloads & driving offline F-1.1 through F-4.3 tests."""

    def __init__(self) -> None:
        self.next_triage: EmailTriageResult | None = None
        self.next_stock_triage: StockRequestTriageResult | None = None
        self.extractions_by_filename: dict[str, InvoiceExtractionResult] = {}
        self.next_extraction: InvoiceExtractionResult | None = None
        self.last_triage_pdf_bytes: bytes | None = None
        self.last_draft_payload: dict | None = None
        self.last_stock_payload: dict | None = None
        self.extract_calls: int = 0

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
            self.last_triage_pdf_bytes = pdf_bytes
            assert self.next_triage is not None
            res = self.next_triage
            return LLMResult(
                parsed=res,  # type: ignore[arg-type]
                raw_text=res.model_dump_json(),
                input_tokens=300,
                output_tokens=100,
                latency_ms=10,
                cost_inr=0.004,
                model_name="mock-gemini",
            )

        if response_schema is StockRequestTriageResult:
            self.last_stock_payload = payload
            assert self.next_stock_triage is not None
            res = self.next_stock_triage
            return LLMResult(
                parsed=res,  # type: ignore[arg-type]
                raw_text=res.model_dump_json(),
                input_tokens=280,
                output_tokens=90,
                latency_ms=10,
                cost_inr=0.004,
                model_name="mock-gemini",
            )

        if response_schema is InvoiceExtractionResult:
            self.extract_calls += 1
            fname = payload.get("filename", "")
            res = self.extractions_by_filename.get(fname)
            if res is None:
                for key, candidate in self.extractions_by_filename.items():
                    if fname.endswith(key):
                        res = candidate
                        break
            res = res or self.next_extraction
            assert res is not None
            return LLMResult(
                parsed=res,  # type: ignore[arg-type]
                raw_text=res.model_dump_json(),
                input_tokens=500,
                output_tokens=120,
                latency_ms=20,
                cost_inr=0.006,
                model_name="mock-gemini",
            )

        if response_schema is EmailDraftResult:
            self.last_draft_payload = payload
            po_num = payload.get("po_number") or ""
            cat = payload["category"]
            recipient = payload["recipient_persona"]
            prefix = f"[{po_num}] " if po_num else ""
            res = EmailDraftResult(
                to_personas=[recipient],
                cc_personas=[PersonaKey.INVENTORY_MANAGER.value],
                subject=f"{prefix}{cat.replace('_', ' ').title()}",
                body=(
                    f"Dear {payload['recipient_name']}, currency={payload.get('currency', 'INR')} "
                    f"for {payload.get('part_sku')}."
                ),
            )
            return LLMResult(
                parsed=res,  # type: ignore[arg-type]
                raw_text=res.model_dump_json(),
                input_tokens=250,
                output_tokens=80,
                latency_ms=10,
                cost_inr=0.003,
                model_name="mock-gemini",
            )

        raise AssertionError(f"Unexpected schema {response_schema}")


def _make_client(seeded_session, engine: WorkflowEngine) -> TestClient:
    app = create_app()

    def override_session():
        yield seeded_session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_workflow_engine] = lambda: engine
    return TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_attachments_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "attachments_dir", tmp_path)


def _setup_po(
    session,
    *,
    sku: str = "SNS-802",
    persona: PersonaKey = PersonaKey.SUP1,
    qty: int = 12,
    price: float = 600.0,
    status: POStatus = POStatus.ACKNOWLEDGED,
):
    part = session.scalar(select(Part).where(Part.sku == sku))
    sup = session.scalar(select(Supplier).where(Supplier.persona_key == persona.value))
    assert part is not None and sup is not None
    po = create_po(session, part_id=part.id, supplier_id=sup.id, qty=qty, unit_price=price)
    if status != POStatus.PENDING_DISPATCH_APPROVAL:
        transition_po(session, po, POStatus.SENT_AWAITING_ACK)
    if status == POStatus.ACKNOWLEDGED:
        transition_po(
            session,
            po,
            POStatus.ACKNOWLEDGED,
            promised_eta=clock.now(session) + timedelta(days=5),
        )
    session.flush()
    return part, sup, po


def test_f1_1_damaged_grn_quarantines_and_blocks_clean_invoice_release(seeded_session) -> None:
    part, sup, po = _setup_po(seeded_session, sku="SNS-802", qty=12, price=600.0)
    starting_on_hand = part.on_hand_stock
    llm = FixesMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    client = _make_client(seeded_session, engine)

    # 1. Log DAMAGED GRN -> immediately quarantined, DAMAGED_GOODS_HOLD raised
    grn_resp = client.post(
        f"/api/pos/{po.id}/grn", json={"received_qty": 12, "condition": "DAMAGED"}
    )
    assert grn_resp.status_code == 200
    assert grn_resp.json()["grn_status"] == GRNStatus.HELD_QUARANTINE.value
    seeded_session.refresh(part)
    assert part.quarantined_stock == 12
    assert part.on_hand_stock == starting_on_hand

    # 2. Clean matching PDF invoice arrives -> Invoice=MATCHED, GRN HELD_QUARANTINE, PO open!
    clean_pdf = build_invoice_pdf_bytes(
        supplier_name=sup.name,
        invoice_number="APX-2026-930",
        po_number=po.po_number,
        invoice_date="2026-11-20",
        lines=[InvoiceLineSpec(sku=part.sku, description=part.name, qty=12, unit_price=600.0)],
    )
    llm.next_triage = EmailTriageResult(
        matched_po_number=po.po_number,
        intent=EmailIntent.INVOICE_SUBMISSION,
        confidence=0.98,
        summary_for_manager=f"Invoice APX-2026-930 for {po.po_number}.",
        suggested_action="Match.",
        reasoning="Invoice PDF attached.",
    )
    llm.next_extraction = InvoiceExtractionResult(
        invoice_number="APX-2026-930",
        po_number=po.po_number,
        referenced_po_numbers=[po.po_number],
        supplier_name=sup.name,
        line_items=[
            InvoiceLineItem(
                description=part.name,
                part_sku=part.sku,
                qty=12,
                unit_price=600.0,
                line_total=7200.0,
            )
        ],
        subtotal=7200.0,
        tax_amount=0.0,
        total_amount=7200.0,
        confidence=0.99,
    )
    send_resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": sup.persona_key,
            "to_personas": PersonaKey.LEX.value,
            "subject": f"Invoice APX-2026-930 for {po.po_number}",
            "body": "Invoice attached.",
        },
        files={"attachments": ("inv_damaged.pdf", clean_pdf, "application/pdf")},
    )
    assert send_resp.status_code == 200
    triage_out = send_resp.json()["triage"]
    assert triage_out["match_result"]["matched"] is True
    assert triage_out["match_result"]["damaged_hold"] is True

    seeded_session.refresh(part)
    seeded_session.refresh(po)
    grn = seeded_session.scalar(select(GoodsReceipt).where(GoodsReceipt.po_id == po.id))
    inv = seeded_session.scalar(select(Invoice).where(Invoice.po_id == po.id))
    assert inv is not None and inv.match_status == InvoiceMatchStatus.MATCHED.value
    assert grn is not None and grn.status == GRNStatus.HELD_QUARANTINE.value
    assert po.status == POStatus.QUARANTINED_INVOICE_MISMATCH.value
    assert part.on_hand_stock == starting_on_hand
    assert part.quarantined_stock == 12  # No double-counting!

    hold_alert = seeded_session.get(ActionItem, triage_out["alert_id"])
    assert hold_alert is not None and hold_alert.type == "DAMAGED_GOODS_HOLD"
    actions = {a["action"] for a in hold_alert.suggested_actions_json}
    assert {"release_damaged_goods", "draft_damage_claim", "escalate_procurement"}.issubset(actions)

    # 3. Draft damage claim CTA -> generates draft CC'ing inventory_manager + procurement
    claim_resp = client.post(
        f"/api/alerts/{hold_alert.id}/execute-cta",
        json={"action": "draft_damage_claim", "params": {"note": "4 sensors cracked"}},
    )
    assert claim_resp.status_code == 200
    claim_draft_id = claim_resp.json()["draft_comm_id"]
    drafts = client.get("/api/mailroom/lex/drafts").json()
    claim_draft = next(d for d in drafts if d["id"] == claim_draft_id)
    assert set(claim_draft["cc_personas"]) >= {"inventory_manager", "procurement"}

    # 4. Manager releases 8 usable units and scraps 4 damaged units
    rel_resp = client.post(
        f"/api/alerts/{hold_alert.id}/execute-cta",
        json={"action": "release_damaged_goods", "params": {"good_qty": 8}},
    )
    assert rel_resp.status_code == 200
    seeded_session.refresh(part)
    assert part.on_hand_stock == starting_on_hand + 8
    assert part.quarantined_stock == 0


def test_f1_2_and_f3_3_locked_corrupt_and_unsupported_attachments(seeded_session) -> None:
    part, sup, po = _setup_po(
        seeded_session, sku="BLT-301", persona=PersonaKey.SUP2, qty=10, price=100.0
    )
    llm = FixesMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    client = _make_client(seeded_session, engine)

    # 1. .xlsx, .zip, and 0-byte .pdf return clean 400
    for fname, data, mime in [
        ("prices.xlsx", b"PK\x03\x04dummy", "application/octet-stream"),
        ("bundle.zip", b"PK\x03\x04dummy", "application/zip"),
        ("empty.pdf", b"", "application/pdf"),
    ]:
        bad_resp = client.post(
            "/api/mailroom/send",
            data={
                "from_persona": PersonaKey.SUP2.value,
                "to_personas": PersonaKey.INVENTORY_MANAGER.value,
                "cc_personas": PersonaKey.LEX.value,
                "subject": f"Bad file {fname}",
                "body": "See attached.",
            },
            files={"attachments": (fname, data, mime)},
        )
        assert bad_resp.status_code == 400

    # 2. Password-protected /Encrypt PDF -> 200, stores email, skips Gemini extract, HIGH alert
    locked_pdf = Path("docs/test-scenarios/fixtures/locked_invoice.pdf").read_bytes()
    llm.next_triage = EmailTriageResult(
        matched_po_number=po.po_number,
        intent=EmailIntent.INVOICE_SUBMISSION,
        confidence=0.95,
        summary_for_manager=f"Locked PDF invoice for {po.po_number}.",
        suggested_action="Extract.",
        reasoning="PDF attached.",
    )
    locked_resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": PersonaKey.SUP2.value,
            "to_personas": PersonaKey.INVENTORY_MANAGER.value,
            "cc_personas": PersonaKey.LEX.value,
            "subject": f"Locked invoice for {po.po_number}",
            "body": "Invoice attached.",
        },
        files={"attachments": ("locked_invoice.pdf", locked_pdf, "application/pdf")},
    )
    assert locked_resp.status_code == 200
    assert llm.extract_calls == 0
    alert_id = locked_resp.json()["triage"]["alert_id"]
    alert = seeded_session.get(ActionItem, alert_id)
    assert alert is not None and alert.type == "INVOICE_EXTRACTION_REVIEW"
    assert alert.severity == AlertSeverity.HIGH.value
    assert "password-protected or unreadable" in alert.summary


def test_f1_3_f1_4_expedite_cc_procurement_and_inr_currency_context(seeded_session) -> None:
    part, sup, po = _setup_po(
        seeded_session, sku="SNS-802", persona=PersonaKey.SUP3, qty=5, price=600.0
    )
    part.criticality = "CRITICAL"
    seeded_session.flush()

    llm = FixesMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    llm.next_triage = EmailTriageResult(
        matched_po_number=po.po_number,
        intent=EmailIntent.POST_ACK_DELAY,
        confidence=0.96,
        proposed_eta_date="2026-12-10",
        delay_reason="Machine breakdown",
        summary_for_manager=f"{po.po_number} delayed to 2026-12-10.",
        suggested_action="Expedite.",
        reasoning="Post-ack delay on CRITICAL part.",
    )
    client = _make_client(seeded_session, engine)
    resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": PersonaKey.SUP3.value,
            "to_personas": PersonaKey.INVENTORY_MANAGER.value,
            "cc_personas": PersonaKey.LEX.value,
            "subject": f"Delay {po.po_number}",
            "body": "Machine breakdown, ETA Dec 10.",
        },
    )
    assert resp.status_code == 200
    draft_id = resp.json()["triage"]["draft_comm_id"]
    assert draft_id is not None
    drafts = client.get("/api/mailroom/lex/drafts").json()
    draft = next(d for d in drafts if d["id"] == draft_id)
    assert set(draft["cc_personas"]) == {"inventory_manager", "procurement"}
    assert llm.last_draft_payload is not None
    assert llm.last_draft_payload["currency"] == "INR"
    assert llm.last_draft_payload["context_facts"]["currency"] == "INR"


def test_f2_1_foreign_currency_guardrail_blocks_overwrite_and_drafts_inr_request(
    seeded_session,
) -> None:
    part, sup, po = _setup_po(
        seeded_session,
        sku="RLY-901",
        persona=PersonaKey.SUP3,
        qty=10,
        price=100.0,
        status=POStatus.SENT_AWAITING_ACK,
    )
    llm = FixesMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    client = _make_client(seeded_session, engine)

    quote_pdf = Path("docs/test-scenarios/fixtures/quote_sup3_usd_po1040.pdf").read_bytes()
    llm.next_triage = EmailTriageResult(
        matched_po_number=po.po_number,
        intent=EmailIntent.PRICE_CHANGE,
        confidence=0.95,
        proposed_unit_price=18.5,
        proposed_currency="USD",
        proposed_eta_date="2026-12-04",
        summary_for_manager=f"Quoted 18.50 USD for {po.po_number}.",
        suggested_action="Request INR quote.",
        reasoning="USD quote attached.",
    )
    resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": PersonaKey.SUP3.value,
            "to_personas": PersonaKey.INVENTORY_MANAGER.value,
            "cc_personas": PersonaKey.LEX.value,
            "subject": f"Quote for {po.po_number}",
            "body": "See quote at 18.50 USD.",
        },
        files={"attachments": ("quote_usd.pdf", quote_pdf, "application/pdf")},
    )
    assert resp.status_code == 200
    # F-3.2: non-invoice PDF bytes were passed directly into email_triage!
    assert llm.last_triage_pdf_bytes == quote_pdf

    seeded_session.refresh(po)
    assert po.unit_price == 100.0  # Never overwritten with 18.50!
    assert po.status == POStatus.EXCEPTION_REVIEW.value

    alert_id = resp.json()["triage"]["alert_id"]
    alert = seeded_session.get(ActionItem, alert_id)
    assert alert is not None and alert.type == "CURRENCY_MISMATCH"
    assert "USD" in alert.summary

    # Blind update_po CTA must be rejected
    bad_update = client.post(
        f"/api/alerts/{alert_id}/execute-cta",
        json={"action": "update_po", "params": {}},
    )
    assert bad_update.status_code == 400

    # request_inr_quote CTA generates DRAFT_PENDING_APPROVAL reply
    inr_req = client.post(
        f"/api/alerts/{alert_id}/execute-cta",
        json={"action": "request_inr_quote", "params": {}},
    )
    assert inr_req.status_code == 200
    assert inr_req.json()["draft_comm_id"] is not None


def test_f2_2_post_ack_commercial_reversal_enters_exception_review_and_drafts_revised_po(
    seeded_session,
) -> None:
    part, sup, po = _setup_po(
        seeded_session,
        sku="VLV-204",
        persona=PersonaKey.SUP2,
        qty=9,
        price=1500.0,
        status=POStatus.ACKNOWLEDGED,
    )
    ack_ts = po.acknowledged_at
    promised_eta = po.promised_eta
    llm = FixesMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    client = _make_client(seeded_session, engine)

    llm.next_triage = EmailTriageResult(
        matched_po_number=po.po_number,
        intent=EmailIntent.PRICE_CHANGE,
        confidence=0.97,
        proposed_unit_price=1780.0,
        proposed_currency="INR",
        proposed_eta_date="2026-12-03",
        summary_for_manager=f"Post-ack price hike to ₹1,780 on {po.po_number}.",
        suggested_action="Review price change.",
        reasoning="Post-ack reversal.",
    )
    resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": PersonaKey.SUP2.value,
            "to_personas": PersonaKey.INVENTORY_MANAGER.value,
            "cc_personas": PersonaKey.LEX.value,
            "subject": f"Re: {po.po_number}",
            "body": "Finance revised price to ₹1,780/unit.",
        },
    )
    assert resp.status_code == 200
    seeded_session.refresh(po)
    assert po.status == POStatus.EXCEPTION_REVIEW.value
    assert clock.ensure_utc(po.acknowledged_at) == clock.ensure_utc(ack_ts)
    assert clock.ensure_utc(po.promised_eta) == clock.ensure_utc(promised_eta)

    alert_id = resp.json()["triage"]["alert_id"]
    upd_resp = client.post(
        f"/api/alerts/{alert_id}/execute-cta",
        json={"action": "update_po", "params": {}},
    )
    assert upd_resp.status_code == 200
    upd_data = upd_resp.json()
    assert upd_data["po_status"] == POStatus.PENDING_DISPATCH_APPROVAL.value
    assert upd_data["draft_comm_id"] is not None

    drafts = client.get("/api/mailroom/lex/drafts").json()
    rev_draft = next(d for d in drafts if d["id"] == upd_data["draft_comm_id"])
    assert rev_draft["category"] == EmailCategory.REVISED_PO.value


def test_f2_3_multi_po_email_splitting_and_consolidated_multi_po_pdf_escalation(
    seeded_session,
) -> None:
    part1, sup2, po_crit = _setup_po(
        seeded_session,
        sku="VLV-204",
        persona=PersonaKey.SUP2,
        qty=5,
        price=1500.0,
        status=POStatus.ACKNOWLEDGED,
    )
    part1.criticality = "CRITICAL"
    part2, _, po_low = _setup_po(
        seeded_session,
        sku="WSH-104",
        persona=PersonaKey.SUP2,
        qty=25,
        price=45.0,
        status=POStatus.ACKNOWLEDGED,
    )
    part2.criticality = "LOW"
    part2.on_hand_stock = 120
    seeded_session.flush()

    llm = FixesMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    client = _make_client(seeded_session, engine)

    llm.next_triage = EmailTriageResult(
        matched_po_number=po_crit.po_number,
        intent=EmailIntent.POST_ACK_DELAY,
        confidence=0.95,
        proposed_eta_date="2026-12-05",
        delay_reason="Furnace maintenance",
        summary_for_manager=f"{po_crit.po_number} delayed to 2026-12-05.",
        suggested_action="Expedite critical PO.",
        reasoning="Two POs cited explicitly.",
        additional_po_updates=[
            AdditionalPOUpdate(
                matched_po_number=po_low.po_number,
                intent=EmailIntent.POST_ACK_DELAY,
                confidence=0.95,
                proposed_eta_date="2026-12-05",
                delay_reason="Furnace maintenance",
                summary_for_manager=f"{po_low.po_number} delayed to 2026-12-05.",
                suggested_action="Accept minor delay.",
            )
        ],
    )
    resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": PersonaKey.SUP2.value,
            "to_personas": PersonaKey.INVENTORY_MANAGER.value,
            "cc_personas": PersonaKey.LEX.value,
            "subject": f"Delay on {po_crit.po_number} and {po_low.po_number}",
            "body": f"Both {po_crit.po_number} and {po_low.po_number} delayed to Dec 5.",
        },
    )
    assert resp.status_code == 200
    triage_out = resp.json()["triage"]
    assert len(triage_out["alert_ids"]) == 2
    seeded_session.refresh(po_crit)
    seeded_session.refresh(po_low)
    assert po_crit.status == POStatus.DELAYED_CRITICAL.value
    assert po_low.status == POStatus.ACKNOWLEDGED.value
    assert po_low.revised_eta is not None

    # Consolidated multi-PO PDF escalates cleanly listing all POs
    cons_pdf = Path("docs/test-scenarios/fixtures/inv_sup1_consolidated_2pos.pdf").read_bytes()
    llm.next_triage = EmailTriageResult(
        matched_po_number=None,
        intent=EmailIntent.INVOICE_SUBMISSION,
        confidence=0.95,
        summary_for_manager="Consolidated invoice for 2 POs.",
        suggested_action="Review.",
        reasoning="Consolidated invoice.",
    )
    llm.next_extraction = InvoiceExtractionResult(
        invoice_number="APX-2026-910",
        po_number=None,
        referenced_po_numbers=[po_crit.po_number, po_low.po_number],
        supplier_name="Apex",
        line_items=[
            InvoiceLineItem(description="A", qty=5, unit_price=1500.0, line_total=7500.0),
            InvoiceLineItem(description="B", qty=25, unit_price=45.0, line_total=1125.0),
        ],
        subtotal=8625.0,
        total_amount=8625.0,
        confidence=0.97,
    )
    cons_resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": PersonaKey.SUP1.value,
            "to_personas": PersonaKey.LEX.value,
            "subject": "Consolidated invoice",
            "body": "Attached combined invoice.",
        },
        files={"attachments": ("consolidated.pdf", cons_pdf, "application/pdf")},
    )
    assert cons_resp.status_code == 200
    cons_alert = seeded_session.get(ActionItem, cons_resp.json()["triage"]["alert_id"])
    assert cons_alert is not None and cons_alert.type == "INVOICE_EXTRACTION_REVIEW"
    assert cons_alert.cta_payload_json["referenced_po_numbers"] == [
        po_crit.po_number,
        po_low.po_number,
    ]


def test_f3_1_multi_attachment_credit_note_and_packing_list_crosscheck(seeded_session) -> None:
    part, sup, po = _setup_po(
        seeded_session, sku="VLV-204", persona=PersonaKey.SUP2, qty=5, price=1500.0
    )
    starting_on_hand = part.on_hand_stock
    llm = FixesMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    client = _make_client(seeded_session, engine)

    client.post(f"/api/pos/{po.id}/grn", json={"received_qty": 5, "condition": "GOOD"})

    inv_pdf = Path("docs/test-scenarios/fixtures/inv_sup2_overbilled_v1.pdf").read_bytes()
    cn_pdf = Path("docs/test-scenarios/fixtures/cn_sup2_credit_note.pdf").read_bytes()
    llm.next_triage = EmailTriageResult(
        matched_po_number=po.po_number,
        intent=EmailIntent.INVOICE_SUBMISSION,
        confidence=0.97,
        summary_for_manager="Invoice + Credit Note attached.",
        suggested_action="Review net settlement.",
        reasoning="Invoice + CN.",
    )
    llm.extractions_by_filename = {
        "inv.pdf": InvoiceExtractionResult(
            document_type=PDFDocumentType.INVOICE,
            invoice_number="BF-4801",
            po_number=po.po_number,
            referenced_po_numbers=[po.po_number],
            line_items=[
                InvoiceLineItem(
                    description="VLV-204",
                    part_sku="VLV-204",
                    qty=5,
                    unit_price=1800.0,
                    line_total=9000.0,
                )
            ],
            subtotal=9000.0,
            total_amount=9000.0,
            confidence=0.98,
        ),
        "cn.pdf": InvoiceExtractionResult(
            document_type=PDFDocumentType.CREDIT_NOTE,
            invoice_number="CN-BF-109",
            po_number=po.po_number,
            referenced_po_numbers=[po.po_number],
            line_items=[],
            subtotal=-1500.0,
            total_amount=-1500.0,
            confidence=0.96,
        ),
    }
    resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": PersonaKey.SUP2.value,
            "to_personas": PersonaKey.INVENTORY_MANAGER.value,
            "cc_personas": PersonaKey.LEX.value,
            "subject": f"BF-4801 + CN-BF-109 for {po.po_number}",
            "body": "Overbilled invoice + credit note attached.",
        },
        files=[
            ("attachments", ("inv.pdf", inv_pdf, "application/pdf")),
            ("attachments", ("cn.pdf", cn_pdf, "application/pdf")),
        ],
    )
    assert resp.status_code == 200
    email_out = resp.json()["email"]
    assert len(email_out["attachment_paths"]) == 2
    assert email_out["attachment_path"] == email_out["attachment_paths"][0]

    cn_alert_id = resp.json()["triage"]["alert_id"]
    cn_alert = seeded_session.get(ActionItem, cn_alert_id)
    assert cn_alert is not None and cn_alert.type == "INVOICE_WITH_CREDIT_NOTE_REVIEW"
    assert cn_alert.cta_payload_json["net_amount"] == 7500.0

    # Approve Net Settlement & Release Stock
    settle_resp = client.post(
        f"/api/alerts/{cn_alert_id}/execute-cta",
        json={"action": "approve_net_settlement", "params": {}},
    )
    assert settle_resp.status_code == 200
    seeded_session.refresh(part)
    seeded_session.refresh(po)
    assert po.status == POStatus.CLOSED.value
    assert part.on_hand_stock == starting_on_hand + 5

    # Packing List vs Invoice cross-check (SCN-44)
    part2, sup2, po2 = _setup_po(
        seeded_session, sku="BLT-301", persona=PersonaKey.SUP2, qty=100, price=100.0
    )
    client.post(f"/api/pos/{po2.id}/grn", json={"received_qty": 90, "condition": "GOOD"})
    pl_pdf = Path("docs/test-scenarios/fixtures/packing_list_90pcs.pdf").read_bytes()
    inv100_pdf = Path("docs/test-scenarios/fixtures/inv_sup2_100pcs.pdf").read_bytes()
    llm.next_triage = EmailTriageResult(
        matched_po_number=po2.po_number,
        intent=EmailIntent.INVOICE_SUBMISSION,
        confidence=0.97,
        summary_for_manager="Packing list + invoice.",
        suggested_action="Match.",
        reasoning="2 PDFs.",
    )
    llm.extractions_by_filename = {
        "pl.pdf": InvoiceExtractionResult(
            document_type=PDFDocumentType.PACKING_LIST,
            invoice_number="PL-880",
            po_number=po2.po_number,
            referenced_po_numbers=[po2.po_number],
            line_items=[
                InvoiceLineItem(
                    description="BLT-301",
                    part_sku="BLT-301",
                    qty=90,
                    unit_price=0.0,
                    line_total=0.0,
                )
            ],
            subtotal=0.0,
            total_amount=0.0,
            confidence=0.97,
        ),
        "inv100.pdf": InvoiceExtractionResult(
            document_type=PDFDocumentType.INVOICE,
            invoice_number="BF-5012",
            po_number=po2.po_number,
            referenced_po_numbers=[po2.po_number],
            line_items=[
                InvoiceLineItem(
                    description="BLT-301",
                    part_sku="BLT-301",
                    qty=100,
                    unit_price=100.0,
                    line_total=10000.0,
                )
            ],
            subtotal=10000.0,
            total_amount=10000.0,
            confidence=0.98,
        ),
    }
    pl_resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": PersonaKey.SUP2.value,
            "to_personas": PersonaKey.LEX.value,
            "subject": f"Docs for {po2.po_number}",
            "body": "Packing list and invoice attached.",
        },
        files=[
            ("attachments", ("pl.pdf", pl_pdf, "application/pdf")),
            ("attachments", ("inv100.pdf", inv100_pdf, "application/pdf")),
        ],
    )
    assert pl_resp.status_code == 200
    pl_alert = seeded_session.get(ActionItem, pl_resp.json()["triage"]["alert_id"])
    assert pl_alert is not None
    assert pl_alert.cta_payload_json["packing_list_conflict"] is True
    assert pl_alert.cta_payload_json["packing_list_qty"] == 90
    assert "Packing List" in pl_alert.summary


def test_f3_3_making_csv_bom_and_damage_jpg_attachment(seeded_session) -> None:
    part, sup, po = _setup_po(
        seeded_session, sku="VLV-204", persona=PersonaKey.SUP2, qty=10, price=300.0
    )
    llm = FixesMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    client = _make_client(seeded_session, engine)

    # 1. Making CSV BOM
    csv_bytes = Path("docs/test-scenarios/fixtures/making_bom_line3.csv").read_bytes()
    llm.next_stock_triage = StockRequestTriageResult(
        intent=StockRequestIntent.STOCK_REQUEST,
        confidence=0.96,
        requested_items=[
            StockRequestItem(part_sku="BRG-402", requested_qty=4),
            StockRequestItem(part_sku="GSK-512", requested_qty=6),
        ],
        summary_for_manager="BOM CSV parsed for BRG-402 (4) and GSK-512 (6).",
        suggested_action="Review stock.",
        reasoning="Parsed CSV.",
    )
    csv_resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": PersonaKey.MAKING.value,
            "to_personas": PersonaKey.INVENTORY_MANAGER.value,
            "cc_personas": PersonaKey.LEX.value,
            "subject": "Line 3 BOM",
            "body": "Please issue parts in attached CSV.",
        },
        files={"attachments": ("making_bom_line3.csv", csv_bytes, "text/csv")},
    )
    assert csv_resp.status_code == 200
    assert len(csv_resp.json()["triage"]["stock_request_ids"]) == 2
    assert llm.last_stock_payload is not None
    assert "BRG-402" in (llm.last_stock_payload.get("attachment_csv_text") or "")

    # 2. Damage .jpg photo attachment
    jpg_bytes = Path("docs/test-scenarios/fixtures/damaged_shipment_photo.jpg").read_bytes()
    llm.next_triage = EmailTriageResult(
        matched_po_number=po.po_number,
        intent=EmailIntent.GENERAL_REPLY,
        confidence=0.92,
        summary_for_manager=f"4 pcs damaged in transit on {po.po_number}.",
        suggested_action="Inspect photo.",
        reasoning="Photo attached.",
    )
    jpg_resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": PersonaKey.SUP2.value,
            "to_personas": PersonaKey.INVENTORY_MANAGER.value,
            "cc_personas": PersonaKey.LEX.value,
            "subject": f"Transit damage photo for {po.po_number}",
            "body": "4 pcs damaged, photo attached.",
        },
        files={"attachments": ("damaged_shipment_photo.jpg", jpg_bytes, "image/jpeg")},
    )
    assert jpg_resp.status_code == 200
    jpg_alert = seeded_session.get(ActionItem, jpg_resp.json()["triage"]["alert_id"])
    assert jpg_alert is not None and jpg_alert.severity == AlertSeverity.HIGH.value
    assert "damaged_shipment_photo.jpg" in jpg_alert.summary


def test_f4_1_and_f4_3_ambiguous_clarification_draft_and_eta_0900_normalization(
    seeded_session,
) -> None:
    part, sup, po = _setup_po(
        seeded_session,
        sku="RLY-901",
        persona=PersonaKey.SUP3,
        qty=10,
        price=100.0,
        status=POStatus.SENT_AWAITING_ACK,
    )
    llm = FixesMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    client = _make_client(seeded_session, engine)

    # F-4.1: AMBIGUOUS_EMAIL produces optional DRAFT_PENDING_APPROVAL clarification draft
    llm.next_triage = EmailTriageResult(
        matched_po_number=po.po_number,
        intent=EmailIntent.AMBIGUOUS,
        confidence=0.65,
        summary_for_manager="Hedged supplier reply without firm price or ETA.",
        suggested_action="Request clarification.",
        reasoning="Confidence < 0.85.",
    )
    amb_resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": PersonaKey.SUP3.value,
            "to_personas": PersonaKey.INVENTORY_MANAGER.value,
            "cc_personas": PersonaKey.LEX.value,
            "subject": f"Re: {po.po_number}",
            "body": "Checking rate and transport, will try early Dec.",
        },
    )
    assert amb_resp.status_code == 200
    draft_id = amb_resp.json()["triage"]["draft_comm_id"]
    assert draft_id is not None
    alert = seeded_session.get(ActionItem, amb_resp.json()["triage"]["alert_id"])
    assert alert is not None and alert.draft_comm_id == draft_id

    rej = client.post(f"/api/mailroom/drafts/{draft_id}/reject")
    assert rej.status_code == 200
    assert rej.json()["email"]["status"] == CommStatus.REJECTED.value

    # F-4.3: Date-only ETA normalizes to 09:00:00Z matching SEED_START_TIME
    eta_dt = parse_iso_date_utc("2026-11-22")
    assert eta_dt is not None
    assert eta_dt.hour == 9 and eta_dt.minute == 0
    assert eta_dt == clock.now(seeded_session) + timedelta(days=2)


def test_backup_failover_po_dispatch_prompt_and_facts(seeded_session) -> None:
    from backend.agents.lex.prompts import DRAFT_SYSTEM_PROMPT

    assert "NEVER say you are checking dispatch status" in DRAFT_SYSTEM_PROMPT

    _, _, po = _setup_po(
        seeded_session,
        sku="BRG-402",
        persona=PersonaKey.SUP1,
        qty=4,
        price=450.0,
        status=POStatus.SENT_AWAITING_ACK,
    )
    llm = FixesMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    out = engine.switch_to_backup_supplier(seeded_session, po.id)
    assert out["new_po"].status == POStatus.PENDING_DISPATCH_APPROVAL.value
    from backend.core.models import AgentTrace

    trace = seeded_session.scalar(
        select(AgentTrace)
        .where(AgentTrace.task_type == "email_draft")
        .order_by(AgentTrace.id.desc())
        .limit(1)
    )
    assert trace is not None and trace.raw_prompt is not None
    assert '"backup_failover": true' in trace.raw_prompt
    assert '"total_value": 1920.0' in trace.raw_prompt


def test_manual_po_draft_and_create_flow(seeded_session) -> None:
    llm = FixesMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    client = _make_client(seeded_session, engine)

    parts_resp = client.get("/api/parts")
    assert parts_resp.status_code == 200
    brg = next(p for p in parts_resp.json() if p["sku"] == "BRG-402")
    sup1 = next(s for s in brg["suppliers"] if s["supplier_persona"] == PersonaKey.SUP1.value)
    assert sup1["reliability_score"] is not None
    assert sup1["estimated_arrival_date"] is not None
    assert sup1["otd_rate"] is not None

    draft_resp = client.post(
        "/api/pos/draft-manual",
        json={"part_id": brg["part_id"], "supplier_id": sup1["supplier_id"], "qty": 6},
    )
    assert draft_resp.status_code == 200
    draft_data = draft_resp.json()
    assert draft_data["total_amount"] == 6 * sup1["agreed_unit_price"]
    assert set(draft_data["cc_personas"]) == {
        PersonaKey.LEX.value,
        PersonaKey.INVENTORY_MANAGER.value,
    }

    create_resp = client.post(
        "/api/pos/manual",
        json={
            "part_id": brg["part_id"],
            "supplier_id": sup1["supplier_id"],
            "qty": 6,
            "subject": draft_data["subject"],
            "body": draft_data["body"] + "\nEdited by manager.",
        },
    )
    assert create_resp.status_code == 200
    po_data = create_resp.json()["po"]
    assert po_data["status"] == POStatus.SENT_AWAITING_ACK.value
    assert po_data["qty"] == 6

    inbox_sup1 = client.get("/api/mailroom/sup1/inbox").json()
    assert any(m["id"] == create_resp.json()["comm_id"] for m in inbox_sup1)
    sent_mgr = client.get("/api/mailroom/inventory_manager/sent").json()
    sent_msg = next(m for m in sent_mgr if m["id"] == create_resp.json()["comm_id"])
    assert set(sent_msg["cc_personas"]) == {
        PersonaKey.LEX.value,
        PersonaKey.INVENTORY_MANAGER.value,
    }
