import json
from datetime import timedelta
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import select

from backend.agents.lex import (
    EmailDraftResult,
    EmailIntent,
    EmailTriageResult,
    LexAgent,
    ReminderWakeupResult,
)
from backend.agents.llm_client import LLMResult
from backend.agents.tools import ToolRegistry
from backend.core import clock
from backend.core.enums import (
    AlertSeverity,
    CommStatus,
    DispatchDetailsStatus,
    EmailCategory,
    LexReminderStatus,
    PersonaKey,
    POStatus,
)
from backend.core.models import (
    ActionItem,
    AgentTrace,
    LexReminder,
    Part,
    PurchaseOrder,
    Supplier,
    SupplierMemoryNote,
)
from backend.mailroom import OutboundMessage, list_drafts
from backend.orchestrator.lex_tools import (
    LexToolTracker,
    build_lex_tool_registry,
    evaluate_memory_advisory_alerts,
)
from backend.orchestrator.workflows import WorkflowEngine
from backend.po import create_po, transition_po

T = TypeVar("T", bound=BaseModel)


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


class _ToolUsingMockLLM:
    """Tool-capable deterministic mock LLM exercising real LexToolRegistry execution."""

    def __init__(self) -> None:
        self.triage_tool_plan: list[tuple[str, dict[str, Any]]] = []
        self.next_triage: EmailTriageResult | None = None
        self.wakeup_tool_plan: list[tuple[str, dict[str, Any]]] = []
        self.next_wakeup: ReminderWakeupResult | None = None

    def generate_structured(
        self,
        *,
        prompt: str,
        response_schema: type[T],
        system_instruction: str | None = None,
        temperature: float = 0.0,
        pdf_bytes: bytes | None = None,
        image_attachments: list[tuple[str, bytes]] | None = None,
    ) -> LLMResult[T]:
        del system_instruction, temperature, pdf_bytes, image_attachments
        payload = json.loads(prompt)
        if response_schema is EmailDraftResult:
            po_num = payload.get("po_number", "")
            cat = payload["category"]
            recipient = payload["recipient_persona"]
            res = EmailDraftResult(
                to_personas=[recipient],
                cc_personas=[PersonaKey.INVENTORY_MANAGER.value],
                subject=f"[{po_num}] {cat.replace('_', ' ').title()}",
                body=(
                    f"Dear {payload['recipient_name']},\n\n"
                    f"Please share the dispatch details for **{po_num}** "
                    f"(**{payload['part_sku']}**).\n\n"
                    "Best regards,\n**Lex**\nAI Supply Chain Assistant"
                ),
            )
            return LLMResult(
                parsed=res,  # type: ignore[arg-type]
                raw_text=res.model_dump_json(),
                input_tokens=200,
                output_tokens=80,
                latency_ms=10,
                cost_inr=0.003,
                model_name="mock-gemini-v2",
            )
        raise AssertionError(f"Unexpected generate_structured schema {response_schema}")

    def generate_with_tools(
        self,
        *,
        prompt: str,
        response_schema: type[T],
        tool_registry: ToolRegistry,
        system_instruction: str | None = None,
        temperature: float = 0.0,
        pdf_bytes: bytes | None = None,
        image_attachments: list[tuple[str, bytes]] | None = None,
        max_steps: int = 8,
    ) -> LLMResult[T]:
        del system_instruction, temperature, pdf_bytes, image_attachments, max_steps
        payload = json.loads(prompt)
        tool_logs: list[dict[str, Any]] = []

        if response_schema is EmailTriageResult:
            for name, args in self.triage_tool_plan:
                res = tool_registry.execute(name, args)
                tool_logs.append(ToolRegistry.format_trace_record(res, args))
            assert self.next_triage is not None
            return LLMResult(
                parsed=self.next_triage,  # type: ignore[arg-type]
                raw_text=self.next_triage.model_dump_json(),
                input_tokens=300,
                output_tokens=110,
                latency_ms=15,
                cost_inr=0.004,
                model_name="mock-gemini-v2",
                tool_calls=tool_logs,
            )

        if response_schema is ReminderWakeupResult:
            for name, args in self.wakeup_tool_plan:
                res = tool_registry.execute(name, args)
                tool_logs.append(ToolRegistry.format_trace_record(res, args))
            assert self.next_wakeup is not None
            return LLMResult(
                parsed=self.next_wakeup,  # type: ignore[arg-type]
                raw_text=self.next_wakeup.model_dump_json(),
                input_tokens=240,
                output_tokens=70,
                latency_ms=12,
                cost_inr=0.003,
                model_name="mock-gemini-v2",
                tool_calls=tool_logs,
            )

        if response_schema is EmailDraftResult:
            return self.generate_structured(
                prompt=json.dumps(payload),
                response_schema=response_schema,
            )

        raise AssertionError(f"Unexpected generate_with_tools schema {response_schema}")


def test_all_10_lex_tools_execute_and_deduplicate_cleanly(seeded_session) -> None:
    po = _create_open_po(seeded_session, sku="RLY-901", persona=PersonaKey.SUP2)
    tracker = LexToolTracker()
    registry = build_lex_tool_registry(seeded_session, tracker=tracker)

    expected_tools = {
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
    # Second scheduling call for same PO updates rather than duplicating
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

    # Memory deduplication vs genuinely new note + consolidation
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


def test_variable_4d_deferral_schedules_reminder_and_wakes_on_day4(seeded_session) -> None:
    po = _create_open_po(seeded_session, sku="RLY-901", persona=PersonaKey.SUP3, eta_days=14)
    sim_start = clock.now(seeded_session)
    due_4d = sim_start + timedelta(days=4)

    llm = _ToolUsingMockLLM()
    llm.triage_tool_plan = [
        ("get_po_details", {"po_number_or_id": po.po_number}),
        ("get_part_snapshot", {"sku_or_part_id": "RLY-901"}),
        (
            "run_sandbox_calc",
            {
                "python_script": (
                    "result = {'safe': runway > wait_days + 2, "
                    "'margin': round(runway - wait_days, 2)}"
                ),
                "variables": {"runway": 24.0, "wait_days": 4},
            },
        ),
    ]
    llm.next_triage = EmailTriageResult(
        matched_po_number=po.po_number,
        intent=EmailIntent.ACKNOWLEDGED,
        confidence=0.96,
        proposed_eta_date=(sim_start + timedelta(days=14)).date().isoformat(),
        carrier_name="BlueDart",
        details_promised_later=True,
        details_promised_days=4,
        details_promised_by_date=due_4d.date().isoformat(),
        summary_for_manager=f"Confirmed {po.po_number}; tracking promised after 4 days.",
        suggested_action="Schedule 4-day Lex reminder without nagging today.",
        reasoning="Runway 24.0d safely covers 4-day deferral.",
    )
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))

    ack_res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP3,
            to_personas=[PersonaKey.LEX],
            cc_personas=[PersonaKey.INVENTORY_MANAGER],
            subject=f"Re: {po.po_number}",
            body=(
                f"We acknowledged {po.po_number}, but due to system issues we can only "
                "send shipment tracking details after 4 days."
            ),
            po_id=po.id,
        ),
    )

    seeded_session.refresh(po)
    assert po.status == POStatus.ACKNOWLEDGED.value
    assert po.dispatch_details_status == DispatchDetailsStatus.DEFERRED_2D.value
    assert clock.ensure_utc(po.dispatch_details_due_at) == due_4d
    assert ack_res["triage"]["draft_comm_id"] is None

    # Grounded DISPATCH_DETAILS_DEFERRED alert citing 4-day check-in
    deferred_alert = seeded_session.get(ActionItem, ack_res["triage"]["alert_id"])
    assert deferred_alert is not None
    assert deferred_alert.type == "DISPATCH_DETAILS_DEFERRED"
    assert deferred_alert.severity == AlertSeverity.INFO.value
    assert "4-day" in deferred_alert.summary
    assert deferred_alert.cta_payload_json["deferral_days"] == 4

    # Pending LexReminder created for +4d
    rem = seeded_session.scalar(select(LexReminder).where(LexReminder.po_id == po.id))
    assert rem is not None
    assert rem.status == LexReminderStatus.PENDING.value
    assert clock.ensure_utc(rem.remind_at) == due_4d

    # Trace recorded tool_calls_json
    triage_trace = seeded_session.scalar(
        select(AgentTrace)
        .where(AgentTrace.task_type == "email_triage")
        .order_by(AgentTrace.id.desc())
        .limit(1)
    )
    assert triage_trace is not None
    assert [tc["tool"] for tc in triage_trace.tool_calls_json] == [
        "get_po_details",
        "get_part_snapshot",
        "run_sandbox_calc",
    ]

    # Advance +3 days -> reminder MUST NOT fire early (still 1 day before 4-day promise!)
    day3 = engine.advance_clock_and_watch(seeded_session, days=3)
    seeded_session.refresh(rem)
    assert rem.status == LexReminderStatus.PENDING.value
    assert not any(
        e.po_id == po.id and e.event_type == "DISPATCH_DETAILS_NUDGE_DRAFTED"
        for e in day3["sla_events"]
    )

    # Advance +1 more day (+4d total) -> LexReminder wakes Lex up and drafts single follow-up nudge
    llm.wakeup_tool_plan = [
        ("get_po_details", {"po_number_or_id": po.po_number}),
        ("get_part_snapshot", {"sku_or_part_id": "RLY-901"}),
    ]
    llm.next_wakeup = ReminderWakeupResult(
        should_draft_followup=True,
        summary_for_manager=f"4-day reminder triggered for {po.po_number}; tracking still missing.",
        followup_context_note="4-day window elapsed with 0 GRNs and missing tracking.",
        reasoning="Inspected PO and confirmed tracking_number still None.",
    )
    day4 = engine.advance_clock_and_watch(seeded_session, days=1)
    nudge_events = [
        e
        for e in day4["sla_events"]
        if e.po_id == po.id and e.event_type == "DISPATCH_DETAILS_NUDGE_DRAFTED"
    ]
    assert len(nudge_events) == 1  # Never double-fires!

    seeded_session.refresh(rem)
    seeded_session.refresh(po)
    assert rem.status == LexReminderStatus.TRIGGERED.value
    assert rem.triggered_at is not None
    assert po.dispatch_details_status == DispatchDetailsStatus.TIMED_OUT.value

    nudge_alert = seeded_session.get(ActionItem, nudge_events[0].action_item_id)
    assert nudge_alert is not None
    assert nudge_alert.type == "MISSING_DISPATCH_DETAILS_DUE"
    assert "4-day" in nudge_alert.summary
    draft_row = {d.id: d for d in list_drafts(seeded_session, PersonaKey.LEX.value)}[
        nudge_events[0].draft_comm_id
    ]
    assert draft_row.category == EmailCategory.DISPATCH_DETAILS_NUDGE.value
    assert draft_row.status == CommStatus.DRAFT_PENDING_APPROVAL.value

    wakeup_trace = seeded_session.scalar(
        select(AgentTrace)
        .where(AgentTrace.task_type == "reminder_wakeup")
        .order_by(AgentTrace.id.desc())
        .limit(1)
    )
    assert wakeup_trace is not None
    assert [tc["tool"] for tc in wakeup_trace.tool_calls_json] == [
        "get_po_details",
        "get_part_snapshot",
    ]


def test_reminder_wakeup_skips_followup_when_details_arrive_or_grn_logged(seeded_session) -> None:
    po = _create_open_po(seeded_session, sku="RLY-901", persona=PersonaKey.SUP3, eta_days=14)
    sim_start = clock.now(seeded_session)
    due_4d = sim_start + timedelta(days=4)

    llm = _ToolUsingMockLLM()
    llm.next_triage = EmailTriageResult(
        matched_po_number=po.po_number,
        intent=EmailIntent.ACKNOWLEDGED,
        confidence=0.95,
        proposed_eta_date=(sim_start + timedelta(days=14)).date().isoformat(),
        details_promised_later=True,
        details_promised_days=4,
        summary_for_manager=f"Confirmed {po.po_number}; details in 4 days.",
        suggested_action="Schedule 4d reminder.",
        reasoning="Safe runway.",
    )
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP3,
            to_personas=[PersonaKey.LEX],
            subject=f"Re: {po.po_number}",
            body="Confirmed; will send dispatch details after 4 days.",
            po_id=po.id,
        ),
    )

    rem = seeded_session.scalar(select(LexReminder).where(LexReminder.po_id == po.id))
    assert rem is not None and rem.status == LexReminderStatus.PENDING.value

    # Supplier supplies full dispatch details on Day 2 -> cancels pending reminder
    engine.advance_clock_and_watch(seeded_session, days=2)
    llm.next_triage = EmailTriageResult(
        matched_po_number=po.po_number,
        intent=EmailIntent.GENERAL_REPLY,
        confidence=0.96,
        actual_dispatch_date=clock.now(seeded_session).date().isoformat(),
        carrier_name="BlueDart",
        tracking_number="BD-9901",
        summary_for_manager=f"Shipped {po.po_number} via BlueDart BD-9901.",
        suggested_action="None.",
        reasoning="Full logistics received.",
    )
    engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP3,
            to_personas=[PersonaKey.LEX],
            subject=f"Re: {po.po_number}",
            body="Dispatched today via BlueDart tracking BD-9901.",
            po_id=po.id,
        ),
    )
    seeded_session.refresh(rem)
    assert rem.status == LexReminderStatus.CANCELLED.value

    # Also test wakeup guardrail when a pending reminder hits remind_at AFTER GRN logged
    po2 = _create_open_po(
        seeded_session,
        sku="FLT-108",
        persona=PersonaKey.SUP1,
        status=POStatus.ACKNOWLEDGED,
    )
    rem2 = LexReminder(
        po_id=po2.id,
        part_id=po2.part_id,
        supplier_id=po2.supplier_id,
        remind_at=clock.now(seeded_session) + timedelta(days=2),
        reason="Check dispatch details",
        context_json={"kind": "dispatch_details_followup", "deferral_days": 2},
        status=LexReminderStatus.PENDING.value,
        created_at=clock.now(seeded_session),
    )
    seeded_session.add(rem2)
    seeded_session.flush()

    # Log GRN before reminder fires, and leave LLM erroneously wanting to draft
    from backend.po import record_goods_receipt

    record_goods_receipt(seeded_session, po_id=po2.id, received_qty=po2.qty)
    llm.next_wakeup = ReminderWakeupResult(
        should_draft_followup=True,  # Guardrail must block this!
        summary_for_manager="Should be skipped because GRN exists.",
        reasoning="Testing guardrail.",
    )
    day_wake = engine.advance_clock_and_watch(seeded_session, days=2)
    assert not any(
        e.po_id == po2.id and e.event_type == "DISPATCH_DETAILS_NUDGE_DRAFTED"
        for e in day_wake["sla_events"]
    )
    seeded_session.refresh(rem2)
    assert rem2.status == LexReminderStatus.TRIGGERED.value
    assert due_4d is not None


def test_unsafe_4d_deferral_triggers_high_stockout_risk_escalation(seeded_session) -> None:
    part = seeded_session.scalar(select(Part).where(Part.sku == "VLV-204"))
    assert part is not None
    part.on_hand_stock = 10  # ~2.8d runway (< 4 days deferral!)
    seeded_session.flush()

    po = _create_open_po(seeded_session, sku="VLV-204", persona=PersonaKey.SUP2, eta_days=15)
    llm = _ToolUsingMockLLM()
    llm.next_triage = EmailTriageResult(
        matched_po_number=po.po_number,
        intent=EmailIntent.ACKNOWLEDGED,
        confidence=0.95,
        proposed_eta_date=(clock.now(seeded_session) + timedelta(days=15)).date().isoformat(),
        details_promised_later=True,
        details_promised_days=4,
        summary_for_manager=f"Confirmed {po.po_number}, asked to defer tracking by 4 days.",
        suggested_action="Escalate immediately due to imminent stockout.",
        reasoning="4-day wait exceeds remaining stockout runway.",
    )
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP2,
            to_personas=[PersonaKey.LEX],
            subject=f"Re: {po.po_number}",
            body="Confirmed; will send dispatch details after 4 days.",
            po_id=po.id,
        ),
    )

    seeded_session.refresh(po)
    assert po.dispatch_details_status == DispatchDetailsStatus.AWAITING_IMMEDIATE.value
    assert po.dispatch_details_due_at is None
    rem = seeded_session.scalar(
        select(LexReminder).where(
            LexReminder.po_id == po.id,
            LexReminder.status == LexReminderStatus.PENDING.value,
        )
    )
    assert rem is None

    assert res["triage"]["draft_comm_id"] is not None
    alert = seeded_session.get(ActionItem, res["triage"]["alert_id"])
    assert alert is not None
    assert alert.type == "DISPATCH_DEFERRAL_STOCKOUT_RISK"
    assert alert.severity == AlertSeverity.HIGH.value
    assert "4 days" in alert.summary


def test_memory_deduplication_and_contextual_advisory_alerts(seeded_session) -> None:
    sup2 = seeded_session.scalar(select(Supplier).where(Supplier.persona_key == "sup2"))
    sup3 = seeded_session.scalar(select(Supplier).where(Supplier.persona_key == "sup3"))
    assert sup2 is not None and sup3 is not None
    initial_sup2_notes = len(
        seeded_session.scalars(
            select(SupplierMemoryNote).where(SupplierMemoryNote.supplier_id == sup2.id)
        ).all()
    )

    # 1. Duplicate/overlapping memory note via triage must deduplicate (no duplicate DB rows)
    po2 = _create_open_po(seeded_session, sku="BLT-301", persona=PersonaKey.SUP2, eta_days=15)
    llm = _ToolUsingMockLLM()
    llm.next_triage = EmailTriageResult(
        matched_po_number=po2.po_number,
        intent=EmailIntent.ACKNOWLEDGED,
        confidence=0.95,
        proposed_eta_date=(clock.now(seeded_session) + timedelta(days=15)).date().isoformat(),
        promised_dispatch_date=(clock.now(seeded_session) + timedelta(days=5)).date().isoformat(),
        carrier_name="SafeExpress",
        tracking_number="SE-4410",
        new_memory_note=(
            "Chronically delivers 7-10 days past quoted 15d lead time; verify unit prices on PDFs."
        ),
        summary_for_manager=f"Sup2 confirmed {po2.po_number} via SafeExpress SE-4410.",
        suggested_action="Watch invoice pricing.",
        reasoning="Complete ack.",
    )
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP2,
            to_personas=[PersonaKey.LEX],
            subject=f"Re: {po2.po_number}",
            body="Confirmed via SafeExpress SE-4410.",
            po_id=po2.id,
        ),
    )

    sup2_notes_after = seeded_session.scalars(
        select(SupplierMemoryNote).where(SupplierMemoryNote.supplier_id == sup2.id)
    ).all()
    assert len(sup2_notes_after) == initial_sup2_notes

    # Pre-delivery advisory emitted for Sup2's invoice mismatch habit (and deduplicated on repeat)
    adv_alerts = seeded_session.scalars(
        select(ActionItem).where(
            ActionItem.po_id == po2.id,
            ActionItem.type == "LEX_ADVISORY_ALERT",
        )
    ).all()
    assert len(adv_alerts) == 1
    assert "verify invoice pricing" in adv_alerts[0].title.lower()

    part2 = seeded_session.get(Part, po2.part_id)
    assert part2 is not None
    assert (
        evaluate_memory_advisory_alerts(
            seeded_session,
            po=po2,
            part=part2,
            supplier=sup2,
            trigger_stage="pre_delivery",
        )
        is None
    )

    # 2. Ordering a CRITICAL part from Sup3 surfaces Sup3's price-hike / slow-ack advisory
    crit_part = seeded_session.scalar(select(Part).where(Part.sku == "SNS-802"))
    assert crit_part is not None
    withdraw_qty = crit_part.on_hand_stock - (crit_part.rop - 1)
    w_res = engine.withdraw_stock(seeded_session, part_id=crit_part.id, qty=withdraw_qty)
    po3 = w_res["created_po"]
    assert po3 is not None
    crit_advisory = seeded_session.scalar(
        select(ActionItem).where(
            ActionItem.po_id == po3.id,
            ActionItem.type == "LEX_ADVISORY_ALERT",
        )
    )
    assert crit_advisory is not None
    assert crit_advisory.severity == AlertSeverity.MEDIUM.value
    assert "price hikes" in crit_advisory.summary.lower()
