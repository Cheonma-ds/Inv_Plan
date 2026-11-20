from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.db import get_session
from backend.core.enums import ActionItemStatus
from backend.core.models import POCommunication
from backend.orchestrator.deps import get_workflow_engine
from backend.orchestrator.workflows import WorkflowEngine
from backend.po import dismiss_action_item, list_action_items

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class ExecuteCTARequest(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)


@router.get("")
def get_alerts(
    status: str | None = Query(default=ActionItemStatus.OPEN.value),
    session: Session = Depends(get_session),
) -> list[dict]:
    items = list_action_items(session, status=status)
    output: list[dict] = []
    for item in items:
        draft_preview = None
        if item.draft_comm_id:
            comm = session.get(POCommunication, item.draft_comm_id)
            if comm is not None:
                draft_preview = {
                    "id": comm.id,
                    "status": comm.status,
                    "category": comm.category,
                    "to_personas": comm.to_personas,
                    "cc_personas": comm.cc_personas,
                    "subject": comm.subject,
                    "body": comm.body,
                }
        output.append(
            {
                "id": item.id,
                "type": item.type,
                "severity": item.severity,
                "urgency_score": item.urgency_score,
                "title": item.title,
                "summary": item.summary,
                "po_id": item.po_id,
                "part_id": item.part_id,
                "assigned_team": item.assigned_team,
                "status": item.status,
                "draft_comm_id": item.draft_comm_id,
                "draft_preview": draft_preview,
                "suggested_actions": item.suggested_actions_json or [],
                "cta_payload": item.cta_payload_json or {},
                "created_at": clock.ensure_utc(item.created_at).isoformat(),
                "resolved_at": (
                    clock.ensure_utc(item.resolved_at).isoformat() if item.resolved_at else None
                ),
            }
        )
    return output


@router.post("/{action_item_id}/execute-cta")
def execute_cta(
    action_item_id: int,
    payload: ExecuteCTARequest,
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict:
    try:
        result = engine.execute_alert_cta(
            session,
            action_item_id,
            action=payload.action,
            params=payload.params,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return result


@router.post("/{action_item_id}/dismiss")
def dismiss_alert(
    action_item_id: int,
    session: Session = Depends(get_session),
) -> dict:
    try:
        item = dismiss_action_item(session, action_item_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    session.commit()
    return {"id": item.id, "status": item.status}
