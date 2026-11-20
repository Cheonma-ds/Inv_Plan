from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.agents.lex import LexAgent, LexEvalExecutor
from backend.core import clock
from backend.core.db import get_session
from backend.observability import (
    AgentEvalExecutor,
    get_phoenix_status,
    list_autonomy_policies,
    list_eval_runs,
    list_traces,
    load_eval_suite,
    run_eval_suite,
    serialize_eval_run,
)

router = APIRouter(tags=["observability"])

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class EvalRunRequest(BaseModel):
    mode: Literal["offline", "live"] = "offline"


def _get_agent_eval_config(agent_name: str) -> tuple[AgentEvalExecutor, Path]:
    if agent_name == LexAgent.agent_name:
        suite_rel = LexAgent.eval_suite_path or "backend/evals/lex.yaml"
        return LexEvalExecutor(), PROJECT_ROOT / suite_rel
    raise HTTPException(
        status_code=404,
        detail=f"No eval suite registered for agent '{agent_name}'.",
    )


@router.get("/api/observability/status")
def get_observability_status() -> dict:
    return {
        "phoenix": get_phoenix_status(),
        "registered_agents": [LexAgent.agent_name],
    }


@router.get("/api/observability/traces")
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
            "human_feedback": r.human_feedback,
            "timestamp": clock.ensure_utc(r.timestamp).isoformat(),
        }
        for r in rows
    ]


@router.get("/api/observability/autonomy")
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


@router.get("/api/evals/{agent_name}/suite")
def get_eval_suite_metadata(agent_name: str) -> dict:
    _, suite_path = _get_agent_eval_config(agent_name)
    suite = load_eval_suite(suite_path)
    return {
        "agent_name": suite.agent_name,
        "suite_name": suite.suite_name,
        "description": suite.description,
        "total_cases": len(suite.cases),
        "cases": [
            {
                "id": c.id,
                "name": c.name,
                "task": c.task,
                "tags": c.tags,
            }
            for c in suite.cases
        ],
    }


@router.get("/api/evals/{agent_name}/run")
def get_eval_runs(
    agent_name: str,
    limit: int = Query(default=10, ge=1, le=50),
    session: Session = Depends(get_session),
) -> list[dict]:
    _get_agent_eval_config(agent_name)
    rows = list_eval_runs(session, agent_name=agent_name, limit=limit)
    return [serialize_eval_run(row) for row in rows]


@router.post("/api/evals/{agent_name}/run")
def execute_eval_run(
    agent_name: str,
    payload: EvalRunRequest | None = Body(default=None),
    session: Session = Depends(get_session),
) -> dict:
    executor, suite_path = _get_agent_eval_config(agent_name)
    mode = payload.mode if payload is not None else "offline"
    report = run_eval_suite(
        session,
        agent_name=agent_name,
        suite_path=suite_path,
        executor=executor,
        mode=mode,
    )
    session.commit()
    return report.model_dump(mode="json")
