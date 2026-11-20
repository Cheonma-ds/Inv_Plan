from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.core.db import get_session
from backend.inventory import PartNotFoundError
from backend.orchestrator.deps import get_workflow_engine
from backend.orchestrator.workflows import WorkflowEngine
from backend.planning import get_planning_overview

router = APIRouter(prefix="/api/planning", tags=["planning"])


class ApplyROPRequest(BaseModel):
    rop: int | None = Field(default=None, ge=0)
    replenishment_point: int | None = Field(default=None, ge=0)


@router.get("/overview")
def planning_overview(session: Session = Depends(get_session)) -> dict:
    summary = get_planning_overview(session)
    return summary.model_dump(mode="json")


@router.post("/recalculate")
def recalculate_planning(
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict:
    outcome = engine.recalculate_planning(session)
    session.commit()
    return {
        **outcome["summary"].model_dump(mode="json"),
        "alerts_created": outcome["alerts_created"],
        "alerts_updated": outcome["alerts_updated"],
        "alerts_resolved": outcome["alerts_resolved"],
    }


@router.post("/parts/{part_id}/apply-recommendation")
def apply_part_recommendation(
    part_id: int,
    payload: ApplyROPRequest | None = None,
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict:
    try:
        result = engine.apply_rop_recommendation(
            session,
            part_id=part_id,
            rop=payload.rop if payload else None,
            replenishment_point=payload.replenishment_point if payload else None,
        )
    except (PartNotFoundError, ValueError) as exc:
        msg = str(exc)
        status = 404 if "not found" in msg.lower() else 400
        raise HTTPException(status_code=status, detail=msg) from exc
    session.commit()
    return result
