import os

import pytest
from sqlalchemy import select

from backend.agents.lex import LexAgent, StockRequestIntent
from backend.agents.llm_client import GeminiLLMClient
from backend.core.config import settings
from backend.core.enums import PersonaKey, POStatus, StockRequestStatus
from backend.core.models import ActionItem, Part, PurchaseOrder, StockRequest
from backend.mailroom import OutboundMessage, list_drafts
from backend.observability import list_traces
from backend.orchestrator.workflows import WorkflowEngine


def _should_run_live_gemini() -> bool:
    has_key = bool(os.environ.get("GEMINI_API_KEY") or settings.gemini_api_key)
    enabled = os.environ.get("RUN_LIVE_GEMINI", "").strip().lower() in {"1", "true", "yes"}
    return has_key and enabled


@pytest.mark.skipif(
    not _should_run_live_gemini(),
    reason="Set RUN_LIVE_GEMINI=1 (with GEMINI_API_KEY) to run live Gemini stock request tests",
)
def test_live_gemini_making_shortage_min_qty_and_po_trigger_end_to_end(seeded_session) -> None:
    """Live Gemini end-to-end smoke test for Making stock request -> shortage -> min-qty -> PO."""
    part = seeded_session.scalar(select(Part).where(Part.sku == "SKU-001"))
    assert part is not None
    assert part.on_hand_stock == 4

    lex = LexAgent(llm_client=GeminiLLMClient(model_name=settings.gemini_model))
    engine = WorkflowEngine(lex_agent=lex)

    # 1. Making requests 10 units of SKU-001 (only 4 on hand) -> S3 shortage + clarification draft
    req_res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.MAKING,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            subject="Urgent: Need 10 units of SKU-001",
            body=(
                f"Hi Inventory team, please issue 10 units of {part.sku} "
                f"({part.name}) for Spindle Assembly Line 2 today."
            ),
        ),
    )
    triage = req_res["triage"]
    assert triage is not None
    assert triage["triage"].intent == StockRequestIntent.STOCK_REQUEST
    assert triage["triage"].confidence >= 0.85
    assert len(triage["stock_request_ids"]) == 1
    assert len(triage["draft_comm_ids"]) == 1

    seeded_session.refresh(part)
    assert part.on_hand_stock == 4  # Zero premature deduction

    shortage_alert_id = triage["alert_ids"][0]
    shortage_alert = seeded_session.get(ActionItem, shortage_alert_id)
    assert shortage_alert is not None and shortage_alert.type == "STOCK_SHORTAGE"

    # 2. Manager sends clarification draft asking Making for minimum qty
    ask_out = engine.execute_alert_cta(
        seeded_session, shortage_alert_id, action="ask_making_minimum"
    )
    assert ask_out["status"] == StockRequestStatus.AWAITING_MIN_QTY.value

    # 3. Making replies with minimum 2 units needed on same thread -> S4 + S2
    reply_res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.MAKING,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            thread_id=req_res["email"].thread_id,
            subject="Re: [SKU-001] Minimum Quantity Needed",
            body="We can manage with minimum 2 units right now until the next shipment arrives.",
        ),
    )
    reply_triage = reply_res["triage"]
    assert reply_triage is not None
    assert reply_triage["triage"].intent == StockRequestIntent.MINIMUM_QTY_CONFIRMATION
    assert reply_triage["triage"].confidence >= 0.85

    min_alert_id = reply_triage["alert_ids"][0]
    min_alert = seeded_session.get(ActionItem, min_alert_id)
    assert min_alert is not None
    assert min_alert.cta_payload_json["effective_qty"] == 2
    assert min_alert.cta_payload_json["will_trigger_po"] is True

    seeded_session.refresh(part)
    assert part.on_hand_stock == 4  # Still unchanged until Manager approves

    # 4. Manager approves withdrawal -> stock becomes 2, replenishment PO + draft created
    final_out = engine.execute_alert_cta(
        seeded_session, min_alert_id, action="approve_stock_withdrawal"
    )
    assert final_out["on_hand_after"] == 2
    assert final_out["created_po_id"] is not None
    assert final_out["draft_comm_id"] is not None

    po = seeded_session.get(PurchaseOrder, final_out["created_po_id"])
    assert po is not None and po.status == POStatus.PENDING_DISPATCH_APPROVAL.value
    drafts = list_drafts(seeded_session, PersonaKey.LEX.value)
    assert any(d.id == final_out["draft_comm_id"] for d in drafts)


@pytest.mark.skipif(
    not _should_run_live_gemini(),
    reason="Set RUN_LIVE_GEMINI=1 (with GEMINI_API_KEY) to run live Gemini stock request tests",
)
def test_live_gemini_vague_stock_request_escalates_ambiguous(seeded_session) -> None:
    part = seeded_session.scalar(select(Part).where(Part.sku == "SKU-001"))
    assert part is not None
    initial_stock = part.on_hand_stock
    sr_before = len(seeded_session.scalars(select(StockRequest)).all())

    lex = LexAgent(llm_client=GeminiLLMClient(model_name=settings.gemini_model))
    engine = WorkflowEngine(lex_agent=lex)

    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.MAKING,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            subject="Need some parts later",
            body="Hey, send over maybe 4 or 5 bearings or bolts whenever free.",
        ),
    )
    triage = res["triage"]
    assert triage is not None
    assert triage["triage"].intent == StockRequestIntent.AMBIGUOUS
    assert triage["stock_request_ids"] == []
    alert = seeded_session.get(ActionItem, triage["alert_ids"][0])
    assert alert is not None and alert.type == "AMBIGUOUS_STOCK_REQUEST"

    seeded_session.refresh(part)
    assert part.on_hand_stock == initial_stock
    assert len(seeded_session.scalars(select(StockRequest)).all()) == sr_before
    traces = list_traces(seeded_session, agent_name="lex", task_type="stock_request_triage")
    assert len(traces) >= 1 and traces[0].input_tokens > 0
