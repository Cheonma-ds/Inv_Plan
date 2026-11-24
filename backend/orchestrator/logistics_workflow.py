from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from backend.agents.lex import EmailTriageResult
from backend.core import clock
from backend.core.enums import AlertSeverity, DispatchDetailsStatus, EmailCategory, POStatus
from backend.core.models import ActionItem, Part, PurchaseOrder, Supplier, SupplierMemoryNote
from backend.mailroom import StoredEmail
from backend.orchestrator.sla_watcher import auto_resolve_po_alerts
from backend.po import append_po_note, apply_po_logistics_updates, create_action_item

if TYPE_CHECKING:
    from backend.orchestrator.workflows import WorkflowEngine

FIELD_LABELS: dict[str, str] = {
    "promised_dispatch_date": "Promised Dispatch Date",
    "actual_dispatch_date": "Actual Dispatch Date",
    "carrier_name": "Carrier",
    "tracking_number": "Tracking Number",
}


def parse_iso_date_utc(date_str: str | None) -> datetime | None:
    """Parse an ISO date or timestamp string into a UTC-aware datetime."""
    if not date_str:
        return None
    try:
        cleaned = date_str.strip()
        if len(cleaned) == 10:
            dt = datetime.fromisoformat(f"{cleaned}T07:30:00+00:00")
        else:
            dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


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


def create_supplier_memory_alert(
    session: Session,
    *,
    supplier: Supplier,
    note: SupplierMemoryNote,
    po: PurchaseOrder | None,
    comm_id: int,
) -> ActionItem:
    """Notify InventoryManager whenever Lex autonomously stores a supplier memory note."""
    po_ref = f" ({po.po_number})" if po is not None else ""
    return create_action_item(
        session,
        type="SUPPLIER_MEMORY_LEARNED",
        severity=AlertSeverity.INFO,
        title=f"Lex saved supplier memory note for {supplier.name}{po_ref}",
        summary=f"Learned from email #{comm_id}: \"{note.note}\"",
        po_id=po.id if po is not None else None,
        part_id=po.part_id if po is not None else None,
        suggested_actions=[],
        cta_payload={
            "supplier_id": supplier.id,
            "memory_note_id": note.id,
            "note": note.note,
            "po_id": po.id if po is not None else None,
            "comm_id": comm_id,
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
) -> dict[str, Any]:
    """Process logistics fields on PO acknowledgment and enforce mandatory Manager alerts."""
    logistics_res = apply_po_logistics_updates(
        session,
        po,
        promised_dispatch_date=parse_iso_date_utc(triage_result.promised_dispatch_date),
        actual_dispatch_date=parse_iso_date_utc(triage_result.actual_dispatch_date),
        carrier_name=triage_result.carrier_name,
        tracking_number=triage_result.tracking_number,
        details_promised_later=triage_result.details_promised_later,
    )

    promised_eta_str = (
        clock.ensure_utc(po.promised_eta).date().isoformat() if po.promised_eta else "TBD"
    )
    changed_text = _format_changed_fields(logistics_res.changed_fields)
    changed_payload = _serialize_changed_fields(logistics_res.changed_fields)

    if logistics_res.dispatch_details_status == DispatchDetailsStatus.COMPLETE.value:
        auto_resolve_po_alerts(
            session,
            po.id,
            (
                "MISSING_DISPATCH_DETAILS",
                "MISSING_DISPATCH_DETAILS_DUE",
                "DISPATCH_DETAILS_DEFERRED",
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
        return {"alert": alert, "draft": None, "logistics": logistics_res}

    missing_text = _format_missing_fields(logistics_res.missing_fields)
    recorded_clause = (
        f" Recorded: {changed_text}." if logistics_res.changed_fields else ""
    )

    if logistics_res.dispatch_details_status == DispatchDetailsStatus.DEFERRED_2D.value:
        assert logistics_res.dispatch_details_due_at is not None
        due_str = clock.ensure_utc(logistics_res.dispatch_details_due_at).date().isoformat()
        promised_by_clause = (
            f" (supplier target: {triage_result.details_promised_by_date})"
            if triage_result.details_promised_by_date
            else ""
        )
        append_po_note(
            session,
            po,
            f"Acknowledged; missing [{missing_text}]. Waiting 2 days until {due_str}"
            f"{promised_by_clause}.",
        )
        alert = create_action_item(
            session,
            type="DISPATCH_DETAILS_DEFERRED",
            severity=AlertSeverity.INFO,
            part_criticality=part.criticality,
            title=f"PO acknowledged; waiting 2 days for dispatch details: {po.po_number}",
            summary=(
                f"Lex acknowledged {po.po_number} from {supplier.name} (Promised ETA: "
                f"{promised_eta_str}).{recorded_clause} Missing [{missing_text}]; supplier "
                f"promised details later{promised_by_clause}, so Lex scheduled a 2-day check-in "
                f"for {due_str}."
            ),
            po_id=po.id,
            part_id=part.id,
            suggested_actions=[],
            cta_payload={
                "po_id": po.id,
                "comm_id": email.id,
                "promised_eta": promised_eta_str,
                "dispatch_details_status": logistics_res.dispatch_details_status,
                "dispatch_details_due_at": clock.ensure_utc(
                    logistics_res.dispatch_details_due_at
                ).isoformat(),
                "missing_fields": logistics_res.missing_fields,
                "details_promised_by_date": triage_result.details_promised_by_date,
                "changed_fields": changed_payload,
            },
        )
        return {"alert": alert, "draft": None, "logistics": logistics_res}

    # AWAITING_IMMEDIATE: supplier ack lacked dispatch fields and did not promise them later
    append_po_note(
        session,
        po,
        f"Acknowledged; missing [{missing_text}]. Drafted immediate dispatch details request.",
    )
    draft = engine._create_lex_draft(
        session,
        po=po,
        part=part,
        supplier=supplier,
        category=EmailCategory.DISPATCH_DETAILS_REQUEST,
        thread_id=email.thread_id,
        thread_subject=email.subject,
        context_facts={
            "promised_eta": promised_eta_str,
            "missing_fields": logistics_res.missing_fields,
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
        },
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

    logistics_res = apply_po_logistics_updates(
        session,
        po,
        promised_dispatch_date=parse_iso_date_utc(triage_result.promised_dispatch_date),
        actual_dispatch_date=parse_iso_date_utc(triage_result.actual_dispatch_date),
        carrier_name=triage_result.carrier_name,
        tracking_number=triage_result.tracking_number,
        details_promised_later=(
            False if repeated_deferral else triage_result.details_promised_later
        ),
    )

    # Preserve existing DEFERRED_2D deadline on partial updates so the 2-day SLA neither
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
        resolved_count = auto_resolve_po_alerts(
            session,
            po.id,
            (
                "MISSING_DISPATCH_DETAILS",
                "MISSING_DISPATCH_DETAILS_DUE",
                "DISPATCH_DETAILS_DEFERRED",
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
            return {"alert": alert, "draft": None, "logistics": logistics_res}

    if repeated_deferral:
        missing_text = _format_missing_fields(logistics_res.missing_fields)
        append_po_note(
            session,
            po,
            f"Supplier deferred dispatch details again while status was {prev_status}; "
            f"2-day timer not restarted. Still missing: [{missing_text}].",
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
                "Lex kept escalation active instead of restarting a 2-day wait."
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

    if (
        logistics_res.dispatch_details_status == DispatchDetailsStatus.DEFERRED_2D.value
        and prev_status != DispatchDetailsStatus.DEFERRED_2D.value
    ):
        assert logistics_res.dispatch_details_due_at is not None
        due_str = clock.ensure_utc(logistics_res.dispatch_details_due_at).date().isoformat()
        missing_text = _format_missing_fields(logistics_res.missing_fields)
        update_clause = (
            f" Updated: {changed_text}." if logistics_res.changed_fields else ""
        )
        append_po_note(
            session,
            po,
            f"Follow-up logistics update via email #{email.id}.{update_clause} "
            f"Waiting 2 days until {due_str} for [{missing_text}].",
        )
        alert = create_action_item(
            session,
            type="DISPATCH_DETAILS_DEFERRED",
            severity=AlertSeverity.INFO,
            part_criticality=part.criticality,
            title=f"Waiting 2 days for remaining dispatch details: {po.po_number}",
            summary=(
                f"Lex processed follow-up from {supplier.name} on {po.po_number}."
                f"{update_clause} Still missing [{missing_text}]; Lex scheduled a 2-day "
                f"check-in for {due_str}."
            ),
            po_id=po.id,
            part_id=part.id,
            suggested_actions=[],
            cta_payload={
                "po_id": po.id,
                "comm_id": email.id,
                "dispatch_details_status": logistics_res.dispatch_details_status,
                "dispatch_details_due_at": clock.ensure_utc(
                    logistics_res.dispatch_details_due_at
                ).isoformat(),
                "missing_fields": logistics_res.missing_fields,
                "changed_fields": changed_payload,
            },
        )
        return {"alert": alert, "draft": None, "logistics": logistics_res}

    if logistics_res.changed_fields:
        missing_text = _format_missing_fields(logistics_res.missing_fields)
        due_clause = (
            f" 2-day check-in remains scheduled for "
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
