import json
from datetime import timedelta
from typing import TypeVar

from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select

from backend.agents.lex import (
    EmailDraftResult,
    LexAgent,
    StockRequestIntent,
    StockRequestItem,
    StockRequestTriageResult,
)
from backend.agents.llm_client import LLMResult
from backend.core import clock
from backend.core.db import get_session
from backend.core.enums import (
    ActionItemStatus,
    AlertSeverity,
    ApprovalOutcome,
    CommStatus,
    EmailCategory,
    InventoryTxType,
    PersonaKey,
    POStatus,
    StockRequestStatus,
)
from backend.core.models import (
    ActionItem,
    AutonomyPolicy,
    InventoryTransaction,
    Part,
    PurchaseOrder,
    StockRequest,
)
from backend.mailroom import OutboundMessage, list_drafts, list_inbox
from backend.main import create_app
from backend.observability import list_traces
from backend.orchestrator.deps import get_workflow_engine
from backend.orchestrator.workflows import WorkflowEngine

T = TypeVar("T", bound=BaseModel)


class MockStockRequestLLM:
    """Deterministic LLM stub for Making-team stock request triage & draft generation."""

    def __init__(self, next_triage: StockRequestTriageResult | None = None) -> None:
        self.next_triage = next_triage

    def generate_structured(
        self,
        *,
        prompt: str,
        response_schema: type[T],
        system_instruction: str | None = None,
        temperature: float = 0.0,
        pdf_bytes: bytes | None = None,
    ) -> LLMResult[T]:
        payload = json.loads(prompt)
        if response_schema is StockRequestTriageResult:
            assert self.next_triage is not None, "Set next_triage before stock request triage"
            res = self.next_triage
            return LLMResult(
                parsed=res,  # type: ignore[arg-type]
                raw_text=res.model_dump_json(),
                input_tokens=320,
                output_tokens=110,
                latency_ms=18,
                cost_inr=0.004,
                model_name="mock-gemini",
            )

        if response_schema is EmailDraftResult:
            cat = payload["category"]
            recipient = payload["recipient_persona"]
            po_num = (payload.get("po_number") or "").strip()
            sku = payload["part_sku"]
            subject = (
                f"[{po_num}] {cat.replace('_', ' ').title()}"
                if po_num
                else f"[{sku}] {cat.replace('_', ' ').title()}"
            )
            result = EmailDraftResult(
                to_personas=[recipient],
                cc_personas=["inventory_manager"],
                subject=subject,
                body=(
                    f"Hello {payload['recipient_name']}, regarding {sku} ({payload['part_name']}): "
                    f"requested {payload['qty']} units."
                ),
            )
            return LLMResult(
                parsed=result,  # type: ignore[arg-type]
                raw_text=result.model_dump_json(),
                input_tokens=280,
                output_tokens=85,
                latency_ms=15,
                cost_inr=0.003,
                model_name="mock-gemini",
            )

        raise AssertionError(f"Unexpected schema {response_schema}")


def test_http_making_to_manager_without_lex_returns_400_and_with_lex_triages(
    seeded_session,
) -> None:
    llm = MockStockRequestLLM(
        next_triage=StockRequestTriageResult(
            intent=StockRequestIntent.STOCK_REQUEST,
            confidence=0.95,
            requested_items=[
                StockRequestItem(
                    part_sku="BRG-402",
                    part_name="Precision Ball Bearing 6205",
                    requested_qty=2,
                )
            ],
            urgency_note="Spindle assembly line #1",
            summary_for_manager="Making Team requested 2 units of BRG-402.",
            suggested_action="Approve withdrawal and trigger replenishment PO.",
            reasoning="Clear BRG-402 request for 2 units.",
        )
    )
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))

    app = create_app()
    app.dependency_overrides[get_session] = lambda: seeded_session
    app.dependency_overrides[get_workflow_engine] = lambda: engine
    client = TestClient(app)

    # 1. Missing 'lex' in To/CC returns 400 Bad Request
    bad_resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": "making",
            "to_personas": "inventory_manager",
            "cc_personas": "",
            "subject": "Need BRG-402",
            "body": "Please issue 2 units of BRG-402.",
        },
    )
    assert bad_resp.status_code == 400
    assert "lex" in bad_resp.json()["detail"].lower()

    part = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    assert part is not None
    assert part.on_hand_stock == 4

    # 2. Including 'lex' in CC succeeds (200) without deducting stock prematurely
    ok_resp = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": "making",
            "to_personas": "inventory_manager",
            "cc_personas": "lex",
            "subject": "Need BRG-402",
            "body": "Please issue 2 units of BRG-402 for spindle assembly.",
        },
    )
    assert ok_resp.status_code == 200
    body = ok_resp.json()
    assert body["triage"]["triage"]["intent"] == StockRequestIntent.STOCK_REQUEST.value
    assert len(body["triage"]["stock_request_ids"]) == 1

    seeded_session.refresh(part)
    assert part.on_hand_stock == 4  # Crucial invariant: zero premature deduction!
    assert len(list_inbox(seeded_session, PersonaKey.LEX.value)) >= 1
    assert len(list_inbox(seeded_session, PersonaKey.INVENTORY_MANAGER.value)) >= 1


def test_scenario_s1_healthy_stock_approve_and_reject(seeded_session) -> None:
    part = seeded_session.scalar(select(Part).where(Part.sku == "VLV-204"))
    assert part is not None
    part.on_hand_stock = 20
    part.rop = 5
    part.replenishment_point = 15
    seeded_session.flush()

    llm = MockStockRequestLLM(
        next_triage=StockRequestTriageResult(
            intent=StockRequestIntent.STOCK_REQUEST,
            confidence=0.97,
            requested_items=[
                StockRequestItem(part_sku="VLV-204", part_name=part.name, requested_qty=3)
            ],
            summary_for_manager="Making Team requested 3 units of VLV-204.",
            suggested_action="Approve stock withdrawal.",
            reasoning="Clear SKU and quantity.",
        )
    )
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    po_count_before = len(seeded_session.scalars(select(PurchaseOrder)).all())

    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.MAKING,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            subject="Request VLV-204",
            body="Please issue 3 units of VLV-204.",
        ),
    )
    triage = res["triage"]
    assert triage is not None
    alert_id = triage["alert_ids"][0]
    sr_id = triage["stock_request_ids"][0]

    # Stock MUST remain 20 prior to Manager approval
    seeded_session.refresh(part)
    assert part.on_hand_stock == 20

    alert = seeded_session.get(ActionItem, alert_id)
    assert alert is not None
    assert alert.type == "STOCK_WITHDRAWAL_REQUEST"
    assert alert.cta_payload_json["will_trigger_po"] is False
    assert alert.cta_payload_json["projected_on_hand"] == 17

    # Manager approves S1 withdrawal -> stock decrements, no PO created
    cta_out = engine.execute_alert_cta(
        seeded_session, alert_id, action="approve_stock_withdrawal"
    )
    assert cta_out["status"] == StockRequestStatus.APPROVED.value
    assert cta_out["on_hand_after"] == 17
    assert cta_out["created_po_id"] is None

    seeded_session.refresh(part)
    assert part.on_hand_stock == 17
    assert len(seeded_session.scalars(select(PurchaseOrder)).all()) == po_count_before

    sr = seeded_session.get(StockRequest, sr_id)
    assert sr is not None and sr.approved_qty == 3 and sr.resolved_at is not None
    tx = seeded_session.scalar(
        select(InventoryTransaction)
        .where(InventoryTransaction.part_id == part.id)
        .order_by(InventoryTransaction.id.desc())
    )
    assert tx is not None and tx.type == InventoryTxType.CONSUMPTION.value and tx.qty_delta == -3

    # Test reject_stock_request path leaves stock untouched at 17
    res2 = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.MAKING,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            subject="Another VLV-204 request",
            body="Please issue 3 units of VLV-204.",
        ),
    )
    alert2_id = res2["triage"]["alert_ids"][0]
    sr2_id = res2["triage"]["stock_request_ids"][0]
    rej_out = engine.execute_alert_cta(
        seeded_session,
        alert2_id,
        action="reject_stock_request",
        params={"note": "Duplicate line request"},
    )
    assert rej_out["status"] == StockRequestStatus.REJECTED.value
    seeded_session.refresh(part)
    assert part.on_hand_stock == 17
    sr2 = seeded_session.get(StockRequest, sr2_id)
    assert sr2 is not None and sr2.approved_qty == 0


def test_scenario_s2_sufficient_stock_triggers_po_only_after_approval(seeded_session) -> None:
    # Hero part BRG-402: on_hand=4, rop=3, S=6 -> withdrawing 2 drops IP to 2 (<3) -> PO for 4
    part = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    assert part is not None
    assert part.on_hand_stock == 4

    llm = MockStockRequestLLM(
        next_triage=StockRequestTriageResult(
            intent=StockRequestIntent.STOCK_REQUEST,
            confidence=0.96,
            requested_items=[
                StockRequestItem(part_sku="BRG-402", part_name=part.name, requested_qty=2)
            ],
            summary_for_manager="Making Team requested 2 units of BRG-402.",
            suggested_action="Approve withdrawal and trigger replenishment PO.",
            reasoning="Clear BRG-402 request.",
        )
    )
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    po_count_before = len(seeded_session.scalars(select(PurchaseOrder)).all())

    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.MAKING,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            subject="Issue 2 units BRG-402",
            body="Need 2 units of BRG-402 for assembly.",
        ),
    )
    alert_id = res["triage"]["alert_ids"][0]
    alert = seeded_session.get(ActionItem, alert_id)
    assert alert is not None
    assert alert.type == "STOCK_WITHDRAWAL_REQUEST"
    assert alert.cta_payload_json["will_trigger_po"] is True
    assert alert.cta_payload_json["projected_order_qty"] == 4
    assert alert.cta_payload_json["primary_supplier_name"] is not None

    # Before Manager approval: stock MUST still be 4, zero new POs!
    seeded_session.refresh(part)
    assert part.on_hand_stock == 4
    assert len(seeded_session.scalars(select(PurchaseOrder)).all()) == po_count_before

    # Manager approves -> stock drops to 2, PO created in PENDING_DISPATCH_APPROVAL + Lex draft
    cta_out = engine.execute_alert_cta(
        seeded_session, alert_id, action="approve_stock_withdrawal"
    )
    assert cta_out["status"] == StockRequestStatus.APPROVED.value
    assert cta_out["on_hand_after"] == 2
    assert cta_out["created_po_id"] is not None
    assert cta_out["draft_comm_id"] is not None

    seeded_session.refresh(part)
    assert part.on_hand_stock == 2
    po = seeded_session.get(PurchaseOrder, cta_out["created_po_id"])
    assert po is not None
    assert po.status == POStatus.PENDING_DISPATCH_APPROVAL.value
    assert po.qty == 4

    drafts = list_drafts(seeded_session, PersonaKey.LEX.value)
    assert any(
        d.id == cta_out["draft_comm_id"] and d.status == CommStatus.DRAFT_PENDING_APPROVAL.value
        for d in drafts
    )


def test_scenario_s3_and_s4_shortage_min_qty_loop_and_partial_issue(seeded_session) -> None:
    part = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    assert part is not None
    part.on_hand_stock = 2
    seeded_session.flush()

    llm = MockStockRequestLLM(
        next_triage=StockRequestTriageResult(
            intent=StockRequestIntent.STOCK_REQUEST,
            confidence=0.95,
            requested_items=[
                StockRequestItem(part_sku="BRG-402", part_name=part.name, requested_qty=10)
            ],
            urgency_note="Line 2 batch build",
            summary_for_manager="Making Team requested 10 units of BRG-402 (only 2 on hand).",
            suggested_action="Ask Making Team for minimum required quantity.",
            reasoning="Requested 10 units of BRG-402.",
        )
    )
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))

    # 1. Making requests 10 units when only 2 are on hand -> Shortage alert + clarification draft
    req_res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.MAKING,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            subject="Need 10 units BRG-402",
            body="Please issue 10 units of BRG-402 for Line 2.",
        ),
    )
    thread_id = req_res["email"].thread_id
    sr_id = req_res["triage"]["stock_request_ids"][0]
    shortage_alert_id = req_res["triage"]["alert_ids"][0]
    draft_id = req_res["triage"]["draft_comm_ids"][0]

    shortage_alert = seeded_session.get(ActionItem, shortage_alert_id)
    assert shortage_alert is not None
    assert shortage_alert.type == "STOCK_SHORTAGE"
    assert shortage_alert.draft_comm_id == draft_id
    assert shortage_alert.cta_payload_json["shortfall_qty"] == 8
    seeded_session.refresh(part)
    assert part.on_hand_stock == 2

    # 2. Manager approves sending min-qty inquiry to Making
    ask_out = engine.execute_alert_cta(
        seeded_session, shortage_alert_id, action="ask_making_minimum"
    )
    assert ask_out["status"] == StockRequestStatus.AWAITING_MIN_QTY.value
    assert ask_out["approval_outcome"] == ApprovalOutcome.APPROVED_AS_IS.value

    making_inbox = list_inbox(seeded_session, PersonaKey.MAKING.value)
    assert any(m.id == draft_id and m.thread_id == thread_id for m in making_inbox)
    policy = seeded_session.scalar(
        select(AutonomyPolicy).where(
            AutonomyPolicy.agent_name == "lex",
            AutonomyPolicy.action_category == EmailCategory.STOCK_REQUEST_CLARIFICATION.value,
        )
    )
    assert policy is not None and policy.approved_as_is_count == 1

    # 3. Making replies on thread confirming minimum 2 units needed
    llm.next_triage = StockRequestTriageResult(
        intent=StockRequestIntent.MINIMUM_QTY_CONFIRMATION,
        confidence=0.94,
        requested_items=[
            StockRequestItem(part_sku="BRG-402", part_name=part.name, requested_qty=2)
        ],
        summary_for_manager="Making Team confirmed minimum 2 units of BRG-402 needed.",
        suggested_action="Approve and issue 2 units of BRG-402.",
        reasoning="Making confirmed min 2 units on thread.",
    )
    reply_res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.MAKING,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            thread_id=thread_id,
            subject="Re: [BRG-402] Clarification",
            body="Minimum 2 units needed to keep the line running.",
        ),
    )
    min_alert_id = reply_res["triage"]["alert_ids"][0]
    min_alert = seeded_session.get(ActionItem, min_alert_id)
    assert min_alert is not None
    assert min_alert.type == "STOCK_WITHDRAWAL_REQUEST"
    assert min_alert.cta_payload_json["effective_qty"] == 2
    assert min_alert.cta_payload_json["is_min_qty_confirmation"] is True

    # Stock MUST still be 2 until Manager approves!
    seeded_session.refresh(part)
    assert part.on_hand_stock == 2

    # 4. Manager approves issuing 2 units -> stock hits 0, StockRequest PARTIALLY_APPROVED (2/10)
    final_out = engine.execute_alert_cta(
        seeded_session, min_alert_id, action="approve_stock_withdrawal"
    )
    assert final_out["on_hand_after"] == 0
    assert final_out["approved_qty"] == 2
    assert final_out["status"] == StockRequestStatus.PARTIALLY_APPROVED.value

    sr = seeded_session.get(StockRequest, sr_id)
    assert sr is not None
    assert sr.status == StockRequestStatus.PARTIALLY_APPROVED.value
    assert sr.approved_qty == 2

    # 5. Also verify direct issue_partial_stock on a shortage alert rejects unused draft
    part.on_hand_stock = 3
    seeded_session.flush()
    llm.next_triage = StockRequestTriageResult(
        intent=StockRequestIntent.STOCK_REQUEST,
        confidence=0.95,
        requested_items=[
            StockRequestItem(part_sku="BRG-402", part_name=part.name, requested_qty=7)
        ],
        summary_for_manager="Making Team requested 7 units of BRG-402 (3 on hand).",
        suggested_action="Issue 3 available units or ask for minimum qty.",
        reasoning="Clear shortage request.",
    )
    partial_req = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.MAKING,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            subject="Need 7 units BRG-402",
            body="Please send 7 units of BRG-402.",
        ),
    )
    partial_alert_id = partial_req["triage"]["alert_ids"][0]
    unused_draft_id = partial_req["triage"]["draft_comm_ids"][0]
    partial_out = engine.execute_alert_cta(
        seeded_session, partial_alert_id, action="issue_partial_stock"
    )
    assert partial_out["approved_qty"] == 3
    assert partial_out["on_hand_after"] == 0
    assert partial_out["status"] == StockRequestStatus.PARTIALLY_APPROVED.value
    unused_draft = [
        d for d in list_drafts(seeded_session, PersonaKey.LEX.value) if d.id == unused_draft_id
    ]
    assert unused_draft == []


def test_scenario_s5_accelerated_burn_triggered_on_zero_stock_with_far_po(seeded_session) -> None:
    part = seeded_session.scalar(select(Part).where(Part.sku == "VLV-204"))
    assert part is not None
    part.on_hand_stock = 2
    part.rop = 1
    part.replenishment_point = 10
    seeded_session.flush()

    sim_now = clock.now(seeded_session)
    open_po = seeded_session.scalar(
        select(PurchaseOrder).where(
            PurchaseOrder.part_id == part.id,
            PurchaseOrder.status.notin_((POStatus.CLOSED.value, POStatus.CANCELLED.value)),
        )
    )
    if open_po is None:
        # Ensure an open PO exists with ETA > sim_now + 3d
        from backend.po import create_po

        sup_id = part.supplier_terms[0].supplier_id
        open_po = create_po(
            seeded_session,
            part_id=part.id,
            supplier_id=sup_id,
            qty=15,
            unit_price=part.supplier_terms[0].agreed_unit_price,
            initial_status=POStatus.ACKNOWLEDGED,
        )
        open_po.promised_eta = sim_now + timedelta(days=10)
    else:
        open_po.promised_eta = sim_now + timedelta(days=10)
        open_po.revised_eta = None
    seeded_session.flush()

    llm = MockStockRequestLLM(
        next_triage=StockRequestTriageResult(
            intent=StockRequestIntent.STOCK_REQUEST,
            confidence=0.96,
            requested_items=[
                StockRequestItem(part_sku="VLV-204", part_name=part.name, requested_qty=2)
            ],
            summary_for_manager="Making Team requested final 2 units of VLV-204.",
            suggested_action="Approve withdrawal.",
            reasoning="Clear VLV-204 request.",
        )
    )
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.MAKING,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            subject="Need 2 units VLV-204",
            body="Please issue 2 units of VLV-204.",
        ),
    )
    alert_id = res["triage"]["alert_ids"][0]
    cta_out = engine.execute_alert_cta(
        seeded_session, alert_id, action="approve_stock_withdrawal"
    )
    assert cta_out["on_hand_after"] == 0
    created_alerts = [seeded_session.get(ActionItem, aid) for aid in cta_out["alert_ids"]]
    burn_alerts = [a for a in created_alerts if a is not None and a.type == "ACCELERATED_BURN"]
    assert len(burn_alerts) == 1
    assert burn_alerts[0].severity == AlertSeverity.CRITICAL.value
    assert burn_alerts[0].status == ActionItemStatus.OPEN.value


def test_low_confidence_and_unknown_sku_guardrails_zero_mutation(seeded_session) -> None:
    part = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    assert part is not None
    initial_on_hand = part.on_hand_stock
    sr_count_before = len(seeded_session.scalars(select(StockRequest)).all())
    po_count_before = len(seeded_session.scalars(select(PurchaseOrder)).all())

    # 1. Confidence < 0.85 forces AMBIGUOUS_STOCK_REQUEST
    llm = MockStockRequestLLM(
        next_triage=StockRequestTriageResult(
            intent=StockRequestIntent.STOCK_REQUEST,
            confidence=0.62,
            requested_items=[
                StockRequestItem(part_sku="BRG-402", part_name=part.name, requested_qty=2)
            ],
            summary_for_manager="Uncertain request for bearings.",
            suggested_action="Clarify SKU and quantity manually.",
            reasoning="Vague wording.",
        )
    )
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    res1 = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.MAKING,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            subject="Maybe send bearings",
            body="Send maybe 2 bearings or something.",
        ),
    )
    triage1 = res1["triage"]
    assert triage1["triage"].intent == StockRequestIntent.AMBIGUOUS
    assert triage1["stock_request_ids"] == []
    alert1 = seeded_session.get(ActionItem, triage1["alert_ids"][0])
    assert alert1 is not None
    assert alert1.type == "AMBIGUOUS_STOCK_REQUEST"
    assert alert1.severity == AlertSeverity.HIGH.value

    # 2. Unknown SKU with high confidence STILL forces AMBIGUOUS_STOCK_REQUEST
    llm.next_triage = StockRequestTriageResult(
        intent=StockRequestIntent.STOCK_REQUEST,
        confidence=0.95,
        requested_items=[
            StockRequestItem(part_sku="SKU-999", part_name="Nonexistent Widget", requested_qty=5)
        ],
        summary_for_manager="Making Team asked for unknown SKU-999.",
        suggested_action="Verify SKU with Making Team.",
        reasoning="Extracted SKU-999.",
    )
    res2 = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.MAKING,
            to_personas=[PersonaKey.INVENTORY_MANAGER],
            cc_personas=[PersonaKey.LEX],
            subject="Need SKU-999",
            body="Please issue 5 units of SKU-999.",
        ),
    )
    triage2 = res2["triage"]
    assert triage2["triage"].intent == StockRequestIntent.AMBIGUOUS
    assert "Guardrail Escalation" in triage2["triage"].reasoning
    assert triage2["stock_request_ids"] == []
    alert2 = seeded_session.get(ActionItem, triage2["alert_ids"][0])
    assert alert2 is not None and alert2.type == "AMBIGUOUS_STOCK_REQUEST"

    # Zero stock, StockRequest, or PO mutation occurred
    seeded_session.refresh(part)
    assert part.on_hand_stock == initial_on_hand
    assert len(seeded_session.scalars(select(StockRequest)).all()) == sr_count_before
    assert len(seeded_session.scalars(select(PurchaseOrder)).all()) == po_count_before

    traces = list_traces(seeded_session, agent_name="lex", limit=20)
    assert any(t.task_type == "stock_request_triage" for t in traces)
