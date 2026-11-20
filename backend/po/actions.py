from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import ActionItemStatus, AlertSeverity
from backend.core.models import ActionItem


class ActionItemNotFoundError(ValueError):
    pass


def create_action_item(
    session: Session,
    *,
    type: str,
    severity: AlertSeverity | str,
    title: str,
    summary: str,
    urgency_score: int = 50,
    po_id: int | None = None,
    part_id: int | None = None,
    draft_comm_id: int | None = None,
    suggested_actions: list[dict[str, Any]] | None = None,
    cta_payload: dict[str, Any] | None = None,
    assigned_team: str = "Inventory",
) -> ActionItem:
    item = ActionItem(
        type=type,
        severity=AlertSeverity(severity).value,
        urgency_score=urgency_score,
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
    session.flush()
    return item
