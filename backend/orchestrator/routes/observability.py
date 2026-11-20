from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.db import get_session
from backend.observability import list_autonomy_policies, list_traces

router = APIRouter(prefix="/api/observability", tags=["observability"])


@router.get("/traces")
def get_traces(
    agent_name: str | None = Query(default=None),
    task_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[dict]:
    rows = list_traces(session, agent_name=agent_name, task_type=task_type, limit=limit)
    return [
        {
            "id": r.id,
            "agent_name": r.agent_name,
            "task_type": r.task_type,
            "trigger_summary": r.trigger_summary,
            "linked_entity_type": r.linked_entity_type,
            "linked_entity_id": r.linked_entity_id,
            "input_summary": r.input_summary,
            "output_summary": r.output_summary,
            "confidence": r.confidence,
            "tool_calls": r.tool_calls_json or [],
            "raw_prompt": r.raw_prompt,
            "raw_response": r.raw_response,
            "latency_ms": r.latency_ms,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "cost_inr": r.cost_inr,
            "timestamp": clock.ensure_utc(r.timestamp).isoformat(),
        }
        for r in rows
    ]


@router.get("/autonomy")
def get_autonomy_policies(
    agent_name: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> list[dict]:
    rows = list_autonomy_policies(session, agent_name=agent_name)
    out: list[dict] = []
    for p in rows:
        total = p.approved_as_is_count + p.approved_edited_count + p.rejected_count
        edit_free_rate = round((p.approved_as_is_count / total) * 100.0, 1) if total > 0 else None
        out.append(
            {
                "id": p.id,
                "agent_name": p.agent_name,
                "action_category": p.action_category,
                "mode": p.mode,
                "approved_as_is_count": p.approved_as_is_count,
                "approved_edited_count": p.approved_edited_count,
                "rejected_count": p.rejected_count,
                "total_decisions": total,
                "edit_free_approval_rate": edit_free_rate,
                "updated_at": clock.ensure_utc(p.updated_at).isoformat(),
            }
        )
    return out
