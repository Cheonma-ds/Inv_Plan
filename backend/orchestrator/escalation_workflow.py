from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import AlertSeverity, EmailCategory, PersonaKey
from backend.core.models import Part, POCommunication, Supplier
from backend.mailroom import OutboundMessage, send_email
from backend.po import create_action_item, escalate_po_to_procurement


def _latest_po_thread_id(session: Session, po_id: int) -> str | None:
    return session.scalar(
        select(POCommunication.thread_id)
        .where(POCommunication.po_id == po_id)
        .order_by(POCommunication.timestamp.desc(), POCommunication.id.desc())
        .limit(1)
    )


def _format_dt(dt: Any) -> str:
    if dt is None:
        return "—"
    return clock.ensure_utc(dt).date().isoformat()


def escalate_po_to_procurement_workflow(
    session: Session,
    *,
    po_id: int,
    reason: str = "",
) -> dict[str, Any]:
    """Escalate an active PO to Procurement: update PO, email procurement, and raise alert."""
    po = escalate_po_to_procurement(session, po_id, note=reason)
    part = session.get(Part, po.part_id)
    supplier = session.get(Supplier, po.supplier_id)

    sku = part.sku if part else f"PART-{po.part_id}"
    part_name = part.name if part else "Unknown Part"
    part_crit = part.criticality if part else None
    sup_name = supplier.name if supplier else f"Supplier #{po.supplier_id}"
    sup_rel = f"{supplier.reliability_score:.1f}/100" if supplier else "N/A"
    total_val = round(po.qty * po.unit_price, 2)
    missing_fields = list(po.missing_dispatch_fields_json or [])

    subject = f"[{po.po_number}] Escalation to Procurement — {sku} ({sup_name})"
    body_lines = [
        "Hi Procurement Team,",
        "",
        f"The Inventory Manager has escalated {po.po_number} for Procurement action.",
        "",
        f"- Part: {sku} — {part_name}" + (f" ({part_crit})" if part_crit else ""),
        f"- Supplier: {sup_name} (Reliability Score: {sup_rel})",
        f"- Order Qty & Value: {po.qty} units @ ₹{po.unit_price:,.2f} (Total ₹{total_val:,.2f})",
        f"- Current PO Status: {po.status}",
        f"- Follow-ups Sent: {po.follow_up_count}",
        f"- Promised / Revised ETA: {_format_dt(po.promised_eta)} / {_format_dt(po.revised_eta)}",
        f"- Promised / Actual Dispatch: {_format_dt(po.promised_dispatch_date)} / "
        f"{_format_dt(po.actual_dispatch_date)}",
        f"- Carrier / Tracking: {po.carrier_name or '—'} / {po.tracking_number or '—'}",
        f"- Missing Dispatch Fields: {', '.join(missing_fields) if missing_fields else 'None'}",
        "",
        f"Escalation Note: {po.procurement_escalation_note}",
    ]

    sent_email = send_email(
        session,
        OutboundMessage(
            from_persona=PersonaKey.INVENTORY_MANAGER,
            to_personas=[PersonaKey.PROCUREMENT],
            cc_personas=[PersonaKey.LEX],
            subject=subject,
            body="\n".join(body_lines),
            po_id=po.id,
            thread_id=_latest_po_thread_id(session, po.id),
            category=EmailCategory.PROCUREMENT_ESCALATION,
        ),
        allow_agent_send=False,
    )

    alert = create_action_item(
        session,
        type="PROCUREMENT_ESCALATION",
        severity=AlertSeverity.HIGH,
        part_criticality=part_crit,
        title=f"Escalated to Procurement: {po.po_number} ({sku})",
        summary=(
            f"Inventory Manager escalated {po.po_number} ({sup_name}, status: {po.status}, "
            f"follow-ups: {po.follow_up_count}) to Procurement. "
            f"Reason: {po.procurement_escalation_note}"
        ),
        po_id=po.id,
        part_id=po.part_id,
        assigned_team="Procurement",
        suggested_actions=[{"action": "add_note", "label": "Log Procurement Update"}],
        cta_payload={
            "po_id": po.id,
            "comm_id": sent_email.id,
            "supplier_id": po.supplier_id,
            "escalation_note": po.procurement_escalation_note,
        },
    )
    session.flush()
    return {
        "po": po,
        "email": sent_email,
        "alert": alert,
    }
