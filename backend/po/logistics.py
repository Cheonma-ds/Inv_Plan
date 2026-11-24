from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import DispatchDetailsStatus
from backend.core.models import PurchaseOrder


@dataclass
class LogisticsUpdateResult:
    """Structured diff and missing-detail status after updating PO logistics fields."""

    changed_fields: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    dispatch_details_status: str = DispatchDetailsStatus.NONE.value
    dispatch_details_due_at: datetime | None = None


def compute_missing_dispatch_fields(po: PurchaseOrder) -> list[str]:
    """Return missing shipment dispatch commitments for an active PO."""
    missing: list[str] = []
    if po.promised_dispatch_date is None and po.actual_dispatch_date is None:
        missing.append("promised_dispatch_date")
    if not (po.carrier_name and po.carrier_name.strip()):
        missing.append("carrier_name")
    if not (po.tracking_number and po.tracking_number.strip()):
        missing.append("tracking_number")
    return missing


def apply_po_logistics_updates(
    session: Session,
    po: PurchaseOrder,
    *,
    promised_dispatch_date: datetime | None = None,
    actual_dispatch_date: datetime | None = None,
    carrier_name: str | None = None,
    tracking_number: str | None = None,
    details_promised_later: bool = False,
    details_due_days: int = 2,
    details_due_at: datetime | None = None,
) -> LogisticsUpdateResult:
    """Apply extracted carrier/tracking/dispatch updates and recompute missing-detail state."""
    changed: dict[str, tuple[Any, Any]] = {}

    if promised_dispatch_date is not None:
        normalized = clock.ensure_utc(promised_dispatch_date)
        prev = clock.ensure_utc(po.promised_dispatch_date) if po.promised_dispatch_date else None
        if prev != normalized:
            changed["promised_dispatch_date"] = (
                prev.date().isoformat() if prev else None,
                normalized.date().isoformat(),
            )
            po.promised_dispatch_date = normalized

    if actual_dispatch_date is not None:
        normalized = clock.ensure_utc(actual_dispatch_date)
        prev = clock.ensure_utc(po.actual_dispatch_date) if po.actual_dispatch_date else None
        if prev != normalized:
            changed["actual_dispatch_date"] = (
                prev.date().isoformat() if prev else None,
                normalized.date().isoformat(),
            )
            po.actual_dispatch_date = normalized

    if carrier_name is not None and carrier_name.strip():
        cleaned_carrier = carrier_name.strip()
        if po.carrier_name != cleaned_carrier:
            changed["carrier_name"] = (po.carrier_name, cleaned_carrier)
            po.carrier_name = cleaned_carrier

    if tracking_number is not None and tracking_number.strip():
        cleaned_tracking = tracking_number.strip()
        if po.tracking_number != cleaned_tracking:
            changed["tracking_number"] = (po.tracking_number, cleaned_tracking)
            po.tracking_number = cleaned_tracking

    missing = compute_missing_dispatch_fields(po)
    po.missing_dispatch_fields_json = missing

    sim_now = clock.now(session)
    if not missing:
        po.dispatch_details_status = DispatchDetailsStatus.COMPLETE.value
        po.dispatch_details_due_at = None
    elif details_promised_later:
        po.dispatch_details_status = DispatchDetailsStatus.DEFERRED_2D.value
        if details_due_at is not None:
            po.dispatch_details_due_at = clock.ensure_utc(details_due_at)
        else:
            po.dispatch_details_due_at = sim_now + timedelta(days=max(1, int(details_due_days)))
    else:
        po.dispatch_details_status = DispatchDetailsStatus.AWAITING_IMMEDIATE.value
        po.dispatch_details_due_at = None

    session.flush()
    return LogisticsUpdateResult(
        changed_fields=changed,
        missing_fields=missing,
        dispatch_details_status=po.dispatch_details_status,
        dispatch_details_due_at=po.dispatch_details_due_at,
    )
