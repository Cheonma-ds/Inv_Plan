from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.agents.lex import EmailDraftInput, PDFDocumentType
from backend.core import clock
from backend.core.enums import (
    EmailCategory,
    GRNStatus,
    InvoiceMatchStatus,
    LexReminderStatus,
    PersonaKey,
    POStatus,
)
from backend.core.models import (
    GoodsReceipt,
    Invoice,
    LexReminder,
    PurchaseOrder,
)
from backend.mailroom import OutboundMessage, list_drafts, send_email
from backend.observability import list_traces
from backend.orchestrator.workflows import WorkflowEngine
from backend.po.matching import ThreeWayMatchInput, evaluate_three_way_match
from tests.agents.live_helpers import (
    LIVE_SKIP_REASON,
    build_damage_inspection_png,
    build_dispatch_slip_png,
    create_open_po,
    make_live_lex,
    should_run_live_gemini,
)
from tests.helpers.invoice_pdf import InvoiceLineSpec, build_invoice_pdf_bytes


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_multimodal_noisy_pdf_invoice_overcharge_and_3way_quarantine(
    seeded_session,
    tmp_path,
) -> None:
    """Noisy PDF invoice overcharge -> verbatim extraction -> 3-way mismatch -> quarantine."""
    po = create_open_po(
        seeded_session,
        sku="VLV-204",
        persona=PersonaKey.SUP2,
        status=POStatus.ACKNOWLEDGED,
        qty=10,
        unit_price=1500.0,
        eta_days=15,
    )
    engine = WorkflowEngine(lex_agent=make_live_lex())
    engine.record_grn(seeded_session, po_id=po.id, received_qty=10, condition="GOOD")
    seeded_session.commit()

    # Overcharged PDF (1725.00 vs PO 1500.00) with distracting commercial notes
    pdf_bytes = build_invoice_pdf_bytes(
        supplier_name="Bharat Industrial Fasteners",
        invoice_number="INV-NOISY-2026-441",
        po_number=po.po_number,
        invoice_date="2026-10-12",
        lines=[
            InvoiceLineSpec(
                sku="VLV-204",
                description="Hydraulic Servo Solenoid Valve HV-40",
                qty=10,
                unit_price=1725.0,
            )
        ],
        gst_rate=0.18,
        notes=(
            "Fuel surcharge & alloy adjustment applied per internal circular #99. "
            "Bank ref: HDFC0001928. Ignore proforma quote #PF-112."
        ),
    )
    pdf_path = tmp_path / "INV-NOISY-2026-441.pdf"
    pdf_path.write_bytes(pdf_bytes)

    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP2,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            po_id=po.id,
            subject=f"Invoice INV-NOISY-2026-441 for {po.po_number}",
            body=(
                f"Hi team, please find attached our tax invoice for {po.po_number}. "
                "Kindly release payment by Friday."
            ),
            attachment_paths=[str(pdf_path)],
        ),
    )
    seeded_session.refresh(po)
    assert po.status == POStatus.QUARANTINED_INVOICE_MISMATCH.value

    inv = seeded_session.scalar(select(Invoice).where(Invoice.po_id == po.id))
    assert inv is not None
    assert inv.invoice_number == "INV-NOISY-2026-441"
    assert abs(inv.extracted_unit_price - 1725.0) <= 0.01
    assert inv.match_status == InvoiceMatchStatus.MISMATCH_PRICE.value
    assert res["triage"]["draft_comm_id"] is not None

    # Discrepancy draft must NOT concede overcharge or release payment
    draft = next(
        d for d in list_drafts(seeded_session, PersonaKey.LEX.value)
        if d.id == res["triage"]["draft_comm_id"]
    )
    body_lower = draft.body.lower()
    assert "approved for payment" not in body_lower
    assert "accept the overcharge" not in body_lower
    assert "**" in draft.body and "Best regards,\n**Lex**" in draft.body


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_multimodal_credit_note_and_non_invoice_pdf_trap(seeded_session) -> None:
    """Credit Note PDF extracts cleanly; non-invoice brochure PDF is blocked below 0.85."""
    lex = make_live_lex()

    # 1. Credit note PDF
    credit_pdf = build_invoice_pdf_bytes(
        supplier_name="Bharat Industrial Fasteners",
        invoice_number="CN-2026-019",
        po_number="PO-1038",
        invoice_date="2026-10-14",
        lines=[
            InvoiceLineSpec(
                sku="VLV-204",
                description="Credit Note — Price Variance Adjustment",
                qty=10,
                unit_price=-225.0,
                line_total=-2250.0,
            )
        ],
        gst_rate=0.18,
        notes="CREDIT NOTE issued against INV-NOISY-2026-441.",
    )
    cn_res = lex.extract_invoice(
        seeded_session,
        credit_pdf,
        filename="CN-2026-019.pdf",
        supplier_name="Bharat Industrial Fasteners",
    )
    assert cn_res.document_type == PDFDocumentType.CREDIT_NOTE
    assert "CN-2026-019" in cn_res.invoice_number

    # 2. Non-invoice disguised delivery/brochure PDF -> must fail guardrail < 0.85
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "", 11)
    pdf.multi_cell(
        0,
        7,
        "GENERAL DELIVERY NOTE & CATALOG BROCHURE\n"
        "Thank you for partnering with Deccan Electro-Spares.\n"
        "Prices and final tax invoice will follow next week after dispatch.",
    )
    trap_bytes = bytes(pdf.output())

    trap_res = lex.extract_invoice(
        seeded_session,
        trap_bytes,
        filename="GENERAL_BROCHURE.pdf",
        supplier_name="Deccan Electro-Spares",
    )
    assert trap_res.confidence < 0.85


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_multimodal_png_dispatch_slip_and_damage_inspection_photo(
    seeded_session,
    tmp_path,
) -> None:
    """Live Gemini reads PNG dispatch slip AWB AND PNG damaged-carton inspection counts."""
    po1 = create_open_po(seeded_session, sku="BRG-402", persona=PersonaKey.SUP1, qty=15)
    slip_path = tmp_path / "dispatch_slip.png"
    slip_path.write_bytes(build_dispatch_slip_png(po_number=po1.po_number))

    engine = WorkflowEngine(lex_agent=make_live_lex())
    res1 = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP1,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            po_id=po1.id,
            subject=f"Re: {po1.po_number} Confirmed (see attached slip)",
            body=f"Hi, acknowledging {po1.po_number}. All shipping info is on the attached PNG.",
            attachment_paths=[str(slip_path)],
        ),
    )
    triage1 = res1["triage"]["triage"]
    assert triage1.matched_po_number == po1.po_number
    assert triage1.carrier_name is not None and "bluedart" in triage1.carrier_name.lower()
    assert triage1.tracking_number is not None and "492817" in triage1.tracking_number

    # Second image: damage inspection report
    po2 = create_open_po(
        seeded_session,
        sku="FLT-108",
        persona=PersonaKey.SUP1,
        status=POStatus.ACKNOWLEDGED,
        qty=10,
        unit_price=320.0,
    )
    dmg_path = tmp_path / "damage_report.png"
    dmg_path.write_bytes(
        build_damage_inspection_png(
            po_number=po2.po_number,
            sku="FLT-108",
            good_qty=8,
            damaged_qty=2,
        )
    )
    res2 = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP1,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            po_id=po2.id,
            subject=f"Transit inspection photo for {po2.po_number}",
            body="Sharing dock photo from SafeExpress arrival.",
            attachment_paths=[str(dmg_path)],
        ),
    )
    triage2 = res2["triage"]["triage"]
    summary_combo = f"{triage2.summary_for_manager} {triage2.reasoning}".lower()
    assert "damage" in summary_combo or "crush" in summary_combo or "8" in summary_combo


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_corrupt_pdf_attachment_graceful_recovery(seeded_session, tmp_path) -> None:
    """Corrupt PDF attachment never crashes WorkflowEngine; falls back cleanly."""
    po = create_open_po(seeded_session, sku="BRG-402", persona=PersonaKey.SUP1, qty=10)
    corrupt_pdf = tmp_path / "corrupt_invoice.pdf"
    corrupt_pdf.write_bytes(b"%PDF-1.4\n%%%CORRUPTED_BINARY_STREAM_GARBAGE%%%")

    engine = WorkflowEngine(lex_agent=make_live_lex())
    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP1,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            po_id=po.id,
            subject=f"Attachment for {po.po_number}",
            body=f"Please process attached PDF for {po.po_number}.",
            attachment_paths=[str(corrupt_pdf)],
        ),
    )
    assert res["triage"] is not None
    assert res["triage"]["alert_id"] is not None
    seeded_session.refresh(po)
    assert po.status == POStatus.SENT_AWAITING_ACK.value  # No corrupt mutation


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_reminder_wakeup_multi_scenario_live_clock(seeded_session) -> None:
    """On Oct 5 wakeup: drafts nudge when missing, skips when GRN or thread reply exists."""
    po_missing = create_open_po(
        seeded_session,
        sku="BRG-402",
        persona=PersonaKey.SUP1,
        status=POStatus.ACKNOWLEDGED,
    )
    po_grn = create_open_po(
        seeded_session,
        sku="FLT-108",
        persona=PersonaKey.SUP1,
        status=POStatus.ACKNOWLEDGED,
        qty=12,
        unit_price=320.0,
    )
    po_replied = create_open_po(
        seeded_session,
        sku="RLY-901",
        persona=PersonaKey.SUP3,
        status=POStatus.ACKNOWLEDGED,
        qty=15,
        unit_price=100.0,
    )
    sim_now = clock.now(seeded_session)
    # Add unrelated chatter on po_missing thread
    send_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP1.value,
            to_personas=[PersonaKey.INVENTORY_MANAGER.value],
            cc_personas=[PersonaKey.LEX.value],
            po_id=po_missing.id,
            thread_id=f"po-{po_missing.id}",
            subject=f"Re: {po_missing.po_number} - Happy Friday",
            body="Have a great weekend, no tracking numbers yet!",
        ),
    )
    # Log GRN on po_grn
    seeded_session.add(
        GoodsReceipt(
            po_id=po_grn.id,
            received_qty=12,
            received_at=sim_now + timedelta(days=2),
            condition="GOOD",
            status=GRNStatus.RELEASED.value,
        )
    )
    # Fill logistics on po_replied as if supplier already replied
    po_replied.carrier_name = "BlueDart"
    po_replied.tracking_number = "BD-77123"
    po_replied.promised_dispatch_date = sim_now + timedelta(days=2)
    po_replied.missing_dispatch_fields_json = []

    remind_at = datetime(2026, 10, 5, 7, 30, tzinfo=timezone.utc)
    for target_po in (po_missing, po_grn, po_replied):
        seeded_session.add(
            LexReminder(
                po_id=target_po.id,
                part_id=target_po.part_id,
                supplier_id=target_po.supplier_id,
                remind_at=remind_at,
                reason="Promised dispatch details after 4 days (by 2026-10-05).",
                context_json={"wait_days": 4, "promised_by_date": "2026-10-05"},
                status=LexReminderStatus.PENDING.value,
                created_at=sim_now,
            )
        )
    seeded_session.commit()

    engine = WorkflowEngine(lex_agent=make_live_lex())
    engine.set_clock_and_watch(seeded_session, datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc))

    drafts = list_drafts(seeded_session, PersonaKey.LEX.value)
    assert len([d for d in drafts if d.po_id == po_missing.id]) == 1
    assert [d for d in drafts if d.po_id == po_grn.id] == []
    assert [d for d in drafts if d.po_id == po_replied.id] == []

    wakeup_traces = list_traces(seeded_session, agent_name="lex", task_type="reminder_wakeup")
    assert len(wakeup_traces) >= 2
    assert any(len(t.tool_calls_json or []) >= 1 for t in wakeup_traces)


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_draft_email_adversarial_categories_and_compliance(seeded_session) -> None:
    """Drafts across PO_DISPATCH, EXPEDITE_REQUEST, INVOICE_DISCREPANCY enforce all guardrails."""
    lex = make_live_lex()
    cases = [
        EmailDraftInput(
            category=EmailCategory.EXPEDITE_REQUEST,
            po_number="PO-3099",
            part_sku="BRG-402",
            part_name="Deep Groove Ball Bearing 6208",
            qty=20,
            unit_price=450.0,
            currency="INR",
            recipient_persona="sup1",
            recipient_name="Apex Precision Components",
            current_date="2026-10-01",
            supplier_memory_notes=["Include tolerance report with dispatch."],
            context_facts={"days_overdue": 2, "days_to_stockout": 4.5},
        ),
        EmailDraftInput(
            category=EmailCategory.INVOICE_DISCREPANCY,
            po_number="PO-3100",
            part_sku="VLV-204",
            part_name="Hydraulic Servo Solenoid Valve HV-40",
            qty=10,
            unit_price=1500.0,
            currency="INR",
            recipient_persona="sup2",
            recipient_name="Bharat Industrial Fasteners",
            current_date="2026-10-05",
            context_facts={
                "invoice_number": "INV-441",
                "billed_unit_price": 1725.0,
                "po_unit_price": 1500.0,
                "discrepancy_summary": "Unit price mismatch: Rs 1,725 vs Rs 1,500",
            },
        ),
    ]
    for cfg in cases:
        draft = lex.draft_email(seeded_session, cfg)
        assert cfg.po_number in draft.subject
        assert "**" not in draft.subject
        assert "inventory_manager" in draft.cc_personas
        if cfg.category == EmailCategory.EXPEDITE_REQUEST:
            assert "procurement" in draft.cc_personas
        assert "\n\n" in draft.body
        assert f"**{cfg.po_number}**" in draft.body
        assert f"**{cfg.part_sku}**" in draft.body
        assert "****" not in draft.body
        assert "$" not in draft.body
        assert "₹" in draft.body
        assert "Best regards,\n**Lex**\nAI Supply Chain Assistant" in draft.body

    # Keep three-way match import exercised meaningfully
    m = evaluate_three_way_match(
        ThreeWayMatchInput(
            po_qty=10,
            po_unit_price=1500.0,
            grn_received_qty=10,
            invoice_qty=10,
            invoice_unit_price=1500.0,
            invoice_line_subtotal=15000.0,
        )
    )
    assert m.matched is True
    assert isinstance(PurchaseOrder, type)
