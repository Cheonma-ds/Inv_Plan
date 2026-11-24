from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.agents.lex import EmailTriageResult
from backend.core import clock
from backend.core.enums import (
    AlertSeverity,
    Criticality,
    DispatchDetailsStatus,
    EmailCategory,
    POStatus,
)
from backend.core.models import (
    ActionItem,
    LexReminder,
    Part,
    PartSupplierTerm,
    PurchaseOrder,
    Supplier,
    SupplierMemoryNote,
)
from backend.mailroom import StoredEmail
from backend.orchestrator.lex_tools import (
    LexToolTracker,
    cancel_pending_lex_reminders_for_po,
    evaluate_memory_advisory_alerts,
    parse_iso_date_utc,
    schedule_po_lex_reminder,
)
from backend.orchestrator.sla_watcher import auto_resolve_po_alerts
from backend.planning import compute_part_planning_metrics
from backend.po import append_po_note, apply_po_logistics_updates, create_action_item

if TYPE_CHECKING:
    from backend.orchestrator.workflows import WorkflowEngine

FIELD_LABELS: dict[str, str] = {
    "promised_dispatch_date": "Promised Dispatch Date",
    "actual_dispatch_date": "Actual Dispatch Date",
    "carrier_name": "Carrier",
    "tracking_number": "Tracking Number",
}


def has_logistics_payload(triage_result: EmailTriageResult) -> bool:
    """Return True if triage extracted any dispatch/carrier/tracking or deferral signal."""
    return any(
        (
            bool(triage_result.promised_dispatch_date),
            bool(triage_result.actual_dispatch_date),
            bool(triage_result.carrier_name and triage_result.carrier_name.strip()),
            bool(triage_result.tracking_number and triage_result.tracking_number.strip()),
            bool(triage_result.details_promised_later),
        )
    )


def _format_changed_fields(changed_fields: dict[str, tuple[Any, Any]]) -> str:
    if not changed_fields:
        return "none"
    parts: list[str] = []
    for key, (old_val, new_val) in changed_fields.items():
        label = FIELD_LABELS.get(key, key)
        if old_val is not None:
            parts.append(f"{label}: {old_val} → {new_val}")
        else:
            parts.append(f"{label}: {new_val}")
    return ", ".join(parts)


def _format_missing_fields(missing_fields: list[str]) -> str:
    return ", ".join(FIELD_LABELS.get(f, f) for f in missing_fields)


def _serialize_changed_fields(
    changed_fields: dict[str, tuple[Any, Any]],
) -> dict[str, dict[str, Any]]:
    return {k: {"old": old_v, "new": new_v} for k, (old_v, new_v) in changed_fields.items()}


def _backup_supplier_payload(
    session: Session, part_id: int, current_supplier_id: int
) -> dict[str, Any]:
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


def evaluate_deferral_safety(
    session: Session,
    *,
    po: PurchaseOrder,
    part: Part,
    supplier: Supplier,
    deferral_days: int,
    sim_now: datetime,
) -> tuple[bool, float | None, float | None, list[str]]:
    """Determine whether waiting ``deferral_days`` for dispatch details is safe."""
    metrics = compute_part_planning_metrics(session, part, sim_now=sim_now)
    days_to_stockout = metrics.days_to_stockout

    eta_raw = po.revised_eta or po.promised_eta
    days_until_eta = (
        max(0.0, (clock.ensure_utc(eta_raw) - sim_now).total_seconds() / 86400.0)
        if eta_raw is not None
        else None
    )

    is_critical = part.criticality == Criticality.CRITICAL.value
    buffer_days = 3.0 if (is_critical or supplier.reliability_score < 60.0) else 2.0
    reasons: list[str] = []

    if part.on_hand_stock <= 0:
        reasons.append(f"{part.sku} is already stocked out (0 units on hand)")
    elif days_to_stockout is not None:
        if days_to_stockout <= float(deferral_days):
            reasons.append(
                f"projected stockout in {days_to_stockout:.1f}d precedes the "
                f"{deferral_days}-day deferral window"
            )
        elif days_to_stockout <= float(deferral_days) + buffer_days:
            reasons.append(
                f"stockout runway ({days_to_stockout:.1f}d) leaves insufficient safety margin "
                f"beyond the {deferral_days}-day wait (requires "
                f">{deferral_days + buffer_days:.1f}d for {part.criticality} / "
                f"supplier reliability {supplier.reliability_score:.0f})"
            )
        elif (
            days_until_eta is not None
            and days_to_stockout < days_until_eta
            and (is_critical or supplier.reliability_score < 65.0)
        ):
            reasons.append(
                f"projected stockout ({days_to_stockout:.1f}d) occurs before PO ETA "
                f"({days_until_eta:.1f}d) with supplier reliability "
                f"{supplier.reliability_score:.0f}"
            )

    if days_until_eta is not None and float(deferral_days) >= days_until_eta:
        reasons.append(
            f"promised details wait (+{deferral_days}d) falls on or after PO ETA "
            f"({days_until_eta:.1f}d)"
        )

    return len(reasons) == 0, days_to_stockout, days_until_eta, reasons


def resolve_deferral_schedule(
    session: Session,
    *,
    po: PurchaseOrder,
    triage_result: EmailTriageResult,
    sim_now: datetime,
    tracker: LexToolTracker | None = None,
) -> tuple[int, datetime]:
    """Resolve variable deferral days & reminder UTC timestamp (fallback: 2 days)."""
    if tracker is not None and po.id in tracker.scheduled_reminders_by_po:
        rem = session.get(LexReminder, tracker.scheduled_reminders_by_po[po.id])
        if rem is not None:
            rem_dt = clock.ensure_utc(rem.remind_at)
            days = max(1, round((rem_dt - sim_now).total_seconds() / 86400.0))
            return days, rem_dt

    if triage_result.details_promised_days is not None and triage_result.details_promised_days > 0:
        days = int(triage_result.details_promised_days)
        return days, sim_now + timedelta(days=days)

    if triage_result.details_promised_by_date:
        parsed_dt = parse_iso_date_utc(triage_result.details_promised_by_date)
        if parsed_dt is not None and parsed_dt > sim_now:
            days = max(1, round((parsed_dt - sim_now).total_seconds() / 86400.0))
            return days, parsed_dt

    return 2, sim_now + timedelta(days=2)


def create_supplier_memory_alert(
    session: Session,
    *,
    supplier: Supplier,
    note: SupplierMemoryNote,
    po: PurchaseOrder | None,
    comm_id: int,
    reasoning: str = "",
) -> ActionItem:
    """Notify InventoryManager whenever Lex autonomously stores a supplier memory note."""
    po_ref = f" ({po.po_number})" if po is not None else ""
    part = session.get(Part, po.part_id) if po is not None else None
    risk_keywords = ("late", "delay", "mismatch", "price", "hike", "slow", "damage", "sla")
    has_risk_signal = any(k in note.note.lower() for k in risk_keywords)
    severity = (
        AlertSeverity.MEDIUM
        if (has_risk_signal and part is not None and part.criticality == Criticality.CRITICAL.value)
        else AlertSeverity.INFO
    )
    context_clause = (
        f" [Supplier reliability: {supplier.reliability_score:.1f}/100, "
        f"OTD: {supplier.otd_rate:.0%}, Invoice accuracy: {supplier.invoice_accuracy_rate:.0%}]"
    )
    reason_clause = f" Rationale: {reasoning.strip()}." if reasoning.strip() else ""
    return create_action_item(
        session,
        type="SUPPLIER_MEMORY_LEARNED",
        severity=severity,
        part_criticality=part.criticality if part is not None else None,
        title=f"Lex saved supplier memory note for {supplier.name}{po_ref}",
        summary=f'Learned from email #{comm_id}: "{note.note}".{reason_clause}{context_clause}',
        po_id=po.id if po is not None else None,
        part_id=po.part_id if po is not None else None,
        suggested_actions=[],
        cta_payload={
            "supplier_id": supplier.id,
            "memory_note_id": note.id,
            "note": note.note,
            "po_id": po.id if po is not None else None,
            "comm_id": comm_id,
            "reliability_score": supplier.reliability_score,
        },
    )


def _draft_immediate_dispatch_request(
    session: Session,
    engine: "WorkflowEngine",
    *,
    po: PurchaseOrder,
    part: Part,
    supplier: Supplier,
    email: StoredEmail,
    promised_eta_str: str,
    missing_fields: list[str],
    extra_context: dict[str, Any] | None = None,
    required_cc_personas: list[str] | None = None,
) -> StoredEmail:
    return engine._create_lex_draft(
        session,
        po=po,
        part=part,
        supplier=supplier,
        category=EmailCategory.DISPATCH_DETAILS_REQUEST,
        thread_id=email.thread_id,
        thread_subject=email.subject,
        required_cc_personas=required_cc_personas,
        context_facts={
            "promised_eta": promised_eta_str,
            "missing_fields": missing_fields,
            "promised_dispatch_date": (
                clock.ensure_utc(po.promised_dispatch_date).date().isoformat()
                if po.promised_dispatch_date
                else None
            ),
            "actual_dispatch_date": (
                clock.ensure_utc(po.actual_dispatch_date).date().isoformat()
                if po.actual_dispatch_date
                else None
            ),
            "carrier_name": po.carrier_name,
            "tracking_number": po.tracking_number,
            **(extra_context or {}),
        },
    )


def handle_po_acknowledgment_logistics(
    session: Session,
    engine: "WorkflowEngine",
    *,
    po: PurchaseOrder,
    part: Part,
    supplier: Supplier,
    email: StoredEmail,
    triage_result: EmailTriageResult,
    tracker: LexToolTracker | None = None,
) -> dict[str, Any]:
    """Process logistics fields on PO acknowledgment with dynamic deferral & safety checks."""
    sim_now = clock.now(session)

    deferral_days = 2
    remind_at = sim_now + timedelta(days=2)
    is_safe = True
    days_to_stockout: float | None = None
    days_until_eta: float | None = None
    unsafe_reasons: list[str] = []

    if triage_result.details_promised_later:
        deferral_days, remind_at = resolve_deferral_schedule(
            session,
            po=po,
            triage_result=triage_result,
            sim_now=sim_now,
            tracker=tracker,
        )
        is_safe, days_to_stockout, days_until_eta, unsafe_reasons = evaluate_deferral_safety(
            session,
            po=po,
            part=part,
            supplier=supplier,
            deferral_days=deferral_days,
            sim_now=sim_now,
        )

    logistics_res = apply_po_logistics_updates(
        session,
        po,
        promised_dispatch_date=parse_iso_date_utc(triage_result.promised_dispatch_date),
        actual_dispatch_date=parse_iso_date_utc(triage_result.actual_dispatch_date),
        carrier_name=triage_result.carrier_name,
        tracking_number=triage_result.tracking_number,
        details_promised_later=bool(triage_result.details_promised_later and is_safe),
        details_due_days=deferral_days,
        details_due_at=remind_at if (triage_result.details_promised_later and is_safe) else None,
    )

    promised_eta_str = (
        clock.ensure_utc(po.promised_eta).date().isoformat() if po.promised_eta else "TBD"
    )
    changed_text = _format_changed_fields(logistics_res.changed_fields)
    changed_payload = _serialize_changed_fields(logistics_res.changed_fields)

    if logistics_res.dispatch_details_status == DispatchDetailsStatus.COMPLETE.value:
        cancel_pending_lex_reminders_for_po(
            session, po.id, reason="Complete dispatch details received on acknowledgment"
        )
        auto_resolve_po_alerts(
            session,
            po.id,
            (
                "MISSING_DISPATCH_DETAILS",
                "MISSING_DISPATCH_DETAILS_DUE",
                "DISPATCH_DETAILS_DEFERRED",
                "DISPATCH_DEFERRAL_STOCKOUT_RISK",
            ),
        )
        append_po_note(
            session,
            po,
            f"Acknowledged with complete dispatch details ({changed_text}).",
        )
        alert = create_action_item(
            session,
            type="PO_ACKNOWLEDGED_COMPLETE",
            severity=AlertSeverity.INFO,
            part_criticality=part.criticality,
            title=f"PO acknowledged with full dispatch details: {po.po_number} ({part.sku})",
            summary=(
                f"Lex acknowledged {po.po_number} from {supplier.name} (Promised ETA: "
                f"{promised_eta_str}) and recorded {changed_text}."
            ),
            po_id=po.id,
            part_id=part.id,
            suggested_actions=[],
            cta_payload={
                "po_id": po.id,
                "comm_id": email.id,
                "promised_eta": promised_eta_str,
                "dispatch_details_status": logistics_res.dispatch_details_status,
                "changed_fields": changed_payload,
            },
        )
        evaluate_memory_advisory_alerts(
            session,
            po=po,
            part=part,
            supplier=supplier,
            trigger_stage="pre_delivery",
            tracker=tracker,
        )
        return {"alert": alert, "draft": None, "logistics": logistics_res}

    missing_text = _format_missing_fields(logistics_res.missing_fields)
    recorded_clause = (
        f" Recorded: {changed_text}." if logistics_res.changed_fields else ""
    )

    if (
        triage_result.details_promised_later
        and is_safe
        and logistics_res.dispatch_details_status == DispatchDetailsStatus.DEFERRED_2D.value
    ):
        assert logistics_res.dispatch_details_due_at is not None
        due_utc = clock.ensure_utc(logistics_res.dispatch_details_due_at)
        due_str = due_utc.date().isoformat()
        rem_res = schedule_po_lex_reminder(
            session,
            po_id=po.id,
            remind_at=due_utc,
            reason=(
                f"Supplier promised missing dispatch details [{missing_text}] "
                f"after {deferral_days} days (by {due_str})."
            ),
            suggested_action=(
                "Inspect live PO/GRN/thread state and draft DISPATCH_DETAILS_NUDGE "
                "only if details remain missing."
            ),
            comm_id=email.id,
            context_extra={
                "kind": "dispatch_details_followup",
                "deferral_days": deferral_days,
                "missing_fields": logistics_res.missing_fields,
                "days_to_stockout": round(days_to_stockout, 2)
                if days_to_stockout is not None
                else None,
            },
            tracker=tracker,
        )
        promised_by_clause = (
            f" (supplier target: {triage_result.details_promised_by_date})"
            if triage_result.details_promised_by_date
            else ""
        )
        runway_clause = (
            f" Stockout runway ({days_to_stockout:.1f}d) safely covers the "
            f"{deferral_days}-day wait."
            if days_to_stockout is not None
            else ""
        )
        append_po_note(
            session,
            po,
            f"Acknowledged; missing [{missing_text}]. Waiting {deferral_days} days until "
            f"{due_str}{promised_by_clause}.{runway_clause}",
        )
        alert = create_action_item(
            session,
            type="DISPATCH_DETAILS_DEFERRED",
            severity=AlertSeverity.INFO,
            part_criticality=part.criticality,
            days_to_stockout=days_to_stockout,
            title=(
                f"PO acknowledged; waiting {deferral_days} days for dispatch details: "
                f"{po.po_number}"
            ),
            summary=(
                f"Lex acknowledged {po.po_number} from {supplier.name} (Promised ETA: "
                f"{promised_eta_str}).{recorded_clause} Missing [{missing_text}]; supplier "
                f"promised details later{promised_by_clause}, so Lex scheduled a "
                f"{deferral_days}-day check-in for {due_str}.{runway_clause}"
            ),
            po_id=po.id,
            part_id=part.id,
            suggested_actions=[],
            cta_payload={
                "po_id": po.id,
                "comm_id": email.id,
                "reminder_id": rem_res["reminder_id"],
                "deferral_days": deferral_days,
                "days_to_stockout": (
                    round(days_to_stockout, 2) if days_to_stockout is not None else None
                ),
                "days_until_eta": (
                    round(days_until_eta, 2) if days_until_eta is not None else None
                ),
                "promised_eta": promised_eta_str,
                "dispatch_details_status": logistics_res.dispatch_details_status,
                "dispatch_details_due_at": due_utc.isoformat(),
                "missing_fields": logistics_res.missing_fields,
                "details_promised_by_date": triage_result.details_promised_by_date,
                "changed_fields": changed_payload,
            },
        )
        evaluate_memory_advisory_alerts(
            session,
            po=po,
            part=part,
            supplier=supplier,
            trigger_stage="pre_delivery",
            tracker=tracker,
        )
        return {"alert": alert, "draft": None, "logistics": logistics_res}

    if triage_result.details_promised_later and not is_safe:
        cancel_pending_lex_reminders_for_po(
            session, po.id, reason="Deferral rejected due to stockout risk"
        )
        reason_text = "; ".join(unsafe_reasons)
        append_po_note(
            session,
            po,
            f"Acknowledged; supplier asked to defer dispatch details by {deferral_days} days, "
            f"but deferral is unsafe ({reason_text}). Drafted immediate expedite/dispatch request.",
        )
        cc_list = [PersonaKey_val for PersonaKey_val in ["inventory_manager"]]
        if part.criticality == Criticality.CRITICAL.value:
            cc_list.append("procurement")
        draft = _draft_immediate_dispatch_request(
            session,
            engine,
            po=po,
            part=part,
            supplier=supplier,
            email=email,
            promised_eta_str=promised_eta_str,
            missing_fields=logistics_res.missing_fields,
            required_cc_personas=cc_list,
            extra_context={
                "deferral_rejected_days": deferral_days,
                "days_to_stockout": (
                    round(days_to_stockout, 2) if days_to_stockout is not None else None
                ),
                "days_until_eta": (
                    round(days_until_eta, 2) if days_until_eta is not None else None
                ),
                "stockout_risk_reasons": unsafe_reasons,
                "urgency_note": (
                    f"Waiting {deferral_days} days is unsafe because {reason_text}. "
                    "Request immediate dispatch confirmation or expedited schedule."
                ),
            },
        )
        backup = _backup_supplier_payload(session, part.id, supplier.id)
        suggested = [
            {
                "action": "approve_draft",
                "label": "Approve & Send Expedite/Details Request",
            }
        ]
        if backup.get("backup_supplier_id"):
            suggested.append(
                {
                    "action": "switch_backup_supplier",
                    "label": f"Switch to {backup['backup_supplier_name']}",
                }
            )
        suggested.append({"action": "reject_draft", "label": "Reject Draft"})
        runway_str = f"{days_to_stockout:.1f}d" if days_to_stockout is not None else "unknown"
        alert = create_action_item(
            session,
            type="DISPATCH_DEFERRAL_STOCKOUT_RISK",
            severity=AlertSeverity.HIGH,
            part_criticality=part.criticality,
            days_to_stockout=days_to_stockout,
            title=(
                f"Stockout risk on {po.po_number} ({part.sku}): "
                f"{deferral_days}d dispatch-details deferral unsafe"
            ),
            summary=(
                f"{supplier.name} asked to share dispatch details [{missing_text}] after "
                f"{deferral_days} days on {po.po_number}, but {part.sku} has only {runway_str} "
                f"runway ({reason_text}). Lex drafted an immediate request instead of waiting."
            ),
            po_id=po.id,
            part_id=part.id,
            draft_comm_id=draft.id,
            suggested_actions=suggested,
            cta_payload={
                "draft_comm_id": draft.id,
                "po_id": po.id,
                "comm_id": email.id,
                "deferral_days": deferral_days,
                "days_to_stockout": (
                    round(days_to_stockout, 2) if days_to_stockout is not None else None
                ),
                "days_until_eta": (
                    round(days_until_eta, 2) if days_until_eta is not None else None
                ),
                "unsafe_reasons": unsafe_reasons,
                "missing_fields": logistics_res.missing_fields,
                **backup,
            },
        )
        return {"alert": alert, "draft": draft, "logistics": logistics_res}

    # AWAITING_IMMEDIATE: supplier ack lacked dispatch fields and did not promise them later
    append_po_note(
        session,
        po,
        f"Acknowledged; missing [{missing_text}]. Drafted immediate dispatch details request.",
    )
    draft = _draft_immediate_dispatch_request(
        session,
        engine,
        po=po,
        part=part,
        supplier=supplier,
        email=email,
        promised_eta_str=promised_eta_str,
        missing_fields=logistics_res.missing_fields,
    )
    alert = create_action_item(
        session,
        type="MISSING_DISPATCH_DETAILS",
        severity=AlertSeverity.MEDIUM,
        part_criticality=part.criticality,
        title=f"PO acknowledged; missing dispatch details for {po.po_number} ({part.sku})",
        summary=(
            f"Lex acknowledged {po.po_number} from {supplier.name} (Promised ETA: "
            f"{promised_eta_str}).{recorded_clause} Missing [{missing_text}]. "
            "Lex drafted a request email for your approval."
        ),
        po_id=po.id,
        part_id=part.id,
        draft_comm_id=draft.id,
        suggested_actions=[
            {"action": "approve_draft", "label": "Approve & Send Request"},
            {"action": "reject_draft", "label": "Reject Draft"},
        ],
        cta_payload={
            "draft_comm_id": draft.id,
            "po_id": po.id,
            "comm_id": email.id,
            "promised_eta": promised_eta_str,
            "dispatch_details_status": logistics_res.dispatch_details_status,
            "missing_fields": logistics_res.missing_fields,
            "changed_fields": changed_payload,
        },
    )
    return {"alert": alert, "draft": draft, "logistics": logistics_res}


def handle_followup_logistics_update(
    session: Session,
    *,
    po: PurchaseOrder,
    part: Part,
    supplier: Supplier,
    email: StoredEmail,
    triage_result: EmailTriageResult,
    create_plain_message_fallback: bool = True,
    engine: "WorkflowEngine | None" = None,
    tracker: LexToolTracker | None = None,
) -> dict[str, Any]:
    """Apply logistics updates from follow-up supplier emails and notify InventoryManager."""
    post_ack_statuses = (
        POStatus.ACKNOWLEDGED.value,
        POStatus.DELAYED_CRITICAL.value,
        POStatus.OVERDUE.value,
    )
    if po.status not in post_ack_statuses or not has_logistics_payload(triage_result):
        if not create_plain_message_fallback:
            return {"alert": None, "draft": None, "logistics": None}
        alert = create_action_item(
            session,
            type="SUPPLIER_MESSAGE",
            severity=AlertSeverity.INFO,
            title=f"Supplier update on {po.po_number}",
            summary=triage_result.summary_for_manager,
            po_id=po.id,
            part_id=part.id,
            suggested_actions=[],
            cta_payload={"po_id": po.id, "comm_id": email.id},
        )
        return {"alert": alert, "draft": None, "logistics": None}

    sim_now = clock.now(session)
    prev_status = po.dispatch_details_status
    prev_due_at = po.dispatch_details_due_at
    repeated_deferral = (
        triage_result.details_promised_later
        and prev_status
        in (
            DispatchDetailsStatus.TIMED_OUT.value,
            DispatchDetailsStatus.AWAITING_IMMEDIATE.value,
        )
    )

    deferral_days = 2
    remind_at = sim_now + timedelta(days=2)
    is_safe = True
    days_to_stockout: float | None = None
    days_until_eta: float | None = None
    unsafe_reasons: list[str] = []

    want_deferral = bool(
        triage_result.details_promised_later
        and not repeated_deferral
        and prev_status != DispatchDetailsStatus.DEFERRED_2D.value
    )
    if want_deferral:
        deferral_days, remind_at = resolve_deferral_schedule(
            session,
            po=po,
            triage_result=triage_result,
            sim_now=sim_now,
            tracker=tracker,
        )
        is_safe, days_to_stockout, days_until_eta, unsafe_reasons = evaluate_deferral_safety(
            session,
            po=po,
            part=part,
            supplier=supplier,
            deferral_days=deferral_days,
            sim_now=sim_now,
        )

    logistics_res = apply_po_logistics_updates(
        session,
        po,
        promised_dispatch_date=parse_iso_date_utc(triage_result.promised_dispatch_date),
        actual_dispatch_date=parse_iso_date_utc(triage_result.actual_dispatch_date),
        carrier_name=triage_result.carrier_name,
        tracking_number=triage_result.tracking_number,
        details_promised_later=bool(want_deferral and is_safe),
        details_due_days=deferral_days,
        details_due_at=remind_at if (want_deferral and is_safe) else None,
    )

    # Preserve existing DEFERRED_2D deadline on partial updates so the wait SLA neither
    # disappears (early partial info) nor resets endlessly (repeated deferral).
    if (
        prev_status == DispatchDetailsStatus.DEFERRED_2D.value
        and prev_due_at is not None
        and logistics_res.missing_fields
    ):
        po.dispatch_details_status = DispatchDetailsStatus.DEFERRED_2D.value
        po.dispatch_details_due_at = prev_due_at
        logistics_res.dispatch_details_status = DispatchDetailsStatus.DEFERRED_2D.value
        logistics_res.dispatch_details_due_at = prev_due_at
        session.flush()

    # Prevent infinite DEFERRED_2D loop if supplier repeats a deferral while already TIMED_OUT
    if (
        repeated_deferral
        and prev_status == DispatchDetailsStatus.TIMED_OUT.value
        and logistics_res.missing_fields
    ):
        po.dispatch_details_status = DispatchDetailsStatus.TIMED_OUT.value
        po.dispatch_details_due_at = None
        logistics_res.dispatch_details_status = DispatchDetailsStatus.TIMED_OUT.value
        session.flush()

    changed_text = _format_changed_fields(logistics_res.changed_fields)
    changed_payload = _serialize_changed_fields(logistics_res.changed_fields)

    if not logistics_res.missing_fields:
        cancel_pending_lex_reminders_for_po(
            session, po.id, reason="Complete dispatch details received via follow-up email"
        )
        resolved_count = auto_resolve_po_alerts(
            session,
            po.id,
            (
                "MISSING_DISPATCH_DETAILS",
                "MISSING_DISPATCH_DETAILS_DUE",
                "DISPATCH_DETAILS_DEFERRED",
                "DISPATCH_DEFERRAL_STOCKOUT_RISK",
            ),
        )
        if logistics_res.changed_fields or prev_status != DispatchDetailsStatus.COMPLETE.value:
            append_po_note(
                session,
                po,
                f"Logistics details completed via email #{email.id} ({changed_text}).",
            )
            alert = create_action_item(
                session,
                type="PO_LOGISTICS_UPDATED",
                severity=AlertSeverity.INFO,
                part_criticality=part.criticality,
                title=f"Logistics complete on {po.po_number} ({part.sku})",
                summary=(
                    f"Lex updated {po.po_number} from {supplier.name}: {changed_text}. "
                    "All required dispatch details are now complete."
                ),
                po_id=po.id,
                part_id=part.id,
                suggested_actions=[],
                cta_payload={
                    "po_id": po.id,
                    "comm_id": email.id,
                    "dispatch_details_status": logistics_res.dispatch_details_status,
                    "changed_fields": changed_payload,
                    "resolved_missing_alerts": resolved_count,
                },
            )
            evaluate_memory_advisory_alerts(
                session,
                po=po,
                part=part,
                supplier=supplier,
                trigger_stage="pre_delivery",
                tracker=tracker,
            )
            return {"alert": alert, "draft": None, "logistics": logistics_res}

    if repeated_deferral:
        missing_text = _format_missing_fields(logistics_res.missing_fields)
        append_po_note(
            session,
            po,
            f"Supplier deferred dispatch details again while status was {prev_status}; "
            f"deferral timer not restarted. Still missing: [{missing_text}].",
        )
        sev = (
            AlertSeverity.HIGH
            if prev_status == DispatchDetailsStatus.TIMED_OUT.value
            else AlertSeverity.MEDIUM
        )
        update_clause = (
            f" Updated: {changed_text}." if logistics_res.changed_fields else ""
        )
        alert = create_action_item(
            session,
            type="PO_LOGISTICS_UPDATED",
            severity=sev,
            part_criticality=part.criticality,
            title=f"Repeated dispatch-detail deferral on {po.po_number} ({supplier.name})",
            summary=(
                f"{supplier.name} again promised dispatch details later on {po.po_number} "
                f"while already {prev_status}.{update_clause} Still missing [{missing_text}]; "
                "Lex kept escalation active instead of restarting a wait timer."
            ),
            po_id=po.id,
            part_id=part.id,
            suggested_actions=[],
            cta_payload={
                "po_id": po.id,
                "comm_id": email.id,
                "dispatch_details_status": po.dispatch_details_status,
                "missing_fields": logistics_res.missing_fields,
                "changed_fields": changed_payload,
                "repeated_deferral": True,
            },
        )
        return {"alert": alert, "draft": None, "logistics": logistics_res}

    if want_deferral and not is_safe:
        missing_text = _format_missing_fields(logistics_res.missing_fields)
        reason_text = "; ".join(unsafe_reasons)
        append_po_note(
            session,
            po,
            f"Follow-up requested {deferral_days}-day dispatch deferral, rejected due to "
            f"stockout risk ({reason_text}).",
        )
        promised_eta_str = (
            clock.ensure_utc(po.promised_eta).date().isoformat() if po.promised_eta else "TBD"
        )
        draft: StoredEmail | None = None
        if engine is not None:
            draft = _draft_immediate_dispatch_request(
                session,
                engine,
                po=po,
                part=part,
                supplier=supplier,
                email=email,
                promised_eta_str=promised_eta_str,
                missing_fields=logistics_res.missing_fields,
                required_cc_personas=(
                    ["inventory_manager", "procurement"]
                    if part.criticality == Criticality.CRITICAL.value
                    else ["inventory_manager"]
                ),
                extra_context={
                    "deferral_rejected_days": deferral_days,
                    "days_to_stockout": (
                        round(days_to_stockout, 2) if days_to_stockout is not None else None
                    ),
                    "stockout_risk_reasons": unsafe_reasons,
                },
            )
        runway_str = f"{days_to_stockout:.1f}d" if days_to_stockout is not None else "unknown"
        suggested = (
            [
                {"action": "approve_draft", "label": "Approve & Send Expedite/Details Request"},
                {"action": "reject_draft", "label": "Reject Draft"},
            ]
            if draft is not None
            else []
        )
        alert = create_action_item(
            session,
            type="DISPATCH_DEFERRAL_STOCKOUT_RISK",
            severity=AlertSeverity.HIGH,
            part_criticality=part.criticality,
            days_to_stockout=days_to_stockout,
            title=(
                f"Stockout risk on {po.po_number} ({part.sku}): "
                f"{deferral_days}d dispatch-details deferral unsafe"
            ),
            summary=(
                f"{supplier.name} asked to share dispatch details [{missing_text}] after "
                f"{deferral_days} days on {po.po_number}, but {part.sku} has only {runway_str} "
                f"runway ({reason_text})."
            ),
            po_id=po.id,
            part_id=part.id,
            draft_comm_id=draft.id if draft is not None else None,
            suggested_actions=suggested,
            cta_payload={
                "draft_comm_id": draft.id if draft is not None else None,
                "po_id": po.id,
                "comm_id": email.id,
                "deferral_days": deferral_days,
                "days_to_stockout": (
                    round(days_to_stockout, 2) if days_to_stockout is not None else None
                ),
                "unsafe_reasons": unsafe_reasons,
                "missing_fields": logistics_res.missing_fields,
            },
        )
        return {"alert": alert, "draft": draft, "logistics": logistics_res}

    if (
        logistics_res.dispatch_details_status == DispatchDetailsStatus.DEFERRED_2D.value
        and prev_status != DispatchDetailsStatus.DEFERRED_2D.value
    ):
        assert logistics_res.dispatch_details_due_at is not None
        due_utc = clock.ensure_utc(logistics_res.dispatch_details_due_at)
        due_str = due_utc.date().isoformat()
        missing_text = _format_missing_fields(logistics_res.missing_fields)
        update_clause = (
            f" Updated: {changed_text}." if logistics_res.changed_fields else ""
        )
        rem_res = schedule_po_lex_reminder(
            session,
            po_id=po.id,
            remind_at=due_utc,
            reason=(
                f"Supplier promised remaining dispatch details [{missing_text}] "
                f"after {deferral_days} days (by {due_str})."
            ),
            suggested_action=(
                "Inspect live PO/GRN/thread state and draft DISPATCH_DETAILS_NUDGE "
                "only if details remain missing."
            ),
            comm_id=email.id,
            context_extra={
                "kind": "dispatch_details_followup",
                "deferral_days": deferral_days,
                "missing_fields": logistics_res.missing_fields,
                "days_to_stockout": (
                    round(days_to_stockout, 2) if days_to_stockout is not None else None
                ),
            },
            tracker=tracker,
        )
        append_po_note(
            session,
            po,
            f"Follow-up logistics update via email #{email.id}.{update_clause} "
            f"Waiting {deferral_days} days until {due_str} for [{missing_text}].",
        )
        alert = create_action_item(
            session,
            type="DISPATCH_DETAILS_DEFERRED",
            severity=AlertSeverity.INFO,
            part_criticality=part.criticality,
            days_to_stockout=days_to_stockout,
            title=f"Waiting {deferral_days} days for remaining dispatch details: {po.po_number}",
            summary=(
                f"Lex processed follow-up from {supplier.name} on {po.po_number}."
                f"{update_clause} Still missing [{missing_text}]; Lex scheduled a "
                f"{deferral_days}-day check-in for {due_str}."
            ),
            po_id=po.id,
            part_id=part.id,
            suggested_actions=[],
            cta_payload={
                "po_id": po.id,
                "comm_id": email.id,
                "reminder_id": rem_res["reminder_id"],
                "deferral_days": deferral_days,
                "dispatch_details_status": logistics_res.dispatch_details_status,
                "dispatch_details_due_at": due_utc.isoformat(),
                "missing_fields": logistics_res.missing_fields,
                "changed_fields": changed_payload,
            },
        )
        return {"alert": alert, "draft": None, "logistics": logistics_res}

    if logistics_res.changed_fields:
        missing_text = _format_missing_fields(logistics_res.missing_fields)
        due_clause = (
            f" Check-in remains scheduled for "
            f"{clock.ensure_utc(po.dispatch_details_due_at).date().isoformat()}."
            if po.dispatch_details_status == DispatchDetailsStatus.DEFERRED_2D.value
            and po.dispatch_details_due_at is not None
            else ""
        )
        append_po_note(
            session,
            po,
            f"Logistics updated via email #{email.id} ({changed_text}). "
            f"Still missing: [{missing_text}].{due_clause}",
        )
        alert = create_action_item(
            session,
            type="PO_LOGISTICS_UPDATED",
            severity=AlertSeverity.INFO,
            part_criticality=part.criticality,
            title=f"Logistics updated on {po.po_number} ({part.sku})",
            summary=(
                f"Lex updated {po.po_number} from {supplier.name}: {changed_text}. "
                f"Still missing [{missing_text}].{due_clause}"
            ),
            po_id=po.id,
            part_id=part.id,
            suggested_actions=[],
            cta_payload={
                "po_id": po.id,
                "comm_id": email.id,
                "dispatch_details_status": logistics_res.dispatch_details_status,
                "missing_fields": logistics_res.missing_fields,
                "changed_fields": changed_payload,
            },
        )
        return {"alert": alert, "draft": None, "logistics": logistics_res}

    if not create_plain_message_fallback:
        return {"alert": None, "draft": None, "logistics": logistics_res}

    alert = create_action_item(
        session,
        type="SUPPLIER_MESSAGE",
        severity=AlertSeverity.INFO,
        title=f"Supplier update on {po.po_number}",
        summary=triage_result.summary_for_manager,
        po_id=po.id,
        part_id=part.id,
        suggested_actions=[],
        cta_payload={"po_id": po.id, "comm_id": email.id},
    )
    return {"alert": alert, "draft": None, "logistics": logistics_res}
