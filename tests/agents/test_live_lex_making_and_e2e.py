import pytest
from sqlalchemy import select

from backend.agents.lex import StockRequestIntent
from backend.core import clock
from backend.core.enums import (
    InvoiceMatchStatus,
    LexReminderStatus,
    PersonaKey,
    POStatus,
    StockRequestStatus,
)
from backend.core.models import (
    ActionItem,
    Invoice,
    LexReminder,
    Part,
    PurchaseOrder,
    StockRequest,
)
from backend.mailroom import OutboundMessage, list_drafts
from backend.orchestrator.workflows import WorkflowEngine
from tests.agents.live_helpers import (
    LIVE_SKIP_REASON,
    make_live_lex,
    should_run_live_gemini,
)
from tests.helpers.invoice_pdf import InvoiceLineSpec, build_invoice_pdf_bytes


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_making_messy_bom_csv_with_unknown_sku_triggers_ambiguous(
    seeded_session,
    tmp_path,
) -> None:
    """BOM CSV mixing 2 valid SKUs + 1 unknown SKU (BRG-999-X) must escalate AMBIGUOUS safely."""
    csv_path = tmp_path / "line4_emergency_bom.csv"
    csv_path.write_text(
        "sku,part_name,qty_needed,shift_note\n"
        "BRG-402,Deep Groove Ball Bearing 6208,3,Line 4 spindle\n"
        "GSK-512,Viton High-Temp Gasket,5,Manifold seal\n"
        "BRG-999-X,Custom Prototype Bearing,2,Ask maintenance\n",
        encoding="utf-8",
    )
    sr_before = len(seeded_session.scalars(select(StockRequest)).all())
    brg = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    assert brg is not None
    brg_before = brg.on_hand_stock

    engine = WorkflowEngine(lex_agent=make_live_lex())
    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.MAKING,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            subject="Urgent Line 4 overhaul BOM attached",
            body=(
                "Hey team, second shift starts in 40 mins—please pull everything on the "
                "attached CSV BOM. Ignore the coffee machine note from morning."
            ),
            attachment_paths=[str(csv_path)],
        ),
    )

    triage = res["triage"]
    assert triage is not None
    assert triage["triage"].intent == StockRequestIntent.AMBIGUOUS
    assert triage["stock_request_ids"] == []
    seeded_session.refresh(brg)
    assert brg.on_hand_stock == brg_before
    assert len(seeded_session.scalars(select(StockRequest)).all()) == sr_before


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_making_shortage_negotiation_and_po_trigger_e2e(seeded_session) -> None:
    """Making shortage -> ask min qty -> noisy reply '2 units' -> approve -> auto-PO."""
    part = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    assert part is not None
    part.on_hand_stock = 4
    seeded_session.commit()

    engine = WorkflowEngine(lex_agent=make_live_lex())
    req_res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.MAKING,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            subject="Line 2 Bearing Pull - 10 units BRG-402",
            body=(
                f"Hi Inventory team, we need 10 units of {part.sku} ({part.name}) for "
                "Spindle Assembly Line 2 today. By the way, supervisor Raj is out at lunch."
            ),
        ),
    )
    triage = req_res["triage"]
    assert triage["triage"].intent == StockRequestIntent.STOCK_REQUEST
    assert len(triage["stock_request_ids"]) == 1
    shortage_alert_id = triage["alert_ids"][0]
    shortage_alert = seeded_session.get(ActionItem, shortage_alert_id)
    assert shortage_alert is not None and shortage_alert.type == "STOCK_SHORTAGE"

    ask_out = engine.execute_alert_cta(
        seeded_session, shortage_alert_id, action="ask_making_minimum"
    )
    assert ask_out["status"] == StockRequestStatus.AWAITING_MIN_QTY.value

    reply_res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.MAKING,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            thread_id=req_res["email"].thread_id,
            subject="Re: [BRG-402] Minimum Quantity Needed",
            body=(
                "Checked with line lead—we can limp through tonight with a minimum of 2 units "
                "right now until your next supplier delivery arrives."
            ),
        ),
    )
    reply_triage = reply_res["triage"]
    assert reply_triage["triage"].intent == StockRequestIntent.MINIMUM_QTY_CONFIRMATION
    min_alert_id = reply_triage["alert_ids"][0]

    final_out = engine.execute_alert_cta(
        seeded_session, min_alert_id, action="approve_stock_withdrawal"
    )
    assert final_out["on_hand_after"] == 2
    assert final_out["created_po_id"] is not None
    po = seeded_session.get(PurchaseOrder, final_out["created_po_id"])
    assert po is not None and po.status == POStatus.PENDING_DISPATCH_APPROVAL.value


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_making_ambiguous_floor_slang_never_mutates_stock(seeded_session) -> None:
    """Vague floor slang without SKU or firm qty escalates AMBIGUOUS_STOCK_REQUEST."""
    part = seeded_session.scalar(select(Part).where(Part.sku == "RLY-901"))
    assert part is not None
    initial_stock = part.on_hand_stock
    sr_before = len(seeded_session.scalars(select(StockRequest)).all())

    engine = WorkflowEngine(lex_agent=make_live_lex())
    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.MAKING,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            subject="Need some spares before second shift",
            body=(
                "Yo can someone drop off a handful of those 24V relays or maybe "
                "the stainless hex bolts whenever the forklift is free?"
            ),
        ),
    )
    triage = res["triage"]
    assert triage["triage"].intent == StockRequestIntent.AMBIGUOUS
    assert triage["stock_request_ids"] == []
    alert = seeded_session.get(ActionItem, triage["alert_ids"][0])
    assert alert is not None and alert.type == "AMBIGUOUS_STOCK_REQUEST"
    seeded_session.refresh(part)
    assert part.on_hand_stock == initial_stock
    assert len(seeded_session.scalars(select(StockRequest)).all()) == sr_before


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_full_order_to_closure_autonomous_agent_e2e(seeded_session, tmp_path) -> None:
    """End-to-end live lifecycle: withdraw -> draft PO -> dispatch -> +4d deferral ->
    day-4 reminder wakeup -> partial GRN + v1 overcharge quarantine -> v2 invoice release.
    """
    part = seeded_session.scalar(select(Part).where(Part.sku == "BLT-301"))
    assert part is not None
    part.on_hand_stock = max(part.rop + 5, 220)  # Healthy runway for 4d deferral
    seeded_session.commit()

    engine = WorkflowEngine(lex_agent=make_live_lex())

    # 1. Withdraw enough stock to drop IP below ROP -> auto-creates PENDING_DISPATCH_APPROVAL PO
    withdraw_qty = part.on_hand_stock - (part.rop - 2)
    w_res = engine.withdraw_stock(seeded_session, part_id=part.id, qty=withdraw_qty)
    po = w_res["created_po"]
    assert po is not None and po.status == POStatus.PENDING_DISPATCH_APPROVAL.value

    # 2. Manager edits & dispatches PO via dispatch_edited_po
    d_res = engine.dispatch_edited_po(seeded_session, po.id, qty=30)
    po = d_res["po"]
    assert po.status == POStatus.SENT_AWAITING_ACK.value

    # 3. Sup2 acknowledges and defers tracking by 4 days
    engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP2,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            po_id=po.id,
            subject=f"Re: {po.po_number} Confirmed",
            body=(
                f"Confirmed {po.po_number} for 30 units of BLT-301 at Rs {po.unit_price:.0f}/unit "
                "(ETA 2026-10-16). Dispatch tracking details will follow after 4 days (2026-10-05)."
            ),
        ),
    )
    seeded_session.refresh(po)
    assert po.status == POStatus.ACKNOWLEDGED.value
    rem = seeded_session.scalar(
        select(LexReminder).where(
            LexReminder.po_id == po.id,
            LexReminder.status == LexReminderStatus.PENDING.value,
        )
    )
    assert rem is not None

    # 4. Advance +3d -> no early nudge; advance +1d -> reminder wakes Lex & drafts nudge
    day3 = engine.advance_clock_and_watch(seeded_session, days=3)
    assert not any(
        e.po_id == po.id and e.event_type == "DISPATCH_DETAILS_NUDGE_DRAFTED"
        for e in day3["sla_events"]
    )
    day4 = engine.advance_clock_and_watch(seeded_session, days=1)
    assert any(
        e.po_id == po.id and e.event_type == "DISPATCH_DETAILS_NUDGE_DRAFTED"
        for e in day4["sla_events"]
    )

    # 5. Partial GRN (15 of 30 units) + v1 overcharged PDF invoice -> QUARANTINED_INVOICE_MISMATCH
    engine.record_grn(seeded_session, po_id=po.id, received_qty=15, condition="GOOD")
    v1_pdf = tmp_path / "INV-E2E-V1.pdf"
    v1_pdf.write_bytes(
        build_invoice_pdf_bytes(
            supplier_name="Bharat Industrial Fasteners",
            invoice_number="INV-E2E-V1",
            po_number=po.po_number,
            invoice_date=clock.to_ist_date(clock.now(seeded_session)).isoformat(),
            lines=[
                InvoiceLineSpec(
                    sku="BLT-301",
                    description="M12 Alloy Hex Bolt Pack",
                    qty=15,
                    unit_price=125.0,  # Overcharge vs PO 100.0
                )
            ],
        )
    )
    engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP2,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            po_id=po.id,
            subject=f"Invoice INV-E2E-V1 for {po.po_number} (15 units)",
            body=f"Attached v1 invoice for partial delivery of {po.po_number}.",
            attachment_paths=[str(v1_pdf)],
        ),
    )
    seeded_session.refresh(po)
    assert po.status == POStatus.QUARANTINED_INVOICE_MISMATCH.value

    # 6. Corrected v2 PDF invoice matching GRN qty 15 & PO price 100 -> releases partial GRN
    v2_pdf = tmp_path / "INV-E2E-V2.pdf"
    v2_pdf.write_bytes(
        build_invoice_pdf_bytes(
            supplier_name="Bharat Industrial Fasteners",
            invoice_number="INV-E2E-V2",
            po_number=po.po_number,
            invoice_date=clock.to_ist_date(clock.now(seeded_session)).isoformat(),
            lines=[
                InvoiceLineSpec(
                    sku="BLT-301",
                    description="M12 Alloy Hex Bolt Pack",
                    qty=15,
                    unit_price=100.0,
                )
            ],
        )
    )
    engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP2,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            po_id=po.id,
            subject=f"Revised Invoice INV-E2E-V2 for {po.po_number}",
            body=f"Apologies, attached corrected invoice INV-E2E-V2 for {po.po_number}.",
            attachment_paths=[str(v2_pdf)],
        ),
    )
    seeded_session.refresh(po)
    assert po.status == POStatus.ACKNOWLEDGED.value  # 15 released, 15 remaining on order
    v2_inv = seeded_session.scalar(
        select(Invoice).where(Invoice.po_id == po.id, Invoice.invoice_number == "INV-E2E-V2")
    )
    assert v2_inv is not None and v2_inv.match_status == InvoiceMatchStatus.MATCHED.value
    assert isinstance(list_drafts(seeded_session, PersonaKey.LEX.value), list)
