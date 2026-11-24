from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.agents.lex import (
    CopilotProposedAction,
    CopilotQueryInput,
    CopilotTurnContext,
)
from backend.core import clock
from backend.core.db import get_session
from backend.core.models import (
    ActionItem,
    CopilotMessage,
    CopilotSession,
    Part,
    PurchaseOrder,
)
from backend.orchestrator.copilot_actions import execute_copilot_actions
from backend.orchestrator.deps import get_workflow_engine
from backend.orchestrator.lex_tools import build_lex_tool_registry
from backend.orchestrator.workflows import WorkflowEngine

router = APIRouter(prefix="/api/copilot", tags=["copilot"])


class CreateSessionRequest(BaseModel):
    title: str | None = None


class CopilotQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    session_id: int | None = None
    revise_message_id: int | None = None


def _resolve_citations(
    session: Session,
    *,
    cited_skus: list[str],
    cited_po_numbers: list[str],
    cited_alert_ids: list[int],
) -> dict[str, list[dict[str, Any]]]:
    resolved_parts: list[dict[str, Any]] = []
    seen_part_ids: set[int] = set()
    for raw_sku in cited_skus:
        clean = raw_sku.strip()
        if not clean:
            continue
        part = session.scalar(select(Part).where(func.lower(Part.sku) == clean.lower()))
        if part is not None and part.id not in seen_part_ids:
            seen_part_ids.add(part.id)
            resolved_parts.append(
                {
                    "part_id": part.id,
                    "sku": part.sku,
                    "name": part.name,
                    "criticality": part.criticality,
                }
            )

    resolved_pos: list[dict[str, Any]] = []
    seen_po_ids: set[int] = set()
    for raw_po in cited_po_numbers:
        clean = raw_po.strip()
        if not clean:
            continue
        po = session.scalar(
            select(PurchaseOrder).where(func.lower(PurchaseOrder.po_number) == clean.lower())
        )
        if po is not None and po.id not in seen_po_ids:
            seen_po_ids.add(po.id)
            resolved_pos.append(
                {
                    "po_id": po.id,
                    "po_number": po.po_number,
                    "status": po.status,
                    "part_id": po.part_id,
                }
            )

    resolved_alerts: list[dict[str, Any]] = []
    seen_alert_ids: set[int] = set()
    for raw_aid in cited_alert_ids:
        try:
            aid = int(raw_aid)
        except (TypeError, ValueError):
            continue
        if aid in seen_alert_ids:
            continue
        alert = session.get(ActionItem, aid)
        if alert is not None:
            seen_alert_ids.add(alert.id)
            resolved_alerts.append(
                {
                    "alert_id": alert.id,
                    "title": alert.title,
                    "status": alert.status,
                    "severity": alert.severity,
                    "po_id": alert.po_id,
                    "part_id": alert.part_id,
                }
            )

    return {
        "resolved_parts": resolved_parts,
        "resolved_pos": resolved_pos,
        "resolved_alerts": resolved_alerts,
    }


def _serialize_message(session: Session, msg: CopilotMessage) -> dict[str, Any]:
    structured = dict(msg.structured_json or {})
    cited_skus = list(structured.get("cited_skus") or [])
    cited_pos = list(structured.get("cited_po_numbers") or [])
    cited_alerts = list(structured.get("cited_alert_ids") or [])
    resolved = (
        _resolve_citations(
            session,
            cited_skus=cited_skus,
            cited_po_numbers=cited_pos,
            cited_alert_ids=cited_alerts,
        )
        if msg.role == "assistant"
        else {"resolved_parts": [], "resolved_pos": [], "resolved_alerts": []}
    )
    return {
        "id": msg.id,
        "session_id": msg.session_id,
        "role": msg.role,
        "content": msg.content,
        "cited_skus": cited_skus,
        "cited_po_numbers": cited_pos,
        "cited_alert_ids": cited_alerts,
        "cited_metrics": list(structured.get("cited_metrics") or []),
        "proposed_actions": list(structured.get("proposed_actions") or []),
        "action_status": structured.get("action_status"),
        "action_execution_results": list(structured.get("action_execution_results") or []),
        "recommended_actions": list(structured.get("recommended_actions") or []),
        "cannot_verify_or_missing_tools": list(
            structured.get("cannot_verify_or_missing_tools") or []
        ),
        "visuals": list(structured.get("visuals") or []),
        "degraded": bool(structured.get("degraded", False)),
        "error_code": structured.get("error_code"),
        "error_detail": structured.get("error_detail"),
        "tool_calls": list(msg.tool_calls_json or []),
        "confidence": msg.confidence,
        "trace_id": msg.trace_id,
        "created_at": clock.ensure_utc(msg.created_at).isoformat(),
        **resolved,
    }


def _serialize_session_summary(session: Session, s: CopilotSession) -> dict[str, Any]:
    msg_count = session.scalar(
        select(func.count(CopilotMessage.id)).where(CopilotMessage.session_id == s.id)
    )
    return {
        "id": s.id,
        "title": s.title,
        "message_count": int(msg_count or 0),
        "created_at": clock.ensure_utc(s.created_at).isoformat(),
        "updated_at": clock.ensure_utc(s.updated_at).isoformat(),
    }


@router.get("/sessions")
def list_copilot_sessions(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    rows = list(
        session.scalars(
            select(CopilotSession).order_by(
                CopilotSession.updated_at.desc(), CopilotSession.id.desc()
            )
        ).all()
    )
    return [_serialize_session_summary(session, s) for s in rows]


@router.post("/sessions")
def create_copilot_session(
    payload: CreateSessionRequest | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    sim_now = clock.now(session)
    title = (payload.title.strip() if payload and payload.title else "") or "New Copilot Chat"
    row = CopilotSession(
        title=title[:200],
        created_at=sim_now,
        updated_at=sim_now,
    )
    session.add(row)
    session.commit()
    return {**_serialize_session_summary(session, row), "messages": []}


@router.get("/sessions/{session_id}")
def get_copilot_session(
    session_id: int,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    row = session.get(CopilotSession, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Copilot session {session_id} not found")
    msgs = list(
        session.scalars(
            select(CopilotMessage)
            .where(CopilotMessage.session_id == row.id)
            .order_by(CopilotMessage.id.asc())
        ).all()
    )
    return {
        **_serialize_session_summary(session, row),
        "messages": [_serialize_message(session, m) for m in msgs],
    }


@router.delete("/sessions/{session_id}")
def delete_copilot_session(
    session_id: int,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    row = session.get(CopilotSession, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Copilot session {session_id} not found")
    session.delete(row)
    session.commit()
    return {"deleted": True, "session_id": session_id}


@router.post("/query")
def query_copilot(
    payload: CopilotQueryRequest,
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict[str, Any]:
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question must not be empty.")

    sim_now = clock.now(session)
    chat_session: CopilotSession | None = None
    if payload.session_id is not None:
        chat_session = session.get(CopilotSession, payload.session_id)
        if chat_session is None:
            raise HTTPException(
                status_code=404, detail=f"Copilot session {payload.session_id} not found"
            )
    else:
        short_title = question[:72] + ("..." if len(question) > 72 else "")
        chat_session = CopilotSession(
            title=short_title,
            created_at=sim_now,
            updated_at=sim_now,
        )
        session.add(chat_session)
        session.flush()

    existing_msgs = list(
        session.scalars(
            select(CopilotMessage)
            .where(CopilotMessage.session_id == chat_session.id)
            .order_by(CopilotMessage.id.asc())
        ).all()
    )
    if not existing_msgs and chat_session.title == "New Copilot Chat":
        chat_session.title = question[:72] + ("..." if len(question) > 72 else "")

    revision_context: str | None = None
    if payload.revise_message_id is not None:
        target_msg = session.get(CopilotMessage, payload.revise_message_id)
        if target_msg is None or target_msg.session_id != chat_session.id:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Message {payload.revise_message_id} not found in session {chat_session.id}"
                ),
            )
        prior_struct = dict(target_msg.structured_json or {})
        prior_actions = list(prior_struct.get("proposed_actions") or [])
        prior_user_prompt = ""
        for prev in reversed(existing_msgs):
            if prev.id < target_msg.id and prev.role == "user":
                prior_user_prompt = prev.content
                break
        prior_struct["action_status"] = "REVISED"
        target_msg.structured_json = prior_struct
        action_lines = [
            f"- {a.get('action_type')}: {a.get('title')} — {a.get('summary')}"
            for a in prior_actions
        ]
        revision_context = (
            f"Original Manager Request: {prior_user_prompt}\n"
            f"Previously Proposed Actions (misunderstood or needing adjustment):\n"
            + ("\n".join(action_lines) if action_lines else "(none)")
            + f"\nManager Clarification / Desired Changes: {question}"
        )

    history_ctx: list[CopilotTurnContext] = []
    for m in existing_msgs[-8:]:
        st = dict(m.structured_json or {})
        history_ctx.append(
            CopilotTurnContext(
                role="user" if m.role == "user" else "assistant",
                content=m.content,
                cited_skus=list(st.get("cited_skus") or []),
                cited_po_numbers=list(st.get("cited_po_numbers") or []),
                proposed_actions_summary=[
                    f"{a.get('action_type')}: {a.get('title')}"
                    for a in (st.get("proposed_actions") or [])
                ],
                action_status=st.get("action_status"),
                timestamp=clock.ensure_utc(m.created_at).isoformat(),
            )
        )

    user_msg = CopilotMessage(
        session_id=chat_session.id,
        role="user",
        content=question,
        created_at=sim_now,
    )
    session.add(user_msg)
    session.flush()

    ro_registry = build_lex_tool_registry(session, read_only_only=True)
    outcome = engine.lex.answer_copilot_query(
        session,
        CopilotQueryInput(
            question=question,
            current_date=sim_now.date().isoformat(),
            conversation_history=history_ctx,
            revision_context=revision_context,
        ),
        tool_registry=ro_registry,
        linked_session_id=chat_session.id,
    )
    res = outcome.result
    proposed_list = [act.model_dump(mode="json") for act in res.proposed_actions]
    structured_payload: dict[str, Any] = {
        "cited_skus": list(res.cited_skus),
        "cited_po_numbers": list(res.cited_po_numbers),
        "cited_alert_ids": list(res.cited_alert_ids),
        "cited_metrics": [m.model_dump(mode="json") for m in res.cited_metrics],
        "proposed_actions": proposed_list,
        "action_status": "PENDING_CONFIRMATION" if proposed_list else None,
        "action_execution_results": [],
        "recommended_actions": list(res.recommended_actions),
        "cannot_verify_or_missing_tools": list(res.cannot_verify_or_missing_tools),
        "visuals": list(outcome.visuals),
        "degraded": outcome.degraded,
        "error_code": outcome.error_code,
        "error_detail": outcome.error_detail,
    }
    assistant_msg = CopilotMessage(
        session_id=chat_session.id,
        role="assistant",
        content=res.answer,
        structured_json=structured_payload,
        tool_calls_json=list(outcome.tool_calls),
        confidence=res.confidence,
        trace_id=outcome.trace_id,
        created_at=clock.now(session),
    )
    session.add(assistant_msg)
    chat_session.updated_at = clock.now(session)
    session.commit()

    return {
        "session": _serialize_session_summary(session, chat_session),
        "user_message": _serialize_message(session, user_msg),
        "assistant_message": _serialize_message(session, assistant_msg),
    }


@router.post("/messages/{message_id}/confirm-actions")
def confirm_copilot_actions(
    message_id: int,
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict[str, Any]:
    msg = session.get(CopilotMessage, message_id)
    if msg is None or msg.role != "assistant":
        raise HTTPException(status_code=404, detail=f"Copilot message {message_id} not found")

    structured = dict(msg.structured_json or {})
    status = structured.get("action_status")
    if status != "PENDING_CONFIRMATION":
        raise HTTPException(
            status_code=400,
            detail=f"Message {message_id} actions are not pending confirmation (status={status}).",
        )

    raw_actions = list(structured.get("proposed_actions") or [])
    actions = [CopilotProposedAction.model_validate(a) for a in raw_actions]
    try:
        receipts = execute_copilot_actions(session, engine, actions)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    structured["action_status"] = "EXECUTED"
    structured["action_execution_results"] = receipts
    msg.structured_json = structured
    if msg.session is not None:
        msg.session.updated_at = clock.now(session)
    session.commit()

    return {
        "message": _serialize_message(session, msg),
        "execution_results": receipts,
    }


@router.post("/messages/{message_id}/decline-actions")
def decline_copilot_actions(
    message_id: int,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    msg = session.get(CopilotMessage, message_id)
    if msg is None or msg.role != "assistant":
        raise HTTPException(status_code=404, detail=f"Copilot message {message_id} not found")

    structured = dict(msg.structured_json or {})
    if structured.get("action_status") != "PENDING_CONFIRMATION":
        raise HTTPException(
            status_code=400,
            detail=f"Message {message_id} actions are not pending confirmation.",
        )

    structured["action_status"] = "DECLINED"
    msg.structured_json = structured
    if msg.session is not None:
        msg.session.updated_at = clock.now(session)
    session.commit()
    return {"message": _serialize_message(session, msg)}
