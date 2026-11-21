from datetime import datetime

from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import POStatus
from backend.core.models import PurchaseOrder


class InvalidPOTransitionError(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[POStatus, set[POStatus]] = {
    POStatus.PENDING_DISPATCH_APPROVAL: {
        POStatus.SENT_AWAITING_ACK,
        POStatus.CANCELLED,
    },
    POStatus.SENT_AWAITING_ACK: {
        POStatus.ACKNOWLEDGED,
        POStatus.EXCEPTION_REVIEW,
        POStatus.UNRESPONSIVE_ESCALATED,
        POStatus.CANCELLED,
    },
    POStatus.EXCEPTION_REVIEW: {
        POStatus.PENDING_DISPATCH_APPROVAL,
        POStatus.CANCELLED,
    },
    POStatus.UNRESPONSIVE_ESCALATED: {
        POStatus.ACKNOWLEDGED,
        POStatus.EXCEPTION_REVIEW,
        POStatus.CANCELLED,
    },
    POStatus.ACKNOWLEDGED: {
        POStatus.DELAYED_CRITICAL,
        POStatus.ACKNOWLEDGED,
        POStatus.EXCEPTION_REVIEW,
        POStatus.OVERDUE,
        POStatus.RECEIVED_PENDING_INVOICE,
    },
    POStatus.DELAYED_CRITICAL: {
        POStatus.EXCEPTION_REVIEW,
        POStatus.OVERDUE,
        POStatus.RECEIVED_PENDING_INVOICE,
        POStatus.CANCELLED,
    },
    POStatus.OVERDUE: {
        POStatus.RECEIVED_PENDING_INVOICE,
        POStatus.CANCELLED,
    },
    POStatus.RECEIVED_PENDING_INVOICE: {
        POStatus.QUARANTINED_INVOICE_MISMATCH,
        POStatus.CLOSED,
    },
    POStatus.QUARANTINED_INVOICE_MISMATCH: {
        POStatus.CLOSED,
        POStatus.CANCELLED,
    },
    POStatus.CLOSED: set(),
    POStatus.CANCELLED: set(),
}


def can_transition(current: POStatus | str, target: POStatus | str) -> bool:
    cur_enum = POStatus(current)
    tgt_enum = POStatus(target)
    return tgt_enum in ALLOWED_TRANSITIONS[cur_enum]


def append_po_note(session: Session, po: PurchaseOrder, note: str) -> None:
    cleaned = note.strip()
    if not cleaned:
        return
    stamp = clock.now(session).strftime("%Y-%m-%d")
    entry = f"[{stamp}] {cleaned}"
    po.notes = f"{po.notes}\n{entry}".strip() if po.notes else entry


def transition_po(
    session: Session,
    po: PurchaseOrder,
    target: POStatus | str,
    *,
    note: str | None = None,
    promised_eta: datetime | None = None,
    revised_eta: datetime | None = None,
) -> PurchaseOrder:
    current = POStatus(po.status)
    target_enum = POStatus(target)

    if not can_transition(current, target_enum):
        raise InvalidPOTransitionError(
            f"Illegal PO transition for {po.po_number}: {current.value} -> {target_enum.value}"
        )

    po.status = target_enum.value
    sim_now = clock.now(session)

    if target_enum == POStatus.SENT_AWAITING_ACK and po.sent_at is None:
        po.sent_at = sim_now

    if target_enum == POStatus.ACKNOWLEDGED and po.acknowledged_at is None:
        po.acknowledged_at = sim_now

    if target_enum == POStatus.CLOSED and po.closed_at is None:
        po.closed_at = sim_now

    # Original promised_eta is immutable once set so OTD scoring stays truthful.
    if promised_eta is not None and po.promised_eta is None:
        po.promised_eta = clock.ensure_utc(promised_eta)

    if revised_eta is not None:
        po.revised_eta = clock.ensure_utc(revised_eta)

    if note:
        append_po_note(session, po, note)

    session.flush()
    return po
