from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import ActionItemStatus, AlertSeverity, CommStatus, Criticality
from backend.core.models import ActionItem, POCommunication

SEVERITY_BASE_SCORE: dict[str, int] = {
    AlertSeverity.CRITICAL.value: 80,
    AlertSeverity.HIGH.value: 60,
    AlertSeverity.MEDIUM.value: 35,
    AlertSeverity.INFO.value: 10,
}

CRITICALITY_BONUS: dict[str, int] = {
    Criticality.CRITICAL.value: 8,
    Criticality.STANDARD.value: 4,
    Criticality.LOW.value: 1,
}


class ActionItemNotFoundError(ValueError):
    pass


def compute_urgency_score(
    severity: AlertSeverity | str,
    *,
    part_criticality: Criticality | str | None = None,
    days_to_stockout: float | None = None,
) -> int:
    """Deterministic 0-100 urgency score combining severity, criticality, and stockout proximity.

    Strict non-overlapping severity bands guarantee CRITICAL (80..100) > HIGH (60..79)
    > MEDIUM (35..54) > INFO (10..29) while ranking higher-criticality and near-stockout
    items first within each tier.
    """
    sev_val = AlertSeverity(severity).value
    base = SEVERITY_BASE_SCORE[sev_val]

    crit_bonus = 0
    if part_criticality is not None:
        crit_val = (
            part_criticality.value
            if isinstance(part_criticality, Criticality)
            else str(part_criticality)
        )
        crit_bonus = CRITICALITY_BONUS.get(crit_val, 0)

    stockout_bonus = 0
    if days_to_stockout is not None:
        if days_to_stockout <= 0:
            stockout_bonus = 11
        elif days_to_stockout <= 3:
            stockout_bonus = 9
        elif days_to_stockout <= 7:
            stockout_bonus = 6
        elif days_to_stockout <= 14:
            stockout_bonus = 3

    band_cap = 100 if sev_val == AlertSeverity.CRITICAL.value else base + 19
    return min(band_cap, base + crit_bonus + stockout_bonus)


def create_action_item(
    session: Session,
    *,
    type: str,
    severity: AlertSeverity | str,
    title: str,
    summary: str,
    urgency_score: int | None = None,
    part_criticality: Criticality | str | None = None,
    days_to_stockout: float | None = None,
    po_id: int | None = None,
    part_id: int | None = None,
    draft_comm_id: int | None = None,
    suggested_actions: list[dict[str, Any]] | None = None,
    cta_payload: dict[str, Any] | None = None,
    assigned_team: str = "Inventory",
) -> ActionItem:
    resolved_urgency = (
        urgency_score
        if urgency_score is not None
        else compute_urgency_score(
            severity,
            part_criticality=part_criticality,
            days_to_stockout=days_to_stockout,
        )
    )
    item = ActionItem(
        type=type,
        severity=AlertSeverity(severity).value,
        urgency_score=resolved_urgency,
        po_id=po_id,
        part_id=part_id,
        draft_comm_id=draft_comm_id,
        title=title,
        summary=summary,
        suggested_actions_json=suggested_actions or [],
        cta_payload_json=cta_payload or {},
        status=ActionItemStatus.OPEN.value,
        assigned_team=assigned_team,
        created_at=clock.now(session),
        resolved_at=None,
    )
    session.add(item)
    session.flush()
    return item


def list_action_items(
    session: Session,
    *,
    status: ActionItemStatus | str | None = ActionItemStatus.OPEN,
) -> list[ActionItem]:
    stmt = select(ActionItem)
    if status is not None:
        stmt = stmt.where(ActionItem.status == ActionItemStatus(status).value)
    stmt = stmt.order_by(ActionItem.urgency_score.desc(), ActionItem.created_at.desc())
    return list(session.scalars(stmt).all())


def resolve_action_item(session: Session, action_item_id: int) -> ActionItem:
    item = session.get(ActionItem, action_item_id)
    if item is None:
        raise ActionItemNotFoundError(f"ActionItem {action_item_id} not found")
    item.status = ActionItemStatus.RESOLVED.value
    item.resolved_at = clock.now(session)
    session.flush()
    return item


def dismiss_action_item(session: Session, action_item_id: int) -> ActionItem:
    item = session.get(ActionItem, action_item_id)
    if item is None:
        raise ActionItemNotFoundError(f"ActionItem {action_item_id} not found")
    item.status = ActionItemStatus.DISMISSED.value
    item.resolved_at = clock.now(session)
    item.dismiss_resolved_at = None
    if item.draft_comm_id is not None:
        comm = session.get(POCommunication, item.draft_comm_id)
        if comm is not None and comm.status == CommStatus.DRAFT_PENDING_APPROVAL.value:
            comm.status = CommStatus.REJECTED.value
    session.flush()
    return item


def reopen_action_item(session: Session, action_item_id: int) -> ActionItem:
    item = session.get(ActionItem, action_item_id)
    if item is None:
        raise ActionItemNotFoundError(f"ActionItem {action_item_id} not found")
    item.status = ActionItemStatus.OPEN.value
    item.resolved_at = None
    item.dismiss_resolved_at = None
    session.flush()
    return item


def acknowledge_dismissed_action_item(session: Session, action_item_id: int) -> ActionItem:
    item = session.get(ActionItem, action_item_id)
    if item is None:
        raise ActionItemNotFoundError(f"ActionItem {action_item_id} not found")
    if item.status != ActionItemStatus.DISMISSED.value:
        raise ValueError(f"ActionItem {action_item_id} is not DISMISSED")
    item.dismiss_resolved_at = clock.now(session)
    session.flush()
    return item
