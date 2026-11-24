from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.agents.lex import EmailDraftInput, LexAgent, ReminderWakeupInput
from backend.agents.tools import ToolRegistry
from backend.core import clock
from backend.core.enums import (
    ActionItemStatus,
    AlertSeverity,
    Criticality,
    DispatchDetailsStatus,
    EmailCategory,
    LexReminderStatus,
    PersonaKey,
    POStatus,
)
from backend.core.models import (
    ActionItem,
    GoodsReceipt,
    Invoice,
    LexReminder,
    Part,
    PartSupplierTerm,
    POCommunication,
    PurchaseOrder,
    Supplier,
    SupplierMemoryNote,
)
from backend.mailroom import OutboundMessage, save_draft
from backend.orchestrator.lex_tools import (
    LexToolTracker,
    build_lex_tool_registry,
    evaluate_memory_advisory_alerts,
)
from backend.planning import compute_demand_forecast, refresh_supplier_metrics
from backend.po import compute_missing_dispatch_fields, create_action_item, transition_po

PROTECTED_NOTIFICATION_ALERT_TYPES: tuple[str, ...] = (
    "PO_ACKNOWLEDGED_COMPLETE",
    "DISPATCH_DETAILS_DEFERRED",
    "PO_LOGISTICS_UPDATED",
    "SUPPLIER_MEMORY_LEARNED",
    "MINOR_DELAY_ACCEPTED",
    "INVOICE_MATCHED_CLOSED",
    "SUPPLIER_MESSAGE",
    "PROCUREMENT_ESCALATION",
)


@dataclass
class SLAEvent:
    po_id: int
    po_number: str
    event_type: str
    summary: str
    draft_comm_id: int | None = None
    action_item_id: int | None = None


def _supplier_memory_texts(session: Session, supplier_id: int) -> list[str]:
    rows = session.scalars(
        select(SupplierMemoryNote.note)
        .where(SupplierMemoryNote.supplier_id == supplier_id)
        .order_by(SupplierMemoryNote.id.asc())
    ).all()
    return list(rows)


def _latest_thread_id(session: Session, po_id: int) -> str | None:
    return session.scalar(
        select(POCommunication.thread_id)
        .where(POCommunication.po_id == po_id)
        .order_by(POCommunication.timestamp.desc(), POCommunication.id.desc())
        .limit(1)
    )


def _backup_supplier_payload(session: Session, part_id: int, current_supplier_id: int) -> dict:
    term = session.scalar(
        select(PartSupplierTerm)
        .where(
            PartSupplierTerm.part_id == part_id,
            PartSupplierTerm.supplier_id != current_supplier_id,
        )
        .order_by(PartSupplierTerm.supplier_rank.asc())
        .limit(1)
    )
    if term is None:
        return {}
    sup = session.get(Supplier, term.supplier_id)
    return {
        "backup_supplier_id": term.supplier_id,
        "backup_supplier_name": sup.name if sup else None,
        "backup_unit_price": term.agreed_unit_price,
        "backup_lead_time_days": term.quoted_lead_time_days,
    }


def _term_currency(session: Session, part_id: int, supplier_id: int) -> str:
    term = session.scalar(
        select(PartSupplierTerm).where(
            PartSupplierTerm.part_id == part_id,
            PartSupplierTerm.supplier_id == supplier_id,
        )
    )
    return term.currency if term is not None and term.currency else "INR"


def _draft_lex_email_for_po(
    session: Session,
    lex_agent: LexAgent,
    *,
    po: PurchaseOrder,
    part: Part,
    supplier: Supplier,
    category: EmailCategory,
    context_facts: dict,
    tool_registry: ToolRegistry | None = None,
):
    sim_now = clock.now(session)
    currency = _term_currency(session, part.id, supplier.id)
    draft_result = lex_agent.draft_email(
        session,
        EmailDraftInput(
            category=category,
            po_number=po.po_number,
            part_sku=part.sku,
            part_name=part.name,
            qty=po.qty,
            unit_price=po.unit_price,
            currency=currency,
            recipient_persona=supplier.persona_key,
            recipient_name=supplier.name,
            current_date=sim_now.date().isoformat(),
            supplier_memory_notes=_supplier_memory_texts(session, supplier.id),
            context_facts={"currency": currency, **context_facts},
        ),
        linked_po_id=po.id,
        tool_registry=tool_registry,
    )
    return save_draft(
        session,
        OutboundMessage(
            from_persona=PersonaKey.LEX,
            to_personas=[PersonaKey(p) for p in draft_result.to_personas],
            cc_personas=[PersonaKey(p) for p in draft_result.cc_personas],
            subject=draft_result.subject,
            body=draft_result.body,
            po_id=po.id,
            thread_id=_latest_thread_id(session, po.id),
            category=category,
        ),
    )


def _handle_overdue_po(
    session: Session,
    lex_agent: LexAgent,
    *,
    po: PurchaseOrder,
    part: Part,
    supplier: Supplier,
    effective_eta,
    is_critical: bool,
    days_of_supply: float | None,
    sim_now,
) -> SLAEvent:
    eta_str = effective_eta.date().isoformat()
    transition_po(
        session,
        po,
        POStatus.OVERDUE,
        note=f"No goods receipt recorded by {eta_str} (+1d grace).",
    )
    refresh_supplier_metrics(session, supplier.id, part_id=part.id)
    promised_str = (
        clock.ensure_utc(po.promised_eta).date().isoformat() if po.promised_eta else None
    )
    draft = _draft_lex_email_for_po(
        session,
        lex_agent,
        po=po,
        part=part,
        supplier=supplier,
        category=EmailCategory.OVERDUE_CHASE,
        context_facts={
            "promised_eta": promised_str,
            "effective_eta": eta_str,
            "days_overdue": max(1, (sim_now - effective_eta).days),
        },
    )
    backup = _backup_supplier_payload(session, part.id, supplier.id)
    suggested = [{"action": "approve_draft", "label": "Approve & Send Overdue Chase"}]
    if backup.get("backup_supplier_id"):
        suggested.append(
            {
                "action": "switch_backup_supplier",
                "label": f"Switch to {backup['backup_supplier_name']}",
            }
        )
    alert = create_action_item(
        session,
        type="OVERDUE_PO",
        severity=AlertSeverity.CRITICAL if is_critical else AlertSeverity.HIGH,
        part_criticality=part.criticality,
        days_to_stockout=days_of_supply,
        title=f"OVERDUE shipment: {po.po_number} ({part.sku})",
        summary=(
            f"{po.po_number} from {supplier.name} missed its ETA ({eta_str}) "
            "with no goods received. Lex drafted an overdue chase email."
        ),
        po_id=po.id,
        part_id=part.id,
        draft_comm_id=draft.id,
        suggested_actions=suggested,
        cta_payload={"draft_comm_id": draft.id, "po_id": po.id, **backup},
    )
    return SLAEvent(
        po_id=po.id,
        po_number=po.po_number,
        event_type="OVERDUE_DETECTED",
        summary=alert.title,
        draft_comm_id=draft.id,
        action_item_id=alert.id,
    )


def _process_due_lex_reminders(
    session: Session,
    lex_agent: LexAgent,
    *,
    sim_now,
    tool_registry: ToolRegistry | None,
) -> tuple[list[SLAEvent], set[int]]:
    """Wake Lex on pending LexReminder rows whose remind_at <= sim_now."""
    due_reminders = list(
        session.scalars(
            select(LexReminder)
            .where(
                LexReminder.status == LexReminderStatus.PENDING.value,
                LexReminder.remind_at <= sim_now,
            )
            .order_by(LexReminder.remind_at.asc(), LexReminder.id.asc())
        ).all()
    )
    events: list[SLAEvent] = []
    handled_deferred_po_ids: set[int] = set()
    tool_capable = hasattr(lex_agent.llm, "generate_with_tools")

    for reminder in due_reminders:
        reminder.status = LexReminderStatus.TRIGGERED.value
        reminder.triggered_at = sim_now

        po = session.get(PurchaseOrder, reminder.po_id) if reminder.po_id else None
        part = session.get(Part, po.part_id) if po is not None else (
            session.get(Part, reminder.part_id) if reminder.part_id else None
        )
        supplier = session.get(Supplier, po.supplier_id) if po is not None else (
            session.get(Supplier, reminder.supplier_id) if reminder.supplier_id else None
        )
        if (
            po is None
            or part is None
            or supplier is None
            or po.status in (POStatus.CLOSED.value, POStatus.CANCELLED.value)
        ):
            reminder.context_json = {
                **(reminder.context_json or {}),
                "wakeup_outcome": "po_inactive_or_missing",
            }
            continue

        grn_count = int(
            session.scalar(
                select(func.count(GoodsReceipt.id)).where(GoodsReceipt.po_id == po.id)
            )
            or 0
        )
        missing_fields = list(compute_missing_dispatch_fields(po))
        po.missing_dispatch_fields_json = missing_fields
        ctx = dict(reminder.context_json or {})
        is_dispatch_reminder = (
            ctx.get("kind") == "dispatch_details_followup"
            or po.dispatch_details_status == DispatchDetailsStatus.DEFERRED_2D.value
        )
        if is_dispatch_reminder:
            handled_deferred_po_ids.add(po.id)

        forecast = compute_demand_forecast(session, part, sim_now=sim_now)
        days_of_supply = (
            round(part.on_hand_stock / forecast.effective_daily_demand, 2)
            if forecast.effective_daily_demand > 0
            else None
        )
        thread_id = _latest_thread_id(session, po.id)

        should_draft = bool(
            grn_count == 0
            and missing_fields
            and po.status
            in (
                POStatus.ACKNOWLEDGED.value,
                POStatus.DELAYED_CRITICAL.value,
                POStatus.OVERDUE.value,
            )
        )
        wakeup_note = ""
        if tool_capable:
            wakeup_decision = lex_agent.inspect_reminder_wakeup(
                session,
                ReminderWakeupInput(
                    reminder_id=reminder.id,
                    po_id=po.id,
                    po_number=po.po_number,
                    part_sku=part.sku,
                    part_name=part.name,
                    supplier_name=supplier.name,
                    current_date=sim_now.date().isoformat(),
                    remind_at=clock.ensure_utc(reminder.remind_at).isoformat(),
                    reminder_reason=reminder.reason,
                    suggested_action=str(ctx.get("suggested_action") or ""),
                    po_status=po.status,
                    grn_count=grn_count,
                    missing_dispatch_fields=missing_fields,
                    days_to_stockout=days_of_supply,
                    thread_id=thread_id,
                ),
                tool_registry=tool_registry,
                linked_po_id=po.id,
            )
            should_draft = bool(wakeup_decision.should_draft_followup and should_draft)
            wakeup_note = (
                wakeup_decision.followup_context_note or wakeup_decision.summary_for_manager
            )

        if not missing_fields or grn_count > 0:
            if (
                not missing_fields
                and po.dispatch_details_status == DispatchDetailsStatus.DEFERRED_2D.value
            ):
                po.dispatch_details_status = DispatchDetailsStatus.COMPLETE.value
                po.dispatch_details_due_at = None
            auto_resolve_po_alerts(
                session,
                po.id,
                ("DISPATCH_DETAILS_DEFERRED", "MISSING_DISPATCH_DETAILS_DUE"),
            )
            reminder.context_json = {
                **ctx,
                "wakeup_outcome": "no_followup_needed",
                "wakeup_note": wakeup_note,
            }
            events.append(
                SLAEvent(
                    po_id=po.id,
                    po_number=po.po_number,
                    event_type="LEX_REMINDER_RESOLVED_NOOP",
                    summary=(
                        wakeup_note
                        or f"Reminder #{reminder.id} checked {po.po_number}; no follow-up needed."
                    ),
                )
            )
            continue

        if should_draft:
            deferral_days = int(
                ctx.get("deferral_days")
                or max(
                    1,
                    round(
                        (
                            clock.ensure_utc(reminder.remind_at)
                            - clock.ensure_utc(reminder.created_at)
                        ).total_seconds()
                        / 86400.0
                    ),
                )
            )
            due_date_str = clock.ensure_utc(reminder.remind_at).date().isoformat()
            po.dispatch_details_status = DispatchDetailsStatus.TIMED_OUT.value
            draft = _draft_lex_email_for_po(
                session,
                lex_agent,
                po=po,
                part=part,
                supplier=supplier,
                category=EmailCategory.DISPATCH_DETAILS_NUDGE,
                context_facts={
                    "missing_fields": missing_fields,
                    "due_date": due_date_str,
                    "deferral_days": deferral_days,
                    "wakeup_note": wakeup_note,
                    "promised_dispatch_date": (
                        clock.ensure_utc(po.promised_dispatch_date).date().isoformat()
                        if po.promised_dispatch_date
                        else None
                    ),
                    "carrier_name": po.carrier_name,
                    "tracking_number": po.tracking_number,
                },
                tool_registry=tool_registry,
            )
            alert = create_action_item(
                session,
                type="MISSING_DISPATCH_DETAILS_DUE",
                severity=AlertSeverity.HIGH,
                part_criticality=part.criticality,
                days_to_stockout=days_of_supply,
                title=(
                    f"Dispatch details overdue ({deferral_days}-day wait expired): "
                    f"{po.po_number} ({supplier.name})"
                ),
                summary=(
                    f"The {deferral_days}-day wait window for {supplier.name} to share missing "
                    f"dispatch details ({', '.join(missing_fields)}) on {po.po_number} "
                    f"expired on {due_date_str}. Lex drafted a follow-up nudge email."
                ),
                po_id=po.id,
                part_id=part.id,
                draft_comm_id=draft.id,
                suggested_actions=[
                    {"action": "approve_draft", "label": "Approve & Send Nudge"},
                    {"action": "reject_draft", "label": "Reject Draft"},
                ],
                cta_payload={
                    "draft_comm_id": draft.id,
                    "po_id": po.id,
                    "reminder_id": reminder.id,
                    "deferral_days": deferral_days,
                    "missing_fields": missing_fields,
                    "due_date": due_date_str,
                },
            )
            reminder.context_json = {
                **ctx,
                "wakeup_outcome": "followup_drafted",
                "draft_comm_id": draft.id,
                "action_item_id": alert.id,
            }
            events.append(
                SLAEvent(
                    po_id=po.id,
                    po_number=po.po_number,
                    event_type="DISPATCH_DETAILS_NUDGE_DRAFTED",
                    summary=alert.title,
                    draft_comm_id=draft.id,
                    action_item_id=alert.id,
                )
            )
        else:
            reminder.context_json = {
                **ctx,
                "wakeup_outcome": "triggered_no_draft",
                "wakeup_note": wakeup_note,
            }
            events.append(
                SLAEvent(
                    po_id=po.id,
                    po_number=po.po_number,
                    event_type="LEX_REMINDER_TRIGGERED",
                    summary=wakeup_note or f"Reminder #{reminder.id} triggered for {po.po_number}.",
                )
            )

    return events, handled_deferred_po_ids


def run_sla_watcher(session: Session, lex_agent: LexAgent) -> list[SLAEvent]:
    """Synchronously evaluate due LexReminders and all active PO SLAs."""
    sim_now = clock.now(session)
    tool_registry = (
        build_lex_tool_registry(session, tracker=LexToolTracker())
        if hasattr(lex_agent.llm, "generate_with_tools")
        else None
    )
    events, handled_deferred_po_ids = _process_due_lex_reminders(
        session,
        lex_agent,
        sim_now=sim_now,
        tool_registry=tool_registry,
    )

    active_statuses = (
        POStatus.SENT_AWAITING_ACK.value,
        POStatus.UNRESPONSIVE_ESCALATED.value,
        POStatus.ACKNOWLEDGED.value,
        POStatus.DELAYED_CRITICAL.value,
        POStatus.OVERDUE.value,
        POStatus.QUARANTINED_INVOICE_MISMATCH.value,
    )
    pos = session.scalars(
        select(PurchaseOrder)
        .where(PurchaseOrder.status.in_(active_statuses))
        .order_by(PurchaseOrder.id.asc())
    ).all()

    for po in pos:
        part = session.get(Part, po.part_id)
        supplier = session.get(Supplier, po.supplier_id)
        if part is None or supplier is None:
            continue

        is_critical = part.criticality == Criticality.CRITICAL.value
        forecast = compute_demand_forecast(session, part, sim_now=sim_now)
        days_of_supply = (
            round(part.on_hand_stock / forecast.effective_daily_demand, 2)
            if forecast.effective_daily_demand > 0
            else None
        )

        # 1. Ack SLA (SENT_AWAITING_ACK): 24h for CRITICAL, 48h for STANDARD/LOW
        # Also check OVERDUE first if effective_eta + 1d has passed with no GRN.
        if po.status == POStatus.SENT_AWAITING_ACK.value:
            effective_eta_raw = po.revised_eta or po.promised_eta
            if effective_eta_raw is not None:
                effective_eta = clock.ensure_utc(effective_eta_raw)
                grn_count = (
                    session.scalar(
                        select(func.count(GoodsReceipt.id)).where(GoodsReceipt.po_id == po.id)
                    )
                    or 0
                )
                if grn_count == 0 and sim_now >= effective_eta + timedelta(days=1):
                    events.append(
                        _handle_overdue_po(
                            session,
                            lex_agent,
                            po=po,
                            part=part,
                            supplier=supplier,
                            effective_eta=effective_eta,
                            is_critical=is_critical,
                            days_of_supply=days_of_supply,
                            sim_now=sim_now,
                        )
                    )
                    continue

            sla_delta = timedelta(hours=24 if is_critical else 48)
            anchor = (
                clock.ensure_utc(po.next_sla_check_at)
                if po.next_sla_check_at is not None
                else clock.ensure_utc(po.sent_at or po.created_at) + sla_delta
            )
            if sim_now >= anchor:
                if po.follow_up_count < 2:
                    po.follow_up_count += 1
                    po.next_sla_check_at = sim_now + sla_delta
                    refresh_supplier_metrics(session, supplier.id, part_id=part.id)
                    sent_date = clock.ensure_utc(po.sent_at or po.created_at).date().isoformat()
                    draft = _draft_lex_email_for_po(
                        session,
                        lex_agent,
                        po=po,
                        part=part,
                        supplier=supplier,
                        category=EmailCategory.ACK_REMINDER,
                        context_facts={
                            "follow_up_number": po.follow_up_count,
                            "sent_at": sent_date,
                            "sla_hours": 24 if is_critical else 48,
                        },
                    )
                    title = (
                        f"No ack on {po.po_number} — "
                        f"Follow-up #{po.follow_up_count} draft ready"
                    )
                    alert = create_action_item(
                        session,
                        type="ACK_REMINDER_DRAFT",
                        severity=AlertSeverity.HIGH if is_critical else AlertSeverity.MEDIUM,
                        part_criticality=part.criticality,
                        days_to_stockout=days_of_supply,
                        title=title,
                        summary=(
                            f"{supplier.name} has not acknowledged {po.po_number} "
                            f"({part.sku}, {po.qty} units). "
                            f"Lex drafted follow-up #{po.follow_up_count} for your approval."
                        ),
                        po_id=po.id,
                        part_id=part.id,
                        draft_comm_id=draft.id,
                        suggested_actions=[
                            {"action": "approve_draft", "label": "Approve & Send Reminder"},
                            {"action": "reject_draft", "label": "Reject Draft"},
                        ],
                        cta_payload={"draft_comm_id": draft.id, "po_id": po.id},
                    )
                    events.append(
                        SLAEvent(
                            po_id=po.id,
                            po_number=po.po_number,
                            event_type="ACK_REMINDER_DRAFTED",
                            summary=alert.title,
                            draft_comm_id=draft.id,
                            action_item_id=alert.id,
                        )
                    )
                else:
                    transition_po(
                        session,
                        po,
                        POStatus.UNRESPONSIVE_ESCALATED,
                        note="Escalated after 2 unanswered acknowledgement follow-ups.",
                    )
                    po.next_sla_check_at = None
                    refresh_supplier_metrics(session, supplier.id, part_id=part.id)
                    backup = _backup_supplier_payload(session, part.id, supplier.id)
                    suggested = []
                    if backup.get("backup_supplier_id"):
                        suggested.append(
                            {
                                "action": "switch_backup_supplier",
                                "label": f"Switch to {backup['backup_supplier_name']}",
                            }
                        )
                    suggested.append({"action": "add_note", "label": "Log Escalation Note"})

                    alert = create_action_item(
                        session,
                        type="UNRESPONSIVE_SUPPLIER",
                        severity=AlertSeverity.CRITICAL if is_critical else AlertSeverity.HIGH,
                        part_criticality=part.criticality,
                        days_to_stockout=days_of_supply,
                        title=f"Unresponsive supplier on {po.po_number} ({supplier.name})",
                        summary=(
                            f"{supplier.name} failed to respond after 2 follow-ups for "
                            f"{po.po_number} ({part.sku}). Consider switching to a backup supplier."
                        ),
                        po_id=po.id,
                        part_id=part.id,
                        suggested_actions=suggested,
                        cta_payload={"po_id": po.id, **backup},
                    )
                    events.append(
                        SLAEvent(
                            po_id=po.id,
                            po_number=po.po_number,
                            event_type="UNRESPONSIVE_ESCALATED",
                            summary=alert.title,
                            action_item_id=alert.id,
                        )
                    )
            continue

        # 1b. Unresponsive Escalated Overdue Detection
        if po.status == POStatus.UNRESPONSIVE_ESCALATED.value:
            effective_eta_raw = po.revised_eta or po.promised_eta
            if effective_eta_raw is not None:
                effective_eta = clock.ensure_utc(effective_eta_raw)
                grn_count = (
                    session.scalar(
                        select(func.count(GoodsReceipt.id)).where(GoodsReceipt.po_id == po.id)
                    )
                    or 0
                )
                if grn_count == 0 and sim_now >= effective_eta + timedelta(days=1):
                    events.append(
                        _handle_overdue_po(
                            session,
                            lex_agent,
                            po=po,
                            part=part,
                            supplier=supplier,
                            effective_eta=effective_eta,
                            is_critical=is_critical,
                            days_of_supply=days_of_supply,
                            sim_now=sim_now,
                        )
                    )
            continue

        # 2 & 3. Deferred Dispatch Details SLA, Pre-ETA Check-in & Overdue Detection
        if po.status in (
            POStatus.ACKNOWLEDGED.value,
            POStatus.DELAYED_CRITICAL.value,
            POStatus.OVERDUE.value,
        ):
            grn_count = (
                session.scalar(
                    select(func.count(GoodsReceipt.id)).where(GoodsReceipt.po_id == po.id)
                )
                or 0
            )

            # Deferred Dispatch Details Expiry -> TIMED_OUT + DISPATCH_DETAILS_NUDGE
            if (
                po.id not in handled_deferred_po_ids
                and grn_count == 0
                and po.dispatch_details_status == DispatchDetailsStatus.DEFERRED_2D.value
                and po.dispatch_details_due_at is not None
                and sim_now >= clock.ensure_utc(po.dispatch_details_due_at)
            ):
                missing_fields = list(
                    po.missing_dispatch_fields_json or compute_missing_dispatch_fields(po)
                )
                if not missing_fields:
                    po.dispatch_details_status = DispatchDetailsStatus.COMPLETE.value
                    po.dispatch_details_due_at = None
                else:
                    due_utc = clock.ensure_utc(po.dispatch_details_due_at)
                    due_date_str = due_utc.date().isoformat()
                    anchor_ref = po.acknowledged_at or po.sent_at or po.created_at
                    wait_days = max(
                        1,
                        round(
                            (due_utc - clock.ensure_utc(anchor_ref)).total_seconds() / 86400.0
                        ),
                    )
                    po.dispatch_details_status = DispatchDetailsStatus.TIMED_OUT.value
                    draft = _draft_lex_email_for_po(
                        session,
                        lex_agent,
                        po=po,
                        part=part,
                        supplier=supplier,
                        category=EmailCategory.DISPATCH_DETAILS_NUDGE,
                        context_facts={
                            "missing_fields": missing_fields,
                            "due_date": due_date_str,
                            "deferral_days": wait_days,
                            "promised_dispatch_date": (
                                clock.ensure_utc(po.promised_dispatch_date).date().isoformat()
                                if po.promised_dispatch_date
                                else None
                            ),
                            "carrier_name": po.carrier_name,
                            "tracking_number": po.tracking_number,
                        },
                        tool_registry=tool_registry,
                    )
                    alert = create_action_item(
                        session,
                        type="MISSING_DISPATCH_DETAILS_DUE",
                        severity=AlertSeverity.HIGH,
                        part_criticality=part.criticality,
                        days_to_stockout=days_of_supply,
                        title=(
                            f"Dispatch details overdue ({wait_days}-day wait expired): "
                            f"{po.po_number} ({supplier.name})"
                        ),
                        summary=(
                            f"The {wait_days}-day wait window for {supplier.name} to share missing "
                            f"dispatch details ({', '.join(missing_fields)}) on {po.po_number} "
                            f"expired on {due_date_str}. Lex drafted a follow-up nudge email."
                        ),
                        po_id=po.id,
                        part_id=part.id,
                        draft_comm_id=draft.id,
                        suggested_actions=[
                            {"action": "approve_draft", "label": "Approve & Send Nudge"},
                            {"action": "reject_draft", "label": "Reject Draft"},
                        ],
                        cta_payload={
                            "draft_comm_id": draft.id,
                            "po_id": po.id,
                            "deferral_days": wait_days,
                            "missing_fields": missing_fields,
                            "due_date": due_date_str,
                        },
                    )
                    events.append(
                        SLAEvent(
                            po_id=po.id,
                            po_number=po.po_number,
                            event_type="DISPATCH_DETAILS_NUDGE_DRAFTED",
                            summary=alert.title,
                            draft_comm_id=draft.id,
                            action_item_id=alert.id,
                        )
                    )

            if po.status not in (POStatus.ACKNOWLEDGED.value, POStatus.DELAYED_CRITICAL.value):
                continue

            effective_eta_raw = po.revised_eta or po.promised_eta
            if effective_eta_raw is None:
                continue
            effective_eta = clock.ensure_utc(effective_eta_raw)

            # Overdue Detection: sim_now >= effective_eta + 1 day with no GRN
            if grn_count == 0 and sim_now >= effective_eta + timedelta(days=1):
                events.append(
                    _handle_overdue_po(
                        session,
                        lex_agent,
                        po=po,
                        part=part,
                        supplier=supplier,
                        effective_eta=effective_eta,
                        is_critical=is_critical,
                        days_of_supply=days_of_supply,
                        sim_now=sim_now,
                    )
                )
                continue

            # Pre-ETA Check-in: 3 days before ETA for CRITICAL parts or low-reliability suppliers
            if (
                po.status == POStatus.ACKNOWLEDGED.value
                and not po.pre_eta_checked
                and (is_critical or supplier.reliability_score < 75.0)
                and sim_now >= effective_eta - timedelta(days=3)
            ):
                po.pre_eta_checked = True
                eta_str = effective_eta.date().isoformat()
                evaluate_memory_advisory_alerts(
                    session,
                    po=po,
                    part=part,
                    supplier=supplier,
                    trigger_stage="pre_delivery",
                )
                draft = _draft_lex_email_for_po(
                    session,
                    lex_agent,
                    po=po,
                    part=part,
                    supplier=supplier,
                    category=EmailCategory.PRE_ETA_CHECKIN,
                    context_facts={
                        "effective_eta": eta_str,
                        "supplier_reliability_score": supplier.reliability_score,
                        "part_criticality": part.criticality,
                    },
                    tool_registry=tool_registry,
                )
                alert = create_action_item(
                    session,
                    type="PRE_ETA_CHECKIN_DRAFT",
                    severity=AlertSeverity.MEDIUM,
                    part_criticality=part.criticality,
                    days_to_stockout=days_of_supply,
                    title=f"Pre-ETA check-in draft for {po.po_number} ({supplier.name})",
                    summary=(
                        f"{po.po_number} is due on {eta_str}. Because of part criticality / "
                        "supplier history, Lex drafted a proactive dispatch check-in email."
                    ),
                    po_id=po.id,
                    part_id=part.id,
                    draft_comm_id=draft.id,
                    suggested_actions=[
                        {"action": "approve_draft", "label": "Approve & Send Check-In"},
                        {"action": "reject_draft", "label": "Skip Check-In"},
                    ],
                    cta_payload={"draft_comm_id": draft.id, "po_id": po.id},
                )
                events.append(
                    SLAEvent(
                        po_id=po.id,
                        po_number=po.po_number,
                        event_type="PRE_ETA_CHECKIN_DRAFTED",
                        summary=alert.title,
                        draft_comm_id=draft.id,
                        action_item_id=alert.id,
                    )
                )
            continue

        # 4. Quarantine SLA (QUARANTINED_INVOICE_MISMATCH): 48h reminder, 96h critical escalation
        if po.status == POStatus.QUARANTINED_INVOICE_MISMATCH.value:
            latest_inv = session.scalar(
                select(Invoice)
                .where(Invoice.po_id == po.id)
                .order_by(Invoice.received_at.desc(), Invoice.id.desc())
                .limit(1)
            )
            latest_grn = session.scalar(
                select(GoodsReceipt)
                .where(GoodsReceipt.po_id == po.id)
                .order_by(GoodsReceipt.received_at.desc(), GoodsReceipt.id.desc())
                .limit(1)
            )
            candidate_anchors = [
                clock.ensure_utc(ts)
                for ts in (
                    latest_inv.received_at if latest_inv else None,
                    latest_grn.received_at if latest_grn else None,
                )
                if ts is not None
            ]
            anchor = (
                max(candidate_anchors)
                if candidate_anchors
                else clock.ensure_utc(po.sent_at or po.created_at)
            )
            elapsed_hours = (sim_now - anchor).total_seconds() / 3600.0

            if elapsed_hours >= 96.0:
                existing_crit = session.scalar(
                    select(ActionItem).where(
                        ActionItem.po_id == po.id,
                        ActionItem.type == "QUARANTINE_ESCALATED",
                        ActionItem.created_at >= anchor,
                    )
                )
                if existing_crit is None:
                    alert = create_action_item(
                        session,
                        type="QUARANTINE_ESCALATED",
                        severity=AlertSeverity.CRITICAL,
                        part_criticality=part.criticality,
                        days_to_stockout=days_of_supply,
                        title=f"CRITICAL: {po.po_number} quarantined >96h (invoice mismatch)",
                        summary=(
                            f"Stock from {po.po_number} ({part.sku}) has remained quarantined "
                            f"for {int(elapsed_hours)}h due to an unresolved invoice mismatch "
                            f"with {supplier.name}."
                        ),
                        po_id=po.id,
                        part_id=part.id,
                        assigned_team="InventoryManager",
                        suggested_actions=[{"action": "add_note", "label": "Log Escalation Note"}],
                        cta_payload={"po_id": po.id},
                    )
                    events.append(
                        SLAEvent(
                            po_id=po.id,
                            po_number=po.po_number,
                            event_type="QUARANTINE_ESCALATED",
                            summary=alert.title,
                            action_item_id=alert.id,
                        )
                    )
            elif elapsed_hours >= 48.0:
                existing_rem = session.scalar(
                    select(ActionItem).where(
                        ActionItem.po_id == po.id,
                        ActionItem.type == "QUARANTINE_REMINDER",
                        ActionItem.created_at >= anchor,
                    )
                )
                if existing_rem is None:
                    reason = (
                        latest_inv.discrepancy_summary
                        if latest_inv and latest_inv.discrepancy_summary
                        else "Invoice mismatch"
                    )
                    draft = _draft_lex_email_for_po(
                        session,
                        lex_agent,
                        po=po,
                        part=part,
                        supplier=supplier,
                        category=EmailCategory.INVOICE_DISCREPANCY,
                        context_facts={
                            "invoice_number": latest_inv.invoice_number if latest_inv else None,
                            "mismatch_reason": reason,
                            "hours_quarantined": int(elapsed_hours),
                        },
                    )
                    alert = create_action_item(
                        session,
                        type="QUARANTINE_REMINDER",
                        severity=AlertSeverity.HIGH,
                        part_criticality=part.criticality,
                        days_to_stockout=days_of_supply,
                        title=f"Quarantine reminder draft for {po.po_number} (>48h)",
                        summary=(
                            f"Corrected invoice for {po.po_number} has not arrived after 48h. "
                            "Lex drafted a follow-up requesting the corrected PDF invoice."
                        ),
                        po_id=po.id,
                        part_id=part.id,
                        draft_comm_id=draft.id,
                        suggested_actions=[
                            {"action": "approve_draft", "label": "Approve & Send Reminder"},
                            {"action": "reject_draft", "label": "Reject Draft"},
                        ],
                        cta_payload={"draft_comm_id": draft.id, "po_id": po.id},
                    )
                    events.append(
                        SLAEvent(
                            po_id=po.id,
                            po_number=po.po_number,
                            event_type="QUARANTINE_REMINDER_DRAFTED",
                            summary=alert.title,
                            draft_comm_id=draft.id,
                            action_item_id=alert.id,
                        )
                    )

    session.flush()
    return events


def auto_resolve_po_alerts(
    session: Session,
    po_id: int,
    alert_types: tuple[str, ...] | None = None,
    *,
    include_notifications: bool = False,
) -> int:
    """Auto-resolve open actionable alerts for a PO while protecting Lex notifications."""
    sim_now = clock.now(session)
    stmt = select(ActionItem).where(
        ActionItem.po_id == po_id,
        ActionItem.status == ActionItemStatus.OPEN.value,
    )
    if alert_types is not None:
        stmt = stmt.where(ActionItem.type.in_(alert_types))
    elif not include_notifications:
        stmt = stmt.where(ActionItem.type.notin_(PROTECTED_NOTIFICATION_ALERT_TYPES))
    rows = session.scalars(stmt).all()
    for row in rows:
        row.status = ActionItemStatus.RESOLVED.value
        row.resolved_at = sim_now
    if rows:
        session.flush()
    return len(rows)
