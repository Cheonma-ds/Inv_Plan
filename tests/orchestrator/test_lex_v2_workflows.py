from datetime import timedelta

from sqlalchemy import select

from backend.agents.lex import EmailIntent, EmailTriageResult
from backend.core import clock
from backend.core.enums import (
    AlertSeverity,
    LexReminderStatus,
    PersonaKey,
    POStatus,
)
from backend.core.models import (
    ActionItem,
    LexReminder,
    Part,
    PurchaseOrder,
    Supplier,
    SupplierMemoryNote,
)
from backend.orchestrator.lex_tools import (
    LexToolTracker,
    build_lex_tool_registry,
    cancel_pending_lex_reminders_for_po,
    evaluate_memory_advisory_alerts,
)
from backend.orchestrator.logistics_workflow import (
    evaluate_deferral_safety,
    resolve_deferral_schedule,
)
from backend.po import create_po, transition_po


def _create_open_po(
    session,
    *,
    sku: str = "RLY-901",
    persona: PersonaKey = PersonaKey.SUP3,
    status: POStatus = POStatus.SENT_AWAITING_ACK,
    qty: int = 10,
    eta_days: int = 14,
) -> PurchaseOrder:
    sim_now = clock.now(session)
    part = session.scalar(select(Part).where(Part.sku == sku))
    sup = session.scalar(select(Supplier).where(Supplier.persona_key == persona.value))
    assert part is not None and sup is not None
    po = create_po(
        session,
        part_id=part.id,
        supplier_id=sup.id,
        qty=qty,
        unit_price=500.0,
        initial_status=POStatus.PENDING_DISPATCH_APPROVAL,
    )
    transition_po(
        session,
        po,
        POStatus.SENT_AWAITING_ACK,
        promised_eta=sim_now + timedelta(days=eta_days),
    )
    if status == POStatus.ACKNOWLEDGED:
        transition_po(
            session,
            po,
            POStatus.ACKNOWLEDGED,
            promised_eta=sim_now + timedelta(days=eta_days),
        )
    session.flush()
    return po


def test_all_13_lex_tools_execute_and_deduplicate_cleanly(seeded_session) -> None:
    po = _create_open_po(seeded_session, sku="RLY-901", persona=PersonaKey.SUP2)
    tracker = LexToolTracker()
    registry = build_lex_tool_registry(seeded_session, tracker=tracker)

    expected_tools = {
        "list_parts_summary",
        "list_alerts",
        "get_planning_overview",
        "get_part_snapshot",
        "get_consumption_history",
        "get_po_details",
        "list_open_pos",
        "get_supplier_profile",
        "get_mailroom_thread",
        "run_sandbox_calc",
        "schedule_lex_reminder",
        "propose_memory_note",
        "create_advisory_alert",
    }
    assert expected_tools == set(registry.names())

    parts_res = registry.execute("list_parts_summary", {})
    assert parts_res.ok and parts_res.data["total_parts"] == 15
    assert len(parts_res.data["parts"]) == 15

    alerts_res = registry.execute("list_alerts", {"status": "ALL"})
    assert alerts_res.ok and "alerts" in alerts_res.data

    plan_res = registry.execute("get_planning_overview", {})
    assert plan_res.ok and len(plan_res.data["supplier_scores"]) == 3
    assert len(plan_res.data["recommendations"]) == 15

    part_res = registry.execute("get_part_snapshot", {"sku_or_part_id": "RLY-901"})
    assert part_res.ok and part_res.data["found"] is True
    assert part_res.data["sku"] == "RLY-901"
    assert part_res.data["days_to_stockout"] is not None

    hist_res = registry.execute(
        "get_consumption_history", {"part_id": part_res.data["part_id"], "days": 30}
    )
    assert hist_res.ok and len(hist_res.data["daily_history"]) == 30

    po_res = registry.execute("get_po_details", {"po_number_or_id": po.po_number})
    assert po_res.ok and po_res.data["po_number"] == po.po_number

    open_res = registry.execute("list_open_pos", {"supplier_id": po.supplier_id})
    assert open_res.ok and open_res.data["count"] >= 1

    sup_res = registry.execute("get_supplier_profile", {"supplier_key_or_id": "sup2"})
    assert sup_res.ok and sup_res.data["persona_key"] == "sup2"
    assert len(sup_res.data["memory_notes"]) >= 1

    thread_res = registry.execute("get_mailroom_thread", {"thread_id": "nonexistent-thread"})
    assert thread_res.ok and thread_res.data["message_count"] == 0

    calc_res = registry.execute(
        "run_sandbox_calc",
        {
            "python_script": "result = {'safe': runway > wait_days + 2, 'gap': runway - wait_days}",
            "variables": {"runway": 24.0, "wait_days": 4},
        },
    )
    assert calc_res.ok and calc_res.data["result"] == {"safe": True, "gap": 20.0}

    bad_calc = registry.execute("run_sandbox_calc", {"python_script": "import os\nos.listdir('.')"})
    assert bad_calc.ok is False and "Security" in (bad_calc.error or "")

    remind_at_iso = (clock.now(seeded_session) + timedelta(days=4)).isoformat()
    rem1 = registry.execute(
        "schedule_lex_reminder",
        {
            "po_id": po.id,
            "remind_at_iso": remind_at_iso,
            "reason": "Wait 4 days for ERP migration tracking export",
            "suggested_action": "Draft nudge if still missing",
        },
    )
    assert rem1.ok and rem1.data["action"] == "scheduled"
    rem2 = registry.execute(
        "schedule_lex_reminder",
        {
            "po_id": po.id,
            "remind_at_iso": remind_at_iso,
            "reason": "Updated 4-day check-in",
        },
    )
    assert rem2.ok and rem2.data["action"] == "updated"
    assert rem2.data["reminder_id"] == rem1.data["reminder_id"]

    dup_mem = registry.execute(
        "propose_memory_note",
        {
            "supplier_id": po.supplier_id,
            "note": (
                "Chronically delivers 7-10 days past quoted 15d lead time; "
                "verify unit prices on PDFs."
            ),
            "reasoning": "Repeated observation",
        },
    )
    assert dup_mem.ok and dup_mem.data["saved"] is False
    assert dup_mem.data["action"] == "deduplicated"

    new_mem = registry.execute(
        "propose_memory_note",
        {
            "supplier_id": po.supplier_id,
            "note": "Requires GST invoice copy inside pallet pouch",
            "reasoning": "Dock receiving check",
        },
    )
    assert new_mem.ok and new_mem.data["saved"] is True

    adv1 = registry.execute(
        "create_advisory_alert",
        {
            "title": f"Verify invoice pricing on {po.po_number}",
            "summary": "Sup2 often has v1 unit-price mismatches.",
            "severity": "MEDIUM",
            "po_id": po.id,
        },
    )
    assert adv1.ok and adv1.data["created"] is True
    adv2 = registry.execute(
        "create_advisory_alert",
        {
            "title": f"Verify invoice pricing on {po.po_number}",
            "summary": "Duplicate advisory check.",
            "severity": "MEDIUM",
            "po_id": po.id,
        },
    )
    assert adv2.ok and adv2.data["deduplicated"] is True


def test_deterministic_deferral_safety_reminder_cancel_and_memory_advisories(
    seeded_session,
) -> None:
    sim_now = clock.now(seeded_session)
    part = seeded_session.scalar(select(Part).where(Part.sku == "VLV-204"))
    sup2 = seeded_session.scalar(select(Supplier).where(Supplier.persona_key == "sup2"))
    assert part is not None and sup2 is not None
    part.on_hand_stock = 8
    seeded_session.flush()

    po = _create_open_po(seeded_session, sku="VLV-204", persona=PersonaKey.SUP2, eta_days=15)
    triage_stub = EmailTriageResult(
        matched_po_number=po.po_number,
        intent=EmailIntent.ACKNOWLEDGED,
        confidence=0.95,
        details_promised_later=True,
        details_promised_days=4,
        summary_for_manager="Confirmed with 4d deferral.",
        suggested_action="Check safety.",
        reasoning="4-day wait.",
    )
    wait_days, due_at = resolve_deferral_schedule(
        seeded_session,
        po=po,
        triage_result=triage_stub,
        sim_now=sim_now,
    )
    assert wait_days == 4
    assert due_at == sim_now + timedelta(days=4)

    is_safe, _runway, _eta, reasons = evaluate_deferral_safety(
        seeded_session,
        po=po,
        part=part,
        supplier=sup2,
        deferral_days=wait_days,
        sim_now=sim_now,
    )
    assert is_safe is False
    assert len(reasons) >= 1

    rem = LexReminder(
        po_id=po.id,
        part_id=po.part_id,
        supplier_id=po.supplier_id,
        remind_at=due_at,
        reason="Test reminder",
        status=LexReminderStatus.PENDING.value,
        created_at=sim_now,
    )
    seeded_session.add(rem)
    seeded_session.flush()
    cancelled = cancel_pending_lex_reminders_for_po(seeded_session, po.id, reason="GRN logged")
    assert cancelled == 1
    seeded_session.refresh(rem)
    assert rem.status == LexReminderStatus.CANCELLED.value

    adv = evaluate_memory_advisory_alerts(
        seeded_session,
        po=po,
        part=part,
        supplier=sup2,
        trigger_stage="pre_delivery",
        sim_now=sim_now,
    )
    assert adv is not None
    assert adv.type == "LEX_ADVISORY_ALERT"
    assert adv.severity == AlertSeverity.MEDIUM.value

    # Repeat call deduplicates
    assert (
        evaluate_memory_advisory_alerts(
            seeded_session,
            po=po,
            part=part,
            supplier=sup2,
            trigger_stage="pre_delivery",
            sim_now=sim_now,
        )
        is None
    )
    assert isinstance(seeded_session.scalars(select(SupplierMemoryNote)).all(), list)
    assert isinstance(seeded_session.scalars(select(ActionItem)).all(), list)
