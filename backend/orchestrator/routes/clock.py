from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.db import get_session
from backend.orchestrator.deps import get_workflow_engine
from backend.orchestrator.workflows import WorkflowEngine

router = APIRouter(prefix="/api/clock", tags=["clock"])


class SetClockRequest(BaseModel):
    sim_now: str
    allow_reset: bool = False


class AdvanceClockRequest(BaseModel):
    hours: int = Field(default=0, ge=0)
    days: int = Field(default=0, ge=0)


class JumpEtaRequest(BaseModel):
    offset_days: int = 0


def _serialize_sla_events(events: list) -> list[dict]:
    return [
        {
            "po_id": e.po_id,
            "po_number": e.po_number,
            "event_type": e.event_type,
            "summary": e.summary,
            "draft_comm_id": e.draft_comm_id,
            "action_item_id": e.action_item_id,
        }
        for e in events
    ]


@router.get("")
def get_clock(session: Session = Depends(get_session)) -> dict:
    return {"sim_now": clock.now(session).isoformat()}


@router.post("")
def set_clock(
    payload: SetClockRequest,
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict:
    try:
        dt = datetime.fromisoformat(payload.sim_now.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        target_utc = dt.astimezone(timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid sim_now ISO-8601 timestamp") from exc

    try:
        result = engine.set_clock_and_watch(
            session,
            target_utc,
            allow_reset=payload.allow_reset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session.commit()
    return {
        "sim_now": result["sim_now"].isoformat(),
        "sla_events": _serialize_sla_events(result["sla_events"]),
    }


@router.post("/advance")
def advance_clock(
    payload: AdvanceClockRequest,
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict:
    if payload.hours == 0 and payload.days == 0:
        raise HTTPException(status_code=400, detail="Provide positive hours or days to advance.")
    result = engine.advance_clock_and_watch(session, hours=payload.hours, days=payload.days)
    session.commit()
    return {
        "sim_now": result["sim_now"].isoformat(),
        "sla_events": [
            {
                "po_id": e.po_id,
                "po_number": e.po_number,
                "event_type": e.event_type,
                "summary": e.summary,
                "draft_comm_id": e.draft_comm_id,
                "action_item_id": e.action_item_id,
            }
            for e in result["sla_events"]
        ],
    }


@router.post("/jump-eta/{po_id}")
def jump_to_po_eta(
    po_id: int,
    payload: JumpEtaRequest | None = None,
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict:
    offset_days = payload.offset_days if payload else 0
    try:
        result = engine.jump_to_po_eta_and_watch(session, po_id, offset_days=offset_days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return {
        "sim_now": result["sim_now"].isoformat(),
        "sla_events": [
            {
                "po_id": e.po_id,
                "po_number": e.po_number,
                "event_type": e.event_type,
                "summary": e.summary,
                "draft_comm_id": e.draft_comm_id,
                "action_item_id": e.action_item_id,
            }
            for e in result["sla_events"]
        ],
    }
