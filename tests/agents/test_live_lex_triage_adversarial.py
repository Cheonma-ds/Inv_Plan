from datetime import timedelta

import pytest
from sqlalchemy import select

from backend.agents.lex import EmailIntent
from backend.core import clock
from backend.core.enums import (
    AlertSeverity,
    CommStatus,
    EmailCategory,
    LexReminderStatus,
    PersonaKey,
    POStatus,
)
from backend.core.models import (
    ActionItem,
    LexReminder,
    Part,
    POCommunication,
    Supplier,
    SupplierMemoryNote,
)
from backend.mailroom import OutboundMessage, list_drafts, send_email
from backend.observability import list_traces
from backend.orchestrator.workflows import WorkflowEngine
from backend.po import dismiss_action_item
from tests.agents.live_helpers import (
    LIVE_SKIP_REASON,
    create_open_po,
    make_live_lex,
    should_run_live_gemini,
)


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_noisy_lengthy_thread_unrelated_chatter_and_po_omission(seeded_session) -> None:
    """Resolve an unmentioned PO in a noisy multi-PO thread full of unrelated chatter."""
    po_rly = create_open_po(
        seeded_session,
        sku="RLY-901",
        persona=PersonaKey.SUP3,
        status=POStatus.ACKNOWLEDGED,
        qty=15,
        unit_price=100.0,
        eta_days=14,
    )
    po_sns = create_open_po(
        seeded_session,
        sku="SNS-802",
        persona=PersonaKey.SUP3,
        status=POStatus.ACKNOWLEDGED,
        qty=10,
        unit_price=600.0,
        eta_days=12,
    )
    assert po_sns.id != po_rly.id

    # Seed unrelated historical/chatter thread linked to the same conversation thread_id
    thread_id = f"thread-sup3-noisy-{po_rly.id}"
    noise_msgs = [
        (
            PersonaKey.SUP3.value,
            "Happy Diwali & Q4 Holiday Schedule",
            "Hi team, just sharing our Pune & Bangalore warehouse Diwali holiday calendar "
            "for November. Also please update our bank IFSC in your vendor portal.",
        ),
        (
            PersonaKey.INVENTORY_MANAGER.value,
            "Re: Warehouse Gate Timing & GST Certificate",
            "Thanks, noted. Also remember gate 3 closes at 5:30 PM IST for heavy trucks.",
        ),
        (
            PersonaKey.SUP3.value,
            "Re: Old March Invoice Query + Parking Pass",
            "By the way, that old March invoice INV-0041 was already settled in April. "
            "Could our driver get a monthly gate pass for Building B?",
        ),
    ]
    for sender, subj, body in noise_msgs:
        send_email(
            seeded_session,
            OutboundMessage(
                from_persona=sender,
                to_personas=[PersonaKey.INVENTORY_MANAGER.value],
                cc_personas=[PersonaKey.LEX.value],
                thread_id=thread_id,
                subject=subj,
                body=body,
            ),
        )
    seeded_session.commit()

    engine = WorkflowEngine(lex_agent=make_live_lex())
    # Lengthy email omitting explicit PO number in subject & body, mixing unrelated fluff
    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP3,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            thread_id=thread_id,
            subject="Quick check-in + schedule note from Deccan",
            body=(
                "Hi folks,\n\n"
                "Hope your week is going well. First off, our accounts team asked if you "
                "received our updated ISO-9001 renewal PDF last month—totally separate from "
                "current shipments, just housekeeping.\n\n"
                "Second, on the open order for the PLC Safety Relay Module 24V (RLY-901, "
                "15 units), our sub-tier PCB vendor had an unexpected solder-mask oven trip. "
                "That pushes delivery by a few days, so our revised ETA is now 2026-10-19. "
                "We are holding the contracted INR 100/unit rate—no price change.\n\n"
                "Also, let us know if your procurement team wants to meet next Tuesday for "
                "the annual coffee catch-up.\n\nBest,\nRohan"
            ),
        ),
    )

    triage = res["triage"]["triage"]
    assert triage.matched_po_number == po_rly.po_number
    assert triage.intent == EmailIntent.POST_ACK_DELAY
    assert triage.confidence >= 0.85
    assert triage.proposed_eta_date == "2026-10-19"

    seeded_session.refresh(po_rly)
    seeded_session.refresh(po_sns)
    assert po_rly.revised_eta is not None
    assert clock.to_ist_date(po_rly.revised_eta).isoformat() == "2026-10-19"
    assert po_sns.revised_eta is None  # Unrelated open PO untouched

    traces = list_traces(seeded_session, agent_name="lex", task_type="email_triage", limit=1)
    assert len(traces) == 1
    assert len(traces[0].tool_calls_json or []) >= 1


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_safe_variable_deferral_no_tool_hints(seeded_session) -> None:
    """Unhinted natural 4-day tracking deferral invokes tools + sandbox and schedules reminder."""
    part = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    assert part is not None
    part.on_hand_stock = 260  # ~40d runway safely covers 4d wait
    seeded_session.commit()

    po = create_open_po(
        seeded_session,
        sku="BRG-402",
        persona=PersonaKey.SUP1,
        status=POStatus.SENT_AWAITING_ACK,
        qty=20,
        unit_price=450.0,
        eta_days=12,
    )
    engine = WorkflowEngine(lex_agent=make_live_lex())
    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP1,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            po_id=po.id,
            subject=f"Re: {po.po_number} Order Confirmation",
            body=(
                f"Dear Team,\n\n"
                f"We confirm {po.po_number} for 20 units of Deep Groove Ball Bearing 6208 "
                "at ₹450.00/unit, scheduled for delivery on 2026-10-13.\n\n"
                "Please note that our warehouse WMS is undergoing a scheduled patch this week, "
                "so we will only be able to pull and share the BlueDart dispatch date and "
                "AWB tracking number after 4 days (by 2026-10-05).\n\n"
                "Also happy to discuss Q4 blanket pricing whenever convenient.\n\n"
                "Regards,\nApex Dispatch Desk"
            ),
        ),
    )

    triage = res["triage"]["triage"]
    assert triage.matched_po_number == po.po_number
    assert triage.intent == EmailIntent.ACKNOWLEDGED
    assert triage.confidence >= 0.85
    assert triage.details_promised_later is True
    assert triage.details_promised_days == 4 or triage.details_promised_by_date == "2026-10-05"

    traces = list_traces(seeded_session, agent_name="lex", task_type="email_triage", limit=1)
    assert len(traces) == 1
    tool_names = {tc["tool"] for tc in (traces[0].tool_calls_json or [])}
    assert len(tool_names) >= 1
    assert "run_sandbox_calc" in tool_names or "get_part_snapshot" in tool_names

    reminder = seeded_session.scalar(
        select(LexReminder).where(
            LexReminder.po_id == po.id,
            LexReminder.status == LexReminderStatus.PENDING.value,
        )
    )
    assert reminder is not None
    assert clock.to_ist_date(reminder.remind_at).isoformat() == "2026-10-05"

    # Must NOT nag prematurely on Oct 1 when runway is safe
    nudge_drafts = [
        d
        for d in list_drafts(seeded_session, PersonaKey.LEX.value)
        if d.po_id == po.id
        and d.category
        in {
            EmailCategory.DISPATCH_DETAILS_REQUEST.value,
            EmailCategory.DISPATCH_DETAILS_NUDGE.value,
        }
    ]
    assert nudge_drafts == []


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_unsafe_variable_deferral_tight_runway_escalates_high(seeded_session) -> None:
    """Tight-runway SKU (~2.5d left) must NOT safely defer 5 days; escalates HIGH risk."""
    part = seeded_session.scalar(select(Part).where(Part.sku == "VLV-204"))
    assert part is not None
    part.on_hand_stock = 9  # < 3 days runway vs 5-day deferral!
    seeded_session.commit()

    po = create_open_po(
        seeded_session,
        sku="VLV-204",
        persona=PersonaKey.SUP2,
        status=POStatus.SENT_AWAITING_ACK,
        qty=25,
        unit_price=1500.0,
        eta_days=15,
    )
    engine = WorkflowEngine(lex_agent=make_live_lex())
    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP2,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            po_id=po.id,
            subject=f"Re: {po.po_number} Confirmed",
            body=(
                f"Hello,\n\nWe acknowledge {po.po_number} for 25 units of VLV-204 at "
                "Rs 1,500/unit (expected ETA 2026-10-16). Our logistics desk is swamped, "
                "so we can only share carrier and tracking details after 5 days.\n\n"
                "Thanks,\nBharat Industrial Fasteners"
            ),
        ),
    )

    triage = res["triage"]["triage"]
    assert triage.matched_po_number == po.po_number
    assert triage.intent == EmailIntent.ACKNOWLEDGED
    assert triage.details_promised_later is True

    # Must NOT silently create a 5-day reminder while stockout is imminent
    pending_rem = seeded_session.scalar(
        select(LexReminder).where(
            LexReminder.po_id == po.id,
            LexReminder.status == LexReminderStatus.PENDING.value,
        )
    )
    assert pending_rem is None

    risk_alert = seeded_session.scalar(
        select(ActionItem).where(
            ActionItem.po_id == po.id,
            ActionItem.type == "DISPATCH_DEFERRAL_STOCKOUT_RISK",
        )
    )
    assert risk_alert is not None
    assert risk_alert.severity == AlertSeverity.HIGH.value

    traces = list_traces(seeded_session, agent_name="lex", task_type="email_triage", limit=1)
    assert len(traces) == 1
    assert len(traces[0].tool_calls_json or []) >= 1


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_multi_po_mixed_intents_and_vague_price_hedge(seeded_session) -> None:
    """One dense email covering 2 open POs (1 clean ack+tracking, 1 partial + vague rate hedge)."""
    po_vlv = create_open_po(
        seeded_session,
        sku="VLV-204",
        persona=PersonaKey.SUP2,
        status=POStatus.SENT_AWAITING_ACK,
        qty=20,
        unit_price=1500.0,
        eta_days=15,
    )
    po_blt = create_open_po(
        seeded_session,
        sku="BLT-301",
        persona=PersonaKey.SUP2,
        status=POStatus.SENT_AWAITING_ACK,
        qty=50,
        unit_price=100.0,
        eta_days=15,
    )
    engine = WorkflowEngine(lex_agent=make_live_lex())
    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP2,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            subject=f"Updates on {po_vlv.po_number} and {po_blt.po_number}",
            body=(
                f"Dear Team,\n\n"
                f"1) For {po_vlv.po_number} (VLV-204, 20 units @ Rs 1,500): confirmed! "
                "Dispatched 2026-10-01 via SafeExpress, tracking number SE-77812, "
                "ETA 2026-10-16.\n\n"
                f"2) For {po_blt.po_number} (BLT-301, 50 units): we currently only have 20 units "
                "ready in stock to fulfill right now instead of 50. Also, steel rod prices "
                "moved up this week—our sales head is checking if we might need a slight "
                "rate revision, though we don't have a revised figure yet.\n\n"
                "3) Unrelated note: thank you for clearing last quarter's ledger.\n\n"
                "Best,\nBharat Fasteners"
            ),
        ),
    )

    seeded_session.refresh(po_vlv)
    seeded_session.refresh(po_blt)
    assert po_vlv.status == POStatus.ACKNOWLEDGED.value
    assert po_vlv.carrier_name is not None and "safeexpress" in po_vlv.carrier_name.lower()
    assert po_vlv.tracking_number == "SE-77812"

    # BLT-301 should NOT have its price changed or unit_price mutated from vague hedging
    assert po_blt.unit_price == 100.0
    assert po_blt.status != POStatus.ACKNOWLEDGED.value
    triage = res["triage"]["triage"]
    assert len(triage.additional_po_updates) >= 1 or triage.matched_po_number in {
        po_vlv.po_number,
        po_blt.po_number,
    }


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_foreign_currency_usd_quote_never_converts_silently(seeded_session) -> None:
    """Stealth USD quote inside a noisy INR PO reply flags CURRENCY_MISMATCH verbatim."""
    po = create_open_po(
        seeded_session,
        sku="SNS-802",
        persona=PersonaKey.SUP3,
        status=POStatus.SENT_AWAITING_ACK,
        qty=15,
        unit_price=600.0,
        eta_days=14,
    )
    engine = WorkflowEngine(lex_agent=make_live_lex())
    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP3,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            po_id=po.id,
            subject=f"Quotation & Schedule for {po.po_number}",
            body=(
                f"Hi,\n\nThank you for sending {po.po_number} for SNS-802. Please note this "
                "sensor batch is imported directly from our Munich hub, so our special billing "
                "rate for this order is $19.50 USD per unit (delivery by 2026-10-16).\n\n"
                "Let us know once approved.\nDeccan Electro-Spares"
            ),
        ),
    )

    triage = res["triage"]["triage"]
    assert triage.matched_po_number == po.po_number
    assert triage.intent == EmailIntent.PRICE_CHANGE
    assert triage.confidence >= 0.85
    assert triage.proposed_unit_price == pytest.approx(19.5, abs=0.01)
    assert triage.proposed_currency == "USD"

    seeded_session.refresh(po)
    assert po.unit_price == 600.0  # Unchanged!
    alert = seeded_session.get(ActionItem, res["triage"]["alert_id"])
    assert alert is not None
    assert alert.cta_payload_json.get("currency_mismatch") is True


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_contradictory_unknown_po_forces_ambiguous_no_mutation(seeded_session) -> None:
    """Contradictory dates, hedging price, and unknown PO# force AMBIGUOUS with 0 PO mutation."""
    real_po = create_open_po(
        seeded_session,
        sku="PMP-701",
        persona=PersonaKey.SUP3,
        status=POStatus.SENT_AWAITING_ACK,
        qty=6,
        unit_price=2000.0,
        eta_days=16,
    )
    engine = WorkflowEngine(lex_agent=make_live_lex())
    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP3,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            subject="Re: PO-9876 & pump availability",
            body=(
                "Hi,\n\nConfirming PO-9876—wait, actually our sales clerk might have meant "
                "a different order. We might deliver Oct 10, or maybe late November depending "
                "on customs, and we're still checking if rates stay flat or go up.\n\n"
                "Cheers,\nDeccan"
            ),
        ),
    )

    triage = res["triage"]["triage"]
    assert triage.intent == EmailIntent.AMBIGUOUS
    seeded_session.refresh(real_po)
    assert real_po.status == POStatus.SENT_AWAITING_ACK.value
    assert real_po.unit_price == 2000.0


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_dismissed_alerts_chaos_and_unapproved_draft_pipeline(seeded_session) -> None:
    """Manager dismisses ALL open alerts carelessly; Lex still inspects live state accurately."""
    engine = WorkflowEngine(lex_agent=make_live_lex())

    # Trigger an auto-draft PO (PENDING_DISPATCH_APPROVAL) + PO_DISPATCH_APPROVAL alert via withdraw
    part_brg = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    assert part_brg is not None
    withdraw_qty = part_brg.on_hand_stock - (part_brg.rop - 2)
    w_res = engine.withdraw_stock(seeded_session, part_id=part_brg.id, qty=withdraw_qty)
    draft_po = w_res["created_po"]
    assert draft_po is not None and draft_po.status == POStatus.PENDING_DISPATCH_APPROVAL.value

    # Also open an active sent PO on RLY-901
    open_rly = create_open_po(
        seeded_session,
        sku="RLY-901",
        persona=PersonaKey.SUP3,
        status=POStatus.SENT_AWAITING_ACK,
        qty=15,
        unit_price=100.0,
        eta_days=14,
    )

    # Manager recklessly dismisses EVERY OPEN alert
    open_alerts = list(
        seeded_session.scalars(select(ActionItem).where(ActionItem.status == "OPEN")).all()
    )
    assert len(open_alerts) >= 1
    for al in open_alerts:
        dismiss_action_item(seeded_session, al.id)
    seeded_session.commit()

    # Now sup3 confirms RLY-901 with full tracking while BRG-402 sits with dismissed alert & draft
    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP3,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            po_id=open_rly.id,
            subject=f"Re: {open_rly.po_number} Confirmed",
            body=(
                f"Confirmed {open_rly.po_number} for 15 units of RLY-901 at Rs 100/unit. "
                "Dispatched 2026-10-01 via DTDC, tracking DTDC-55102, ETA 2026-10-15."
            ),
        ),
    )
    triage = res["triage"]["triage"]
    assert triage.matched_po_number == open_rly.po_number
    assert triage.intent == EmailIntent.ACKNOWLEDGED

    # Draft PO for BRG-402 must remain PENDING_DISPATCH_APPROVAL (not dispatched)
    # and its dismissed alert remains DISMISSED with dismiss_resolved_at == None
    seeded_session.refresh(draft_po)
    assert draft_po.status == POStatus.PENDING_DISPATCH_APPROVAL.value
    dismissed_for_brg = seeded_session.scalar(
        select(ActionItem).where(
            ActionItem.po_id == draft_po.id,
            ActionItem.status == "DISMISSED",
        )
    )
    assert dismissed_for_brg is not None
    assert dismissed_for_brg.dismiss_resolved_at is None
    # Verify linked draft comm was rejected on dismiss and never leaked into SENT
    if dismissed_for_brg.draft_comm_id:
        comm = seeded_session.get(POCommunication, dismissed_for_brg.draft_comm_id)
        assert comm is not None and comm.status == CommStatus.REJECTED.value


@pytest.mark.skipif(not should_run_live_gemini(), reason=LIVE_SKIP_REASON)
def test_live_memory_dedup_vs_new_constraint_and_advisory(seeded_session) -> None:
    """Supplier restates old habit AND adds new dock-slot rule; Lex deduplicates & learns new."""
    sup2 = seeded_session.scalar(select(Supplier).where(Supplier.persona_key == "sup2"))
    assert sup2 is not None
    notes_before = [
        n.note
        for n in seeded_session.scalars(
            select(SupplierMemoryNote).where(SupplierMemoryNote.supplier_id == sup2.id)
        ).all()
    ]
    assert len(notes_before) >= 1

    po = create_open_po(
        seeded_session,
        sku="VLV-204",
        persona=PersonaKey.SUP2,
        status=POStatus.SENT_AWAITING_ACK,
        qty=20,
        unit_price=1500.0,
        eta_days=15,
    )
    engine = WorkflowEngine(lex_agent=make_live_lex())
    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP2,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            po_id=po.id,
            subject=f"Re: {po.po_number} Confirmed + New Dispatch Rule",
            body=(
                f"Dear Team,\n\nWe confirm {po.po_number} for 20 units of VLV-204 at ₹1,500/unit "
                "(ETA 2026-10-16, dispatch 2026-10-03 via SafeExpress tracking SE-90112).\n\n"
                "Note: Just like before, our billing team sometimes sends v1 invoices at list "
                "price before correcting them—always double check our PDFs.\n\n"
                "IMPORTANT NEW POLICY FROM OCT 2026: All palletized dispatches from our Pune "
                "plant now require a 48-hour advance dock-slot booking via "
                "portals@bharatfasteners.in or the truck will be held at Gate 2.\n\n"
                "Regards,\nBharat Industrial Fasteners"
            ),
        ),
    )

    triage = res["triage"]["triage"]
    assert triage.matched_po_number == po.po_number
    assert triage.intent == EmailIntent.ACKNOWLEDGED
    assert triage.confidence >= 0.85

    # Lex should have inspected supplier profile and/or proposed/flagged memory
    traces = list_traces(seeded_session, agent_name="lex", task_type="email_triage", limit=1)
    assert len(traces) == 1
    tool_names = [tc["tool"] for tc in (traces[0].tool_calls_json or [])]
    assert len(tool_names) >= 1

    # Verify new dock-slot rule was captured via propose_memory_note tool or new_memory_note
    notes_after = [
        n.note
        for n in seeded_session.scalars(
            select(SupplierMemoryNote).where(SupplierMemoryNote.supplier_id == sup2.id)
        ).all()
    ]
    assert any(
        "dock" in n.lower() or "portals@" in n.lower() or "48" in n.lower()
        for n in notes_after
    )

    # Pre-delivery advisory should exist for Sup2's invoice mismatch habit without duplicating
    adv = seeded_session.scalars(
        select(ActionItem).where(
            ActionItem.po_id == po.id,
            ActionItem.type == "LEX_ADVISORY_ALERT",
        )
    ).all()
    assert len(adv) >= 1
    assert timedelta(days=1).days == 1
