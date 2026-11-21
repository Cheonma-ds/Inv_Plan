import json
from typing import TypeVar

from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select

from backend.agents.lex import (
    EmailDraftResult,
    EmailIntent,
    EmailTriageResult,
    LexAgent,
)
from backend.agents.llm_client import LLMResult
from backend.core.db import get_session
from backend.core.enums import (
    ActionItemStatus,
    ApprovalOutcome,
    CommStatus,
    Criticality,
    EmailCategory,
    PersonaKey,
    POStatus,
    SupplierMemorySource,
)
from backend.core.models import (
    ActionItem,
    AutonomyPolicy,
    Part,
    POCommunication,
    PurchaseOrder,
    Supplier,
    SupplierMemoryNote,
)
from backend.inventory import get_on_order_stock
from backend.mailroom import OutboundMessage, list_drafts, list_inbox
from backend.main import create_app
from backend.orchestrator.deps import get_workflow_engine
from backend.orchestrator.workflows import WorkflowEngine
from backend.po import record_goods_receipt, submit_invoice_and_match

T = TypeVar("T", bound=BaseModel)


class SmartMockLLM:
    """Deterministic mock LLM covering all email triage and draft scenarios."""

    def __init__(self, next_triage: EmailTriageResult | None = None) -> None:
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
        if response_schema is EmailDraftResult:
            po_num = payload["po_number"]
            cat = payload["category"]
            recipient = payload["recipient_persona"]
            result = EmailDraftResult(
                to_personas=[recipient],
                cc_personas=["inventory_manager"],
                subject=f"[{po_num}] {cat.replace('_', ' ').title()}",
                body=(
                    f"Hello {payload['recipient_name']}, regarding {po_num} "
                    f"({payload['part_sku']}): {cat}."
                ),
            )
            return LLMResult(
                parsed=result,  # type: ignore[arg-type]
                raw_text=result.model_dump_json(),
                input_tokens=300,
                output_tokens=90,
                latency_ms=20,
                cost_inr=0.004,
                model_name="mock-gemini",
            )

        if response_schema is EmailTriageResult:
            assert self.next_triage is not None, "Set mock_llm.next_triage before triage"
            res = self.next_triage
            return LLMResult(
                parsed=res,  # type: ignore[arg-type]
                raw_text=res.model_dump_json(),
                input_tokens=350,
                output_tokens=120,
                latency_ms=25,
                cost_inr=0.005,
                model_name="mock-gemini",
            )

        raise AssertionError(f"Unexpected schema {response_schema}")


def test_withdraw_to_draft_approve_dispatch_and_autonomy_tracking(seeded_session):
    llm = SmartMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))

    # Hero part SKU-001: on_hand=4, rop=3, S=6 -> withdraw 2 => on_hand=2 < 3 => order 4
    part = seeded_session.scalar(select(Part).where(Part.sku == "SKU-001"))
    assert part is not None
    res = engine.withdraw_stock(seeded_session, part_id=part.id, qty=2, note="Spindle repair")
    po = res["created_po"]
    draft = res["draft_email"]
    assert po is not None
    assert po.status == POStatus.PENDING_DISPATCH_APPROVAL.value
    assert po.qty == 4
    assert draft is not None
    assert draft.status == CommStatus.DRAFT_PENDING_APPROVAL.value
    assert get_on_order_stock(seeded_session, part.id) == 4

    # Second withdrawal must NOT raise a duplicate PO
    res2 = engine.withdraw_stock(seeded_session, part_id=part.id, qty=1, note="Line 2")
    assert res2["created_po"] is None

    # Approve Lex draft as-is -> transitions PO to SENT_AWAITING_ACK & updates autonomy counter
    approved = engine.approve_lex_draft(seeded_session, draft.id)
    assert approved["approval_outcome"] == ApprovalOutcome.APPROVED_AS_IS.value
    assert approved["po_transitioned_to"] == POStatus.SENT_AWAITING_ACK.value

    seeded_session.refresh(po)
    assert po.status == POStatus.SENT_AWAITING_ACK.value
    assert po.sent_at is not None

    policy = seeded_session.scalar(
        select(AutonomyPolicy).where(
            AutonomyPolicy.agent_name == "lex",
            AutonomyPolicy.action_category == EmailCategory.PO_DISPATCH.value,
        )
    )
    assert policy is not None
    assert policy.approved_as_is_count == 1

    alert = seeded_session.scalar(
        select(ActionItem).where(ActionItem.draft_comm_id == draft.id)
    )
    assert alert is not None and alert.status == ActionItemStatus.RESOLVED.value


def test_inbound_ack_price_change_post_ack_delay_and_ambiguous_guardrail(seeded_session):
    llm = SmartMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))

    # Grab the seeded SENT_AWAITING_ACK PO (sup3) and ACKNOWLEDGED PO (sup1)
    po_awaiting = seeded_session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.status == POStatus.SENT_AWAITING_ACK.value)
    )
    po_ack = seeded_session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.status == POStatus.ACKNOWLEDGED.value)
    )
    assert po_awaiting is not None and po_ack is not None

    # Promote po_awaiting's part to CRITICAL so post-ack delay exercises CRITICAL path
    part_await = seeded_session.get(Part, po_awaiting.part_id)
    assert part_await is not None
    part_await.criticality = Criticality.CRITICAL.value
    seeded_session.flush()

    # 1. Scenario 1: Supplier acknowledges po_awaiting
    llm.next_triage = EmailTriageResult(
        matched_po_number=po_awaiting.po_number,
        intent=EmailIntent.ACKNOWLEDGED,
        confidence=0.96,
        proposed_eta_date="2026-12-04",
        summary_for_manager=f"Deccan confirmed {po_awaiting.po_number} for 2026-12-04.",
        suggested_action="Track delivery.",
        new_memory_note="Prefers PO numbers in subject brackets for fast warehouse routing.",
        reasoning="Clear confirmation.",
    )
    engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP3,
            to_personas=[PersonaKey.LEX],
            cc_personas=[PersonaKey.INVENTORY_MANAGER],
            subject=f"Re: {po_awaiting.po_number}",
            body="Confirmed! Delivering all units by Dec 4.",
            po_id=po_awaiting.id,
        ),
    )
    seeded_session.refresh(po_awaiting)
    assert po_awaiting.status == POStatus.ACKNOWLEDGED.value
    assert po_awaiting.promised_eta is not None
    original_promised = po_awaiting.promised_eta

    learned = seeded_session.scalars(
        select(SupplierMemoryNote).where(
            SupplierMemoryNote.supplier_id == po_awaiting.supplier_id,
            SupplierMemoryNote.source == SupplierMemorySource.LEX_LEARNED.value,
        )
    ).all()
    assert len(learned) == 1

    # 2. Scenario 1A: Post-ack delay on CRITICAL part -> DELAYED_CRITICAL + EXPEDITE draft
    llm.next_triage = EmailTriageResult(
        matched_po_number=po_awaiting.po_number,
        intent=EmailIntent.POST_ACK_DELAY,
        confidence=0.93,
        proposed_eta_date="2026-12-18",
        delay_reason="Customs hold on sub-assemblies",
        summary_for_manager=f"Deccan delayed {po_awaiting.po_number} to 2026-12-18.",
        suggested_action="Escalate to Procurement to expedite.",
        reasoning="Post-ack delay on already acknowledged PO.",
    )
    delay_res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP3,
            to_personas=[PersonaKey.LEX],
            subject=f"Delay on {po_awaiting.po_number}",
            body="Customs delayed our components; new delivery date is Dec 18.",
        ),
    )
    seeded_session.refresh(po_awaiting)
    assert po_awaiting.status == POStatus.DELAYED_CRITICAL.value
    assert po_awaiting.promised_eta == original_promised  # immutable!
    assert po_awaiting.revised_eta is not None
    assert delay_res["triage"]["draft_comm_id"] is not None

    delay_alert = seeded_session.get(ActionItem, delay_res["triage"]["alert_id"])
    assert delay_alert is not None
    assert delay_alert.assigned_team == "Procurement"
    assert delay_alert.type == "CRITICAL_DELAY"

    # 3. Scenario 2: Price change on a SENT_AWAITING_ACK PO -> EXCEPTION_REVIEW + update_po CTA
    po_exc = engine.withdraw_stock(
        seeded_session,
        part_id=seeded_session.scalar(select(Part.id).where(Part.sku == "SKU-001")),
        qty=2,
    )["created_po"]
    assert po_exc is not None
    draft_exc = list_drafts(seeded_session, PersonaKey.LEX.value)[0]
    engine.approve_lex_draft(seeded_session, draft_exc.id)
    seeded_session.refresh(po_exc)
    assert po_exc.status == POStatus.SENT_AWAITING_ACK.value

    llm.next_triage = EmailTriageResult(
        matched_po_number=po_exc.po_number,
        intent=EmailIntent.PRICE_CHANGE,
        confidence=0.94,
        proposed_unit_price=1350.0,
        summary_for_manager=f"Apex requests ₹1350/unit vs ₹1200/unit on {po_exc.po_number}.",
        suggested_action="Accept revised price or switch to backup supplier.",
        reasoning="Explicit price revision request.",
    )
    exc_res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP1,
            to_personas=[PersonaKey.LEX],
            subject=f"Price update on {po_exc.po_number}",
            body="Raw material costs rose; new unit price is INR 1350.",
        ),
    )
    seeded_session.refresh(po_exc)
    assert po_exc.status == POStatus.EXCEPTION_REVIEW.value
    exc_alert_id = exc_res["triage"]["alert_id"]
    assert exc_alert_id is not None

    # Execute inline CTA: accept updated unit price -> creates REVISED_PO draft
    cta_res = engine.execute_alert_cta(
        seeded_session,
        exc_alert_id,
        action="update_po",
        params={"unit_price": 1350.0, "note": "Approved ₹1350 after market check"},
    )
    seeded_session.refresh(po_exc)
    assert po_exc.unit_price == 1350.0
    assert po_exc.status == POStatus.PENDING_DISPATCH_APPROVAL.value
    assert cta_res["draft_comm_id"] is not None

    # 4. Low-confidence guardrail -> AMBIGUOUS with ZERO PO state mutation
    llm.next_triage = EmailTriageResult(
        matched_po_number=po_exc.po_number,
        intent=EmailIntent.OUT_OF_STOCK,
        confidence=0.61,
        summary_for_manager="Unclear message about stock.",
        suggested_action="Review manually.",
        reasoning="Ambiguous wording.",
    )
    amb_res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP1,
            to_personas=[PersonaKey.LEX],
            subject="Quick note",
            body="Might have an issue next week, let's talk.",
        ),
    )
    seeded_session.refresh(po_exc)
    assert po_exc.status == POStatus.PENDING_DISPATCH_APPROVAL.value  # unchanged!
    amb_alert = seeded_session.get(ActionItem, amb_res["triage"]["alert_id"])
    assert amb_alert is not None and amb_alert.type == "AMBIGUOUS_EMAIL"


def test_sla_watcher_ack_escalation_pre_eta_overdue_and_quarantine(seeded_session):
    llm = SmartMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))

    po_await = seeded_session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.status == POStatus.SENT_AWAITING_ACK.value)
    )
    po_ack = seeded_session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.status == POStatus.ACKNOWLEDGED.value)
    )
    assert po_await is not None and po_ack is not None

    # Make po_ack's supplier reliability < 75 so Pre-ETA Check-in triggers 3 days before ETA
    sup_ack = seeded_session.get(Supplier, po_ack.supplier_id)
    assert sup_ack is not None
    sup_ack.reliability_score = 60.0
    # Make po_await CRITICAL (24h SLA; already sent 24h ago in seed)
    part_await = seeded_session.get(Part, po_await.part_id)
    assert part_await is not None
    part_await.criticality = Criticality.CRITICAL.value
    seeded_session.flush()

    # Step 1: Advance +25h -> po_await hits Breach 1; po_ack (ETA in 4d -> now 3d) hits Pre-ETA!
    r1 = engine.advance_clock_and_watch(seeded_session, hours=25)
    event_types_1 = {(e.po_number, e.event_type) for e in r1["sla_events"]}
    assert (po_await.po_number, "ACK_REMINDER_DRAFTED") in event_types_1
    assert (po_ack.po_number, "PRE_ETA_CHECKIN_DRAFTED") in event_types_1
    seeded_session.refresh(po_await)
    seeded_session.refresh(po_ack)
    assert po_await.follow_up_count == 1
    assert po_ack.pre_eta_checked is True

    # Step 2: Advance +24h -> po_await hits Breach 2 (follow-up #2)
    r2 = engine.advance_clock_and_watch(seeded_session, hours=24)
    assert any(
        e.po_number == po_await.po_number and e.event_type == "ACK_REMINDER_DRAFTED"
        for e in r2["sla_events"]
    )
    seeded_session.refresh(po_await)
    assert po_await.follow_up_count == 2

    # Step 3: Advance +24h -> po_await hits Breach 3 -> UNRESPONSIVE_ESCALATED + backup CTA
    r3 = engine.advance_clock_and_watch(seeded_session, hours=24)
    esc_events = [e for e in r3["sla_events"] if e.po_number == po_await.po_number]
    assert len(esc_events) == 1 and esc_events[0].event_type == "UNRESPONSIVE_ESCALATED"
    seeded_session.refresh(po_await)
    assert po_await.status == POStatus.UNRESPONSIVE_ESCALATED.value

    esc_alert_id = esc_events[0].action_item_id
    assert esc_alert_id is not None
    switch_res = engine.execute_alert_cta(
        seeded_session,
        esc_alert_id,
        action="switch_backup_supplier",
    )
    seeded_session.refresh(po_await)
    assert po_await.status == POStatus.CANCELLED.value
    assert switch_res["new_po_id"] is not None
    assert switch_res["draft_comm_id"] is not None

    # Step 4: Advance +3d -> po_ack is past promised_eta + 1d with no GRN -> OVERDUE
    r4 = engine.advance_clock_and_watch(seeded_session, days=3)
    assert any(
        e.po_number == po_ack.po_number and e.event_type == "OVERDUE_DETECTED"
        for e in r4["sla_events"]
    )
    seeded_session.refresh(po_ack)
    assert po_ack.status == POStatus.OVERDUE.value

    # Step 5: Quarantine SLA -> deliver po_ack & submit bad invoice, advance 49h then 50h
    record_goods_receipt(seeded_session, po_id=po_ack.id, received_qty=po_ack.qty)
    submit_invoice_and_match(
        seeded_session,
        po_id=po_ack.id,
        invoice_number="INV-BAD-ACK",
        extracted_qty=po_ack.qty,
        extracted_unit_price=po_ack.unit_price + 100.0,
        extracted_total=po_ack.qty * (po_ack.unit_price + 100.0),
    )
    seeded_session.refresh(po_ack)
    assert po_ack.status == POStatus.QUARANTINED_INVOICE_MISMATCH.value

    r5 = engine.advance_clock_and_watch(seeded_session, hours=49)
    assert any(
        e.po_number == po_ack.po_number and e.event_type == "QUARANTINE_REMINDER_DRAFTED"
        for e in r5["sla_events"]
    )
    r6 = engine.advance_clock_and_watch(seeded_session, hours=50)
    assert any(
        e.po_number == po_ack.po_number and e.event_type == "QUARANTINE_ESCALATED"
        for e in r6["sla_events"]
    )

    # All Lex outbound drafts must remain DRAFT_PENDING_APPROVAL until approved
    lex_drafts = list_drafts(seeded_session, PersonaKey.LEX.value)
    assert len(lex_drafts) >= 5
    sup1_inbox_ids = {m.id for m in list_inbox(seeded_session, PersonaKey.SUP1.value)}
    for d in lex_drafts:
        assert d.id not in sup1_inbox_ids


def test_fastapi_routes_http_end_to_end(seeded_session):
    po_await = seeded_session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.status == POStatus.SENT_AWAITING_ACK.value)
    )
    assert po_await is not None

    llm = SmartMockLLM(
        next_triage=EmailTriageResult(
            matched_po_number=po_await.po_number,
            intent=EmailIntent.ACKNOWLEDGED,
            confidence=0.95,
            proposed_eta_date="2026-12-05",
            summary_for_manager=f"Deccan confirmed {po_await.po_number}.",
            suggested_action="None.",
            reasoning="Confirmed.",
        )
    )
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    app = create_app()

    def _override_session():
        yield seeded_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_workflow_engine] = lambda: engine

    client = TestClient(app)

    # 1. Manual send from lex returns 403 Forbidden
    resp_forbidden = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": "lex",
            "to_personas": "sup1",
            "subject": "Illegal send",
            "body": "Should fail with 403",
        },
    )
    assert resp_forbidden.status_code == 403

    # 2. Supplier sends ack email to Lex -> 200 & triaged
    resp_ack = client.post(
        "/api/mailroom/send",
        data={
            "from_persona": "sup3",
            "to_personas": "lex",
            "cc_personas": "inventory_manager",
            "subject": f"Re: {po_await.po_number}",
            "body": f"We confirm {po_await.po_number} for Dec 5 delivery.",
        },
    )
    assert resp_ack.status_code == 200
    body = resp_ack.json()
    assert body["triage"]["triage"]["intent"] == "ACKNOWLEDGED"

    # 3. Check traces & autonomy endpoints
    traces_resp = client.get("/api/observability/traces?agent_name=lex")
    assert traces_resp.status_code == 200
    assert len(traces_resp.json()) >= 1

    autonomy_resp = client.get("/api/observability/autonomy?agent_name=lex")
    assert autonomy_resp.status_code == 200
    assert len(autonomy_resp.json()) == len(EmailCategory)

    # 4. Suppliers + memory note CRUD
    sup_resp = client.get("/api/suppliers")
    assert sup_resp.status_code == 200
    sup_id = sup_resp.json()[0]["id"]

    add_mem = client.post(
        f"/api/suppliers/{sup_id}/memory",
        json={"note": "Always ships in wooden crates"},
    )
    assert add_mem.status_code == 200
    note_id = add_mem.json()["id"]

    upd_mem = client.patch(
        f"/api/suppliers/{sup_id}/memory/{note_id}",
        json={"note": "Always ships in reinforced wooden crates"},
    )
    assert upd_mem.status_code == 200

    del_mem = client.delete(f"/api/suppliers/{sup_id}/memory/{note_id}")
    assert del_mem.status_code == 200

    # 5. Withdraw stock via API -> creates draft -> approve edited via API
    parts_resp = client.get("/api/parts")
    assert parts_resp.status_code == 200
    hero_part = next(p for p in parts_resp.json() if p["sku"] == "SKU-001")

    consume_resp = client.post(
        f"/api/parts/{hero_part['part_id']}/consume",
        json={"qty": 2, "note": "HTTP test"},
    )
    assert consume_resp.status_code == 200
    draft_id = consume_resp.json()["draft_comm_id"]
    assert draft_id is not None

    approve_resp = client.post(
        f"/api/mailroom/drafts/{draft_id}/approve",
        json={"edited_body": "Edited PO dispatch body from manager."},
    )
    assert approve_resp.status_code == 200
    assert approve_resp.json()["approval_outcome"] == "APPROVED_EDITED"
    row = seeded_session.get(POCommunication, draft_id)
    assert row is not None and row.status == CommStatus.SENT.value

    # 6. Verify P2 & P17: PATCH /api/parts/{id}/rop and PATCH /api/parts/{id}/demand-override
    rop_ok = client.patch(
        f"/api/parts/{hero_part['part_id']}/rop",
        json={"rop": 4, "replenishment_point": 8},
    )
    assert rop_ok.status_code == 200
    assert rop_ok.json()["part_id"] == hero_part["part_id"]
    assert rop_ok.json()["rop"] == 4
    assert rop_ok.json()["replenishment_point"] == 8

    rop_bad = client.patch(
        f"/api/parts/{hero_part['part_id']}/rop",
        json={"rop": 10, "replenishment_point": 8},
    )
    assert rop_bad.status_code == 400

    rop_404 = client.patch(
        "/api/parts/99999/rop",
        json={"rop": 3, "replenishment_point": 6},
    )
    assert rop_404.status_code == 404

    ov_set = client.patch(
        f"/api/parts/{hero_part['part_id']}/demand-override",
        json={"manual_demand_override": 0.75},
    )
    assert ov_set.status_code == 200
    assert ov_set.json()["manual_demand_override"] == 0.75

    ov_clear = client.patch(
        f"/api/parts/{hero_part['part_id']}/demand-override",
        json={"manual_demand_override": None},
    )
    assert ov_clear.status_code == 200
    assert ov_clear.json()["manual_demand_override"] is None

    # 7. Verify P17: POST /api/clock direct set time (forward ok, backward 400 unless allow_reset)
    cur_clock = client.get("/api/clock").json()["sim_now"]
    back_fail = client.post("/api/clock", json={"sim_now": "2026-11-01T00:00:00Z"})
    assert back_fail.status_code == 400

    fwd_ok = client.post("/api/clock", json={"sim_now": "2026-11-21T12:00:00Z"})
    assert fwd_ok.status_code == 200
    assert fwd_ok.json()["sim_now"].startswith("2026-11-21T12:00:00")

    reset_ok = client.post(
        "/api/clock",
        json={"sim_now": cur_clock, "allow_reset": True},
    )
    assert reset_ok.status_code == 200

    # 8. Verify P18: PATCH /api/pos/{id} rejects CLOSED POs with 400
    closed_po = seeded_session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.status == POStatus.CLOSED.value)
    )
    assert closed_po is not None
    patch_closed = client.patch(
        f"/api/pos/{closed_po.id}",
        json={"unit_price": 999.0},
    )
    assert patch_closed.status_code == 400


def test_scenario_1a_standard_part_above_rop_stockout_math_and_delayed_critical_to_overdue(
    seeded_session,
):
    """Verify P3, P5, P13:
    1. A STANDARD part above ROP that receives a post-ack delay breaching
       projected_days_of_supply - 3d transitions to DELAYED_CRITICAL (not minor delay!).
    2. A STANDARD part with a short safe-buffer delay stays ACKNOWLEDGED (MINOR_DELAY_ACCEPTED).
    3. If the DELAYED_CRITICAL PO misses its revised_eta + 1d without GRN, run_sla_watcher
       transitions it to OVERDUE without InvalidPOTransitionError and lowers supplier score.
    """
    llm = SmartMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))

    # Seeded ACKNOWLEDGED PO is on SKU-006 (STANDARD part, on_hand=15 > ROP=12, ~20d supply)
    po_ack = seeded_session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.status == POStatus.ACKNOWLEDGED.value)
    )
    assert po_ack is not None
    part = seeded_session.get(Part, po_ack.part_id)
    supplier = seeded_session.get(Supplier, po_ack.supplier_id)
    assert part is not None and supplier is not None
    assert part.criticality == Criticality.STANDARD.value
    assert part.on_hand_stock > part.rop

    # 1. Safe minor delay (only 5d out, below projected_days_of_supply - 3d) -> ACKNOWLEDGED
    llm.next_triage = EmailTriageResult(
        matched_po_number=po_ack.po_number,
        intent=EmailIntent.POST_ACK_DELAY,
        confidence=0.95,
        proposed_eta_date="2026-11-25",
        delay_reason="Minor courier rescheduling",
        summary_for_manager="ETA shifted by 1 day to 2026-11-25.",
        suggested_action="No action needed.",
        reasoning="Minor delay.",
    )
    res_minor = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP1,
            to_personas=[PersonaKey.LEX],
            subject=f"Minor schedule update {po_ack.po_number}",
            body="Delivering Nov 25.",
        ),
    )
    seeded_session.refresh(po_ack)
    assert po_ack.status == POStatus.ACKNOWLEDGED.value
    minor_alert = seeded_session.get(ActionItem, res_minor["triage"]["alert_id"])
    assert minor_alert is not None and minor_alert.type == "MINOR_DELAY_ACCEPTED"
    assert minor_alert.cta_payload_json["projected_days_of_supply"] > 10.0

    # 2. Long delay (45d out -> breaches projected_days_of_supply - 3d even with on_hand > ROP!)
    llm.next_triage = EmailTriageResult(
        matched_po_number=po_ack.po_number,
        intent=EmailIntent.POST_ACK_DELAY,
        confidence=0.96,
        proposed_eta_date="2027-01-05",
        delay_reason="Foundry furnace outage",
        summary_for_manager="Delayed until 2027-01-05.",
        suggested_action="Expedite immediately.",
        reasoning="Long post-ack delay.",
    )
    res_crit = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP1,
            to_personas=[PersonaKey.LEX],
            subject=f"Major delay {po_ack.po_number}",
            body="Delayed to Jan 5, 2027.",
        ),
    )
    seeded_session.refresh(po_ack)
    assert po_ack.status == POStatus.DELAYED_CRITICAL.value
    crit_alert = seeded_session.get(ActionItem, res_crit["triage"]["alert_id"])
    assert crit_alert is not None and crit_alert.type == "CRITICAL_DELAY"
    assert crit_alert.cta_payload_json["days_short_of_eta"] > 0

    # 3. Advance clock past revised_eta + 2d -> DELAYED_CRITICAL transitions to OVERDUE (P3 & P5)
    score_before = supplier.reliability_score
    otd_before = supplier.otd_rate
    jump_res = engine.jump_to_po_eta_and_watch(seeded_session, po_ack.id, offset_days=2)
    assert any(
        e.po_id == po_ack.id and e.event_type == "OVERDUE_DETECTED"
        for e in jump_res["sla_events"]
    )
    seeded_session.refresh(po_ack)
    seeded_session.refresh(supplier)
    assert po_ack.status == POStatus.OVERDUE.value
    assert supplier.otd_rate < otd_before
    assert supplier.reliability_score <= score_before


def test_scenario_2_price_variance_stockout_payload_default_update_po_and_cancel_po_cta(
    seeded_session,
):
    """Verify P13 & P17: Scenario 2 PO_EXCEPTION alert computes deterministic price_variance_inr,
    price_variance_pct, total_cost_impact_inr, and projected_stockout_date; update_po CTA falls
    back to cta_payload_json when params omit unit_price/qty/revised_eta; and cancel_po CTA works.
    """
    llm = SmartMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))

    po_await = seeded_session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.status == POStatus.SENT_AWAITING_ACK.value)
    )
    assert po_await is not None
    orig_price = po_await.unit_price  # 780.0
    orig_qty = po_await.qty  # 15

    llm.next_triage = EmailTriageResult(
        matched_po_number=po_await.po_number,
        intent=EmailIntent.PRICE_CHANGE,
        confidence=0.96,
        proposed_unit_price=936.0,  # +156.0 (+20.0%)
        proposed_qty=12,
        proposed_eta_date="2026-12-25",
        summary_for_manager="Supplier hiked price to ₹936 and cut qty to 12.",
        suggested_action="Review exception.",
        reasoning="Price and qty revision.",
    )
    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP3,
            to_personas=[PersonaKey.LEX],
            subject=f"Update on {po_await.po_number}",
            body="Price is now Rs 936 for 12 pcs, ETA Dec 25.",
        ),
    )
    alert = seeded_session.get(ActionItem, res["triage"]["alert_id"])
    assert alert is not None and alert.type == "PO_EXCEPTION"
    cta = alert.cta_payload_json
    assert cta["price_variance_inr"] == round(936.0 - orig_price, 2)
    assert cta["price_variance_pct"] == 20.0
    assert cta["total_cost_impact_inr"] == round((936.0 - orig_price) * 12, 2)
    assert cta["projected_stockout_date"] is not None
    assert cta["projected_days_of_supply"] > 0
    action_names = [a["action"] for a in alert.suggested_actions_json]
    assert "cancel_po" in action_names
    assert "update_po" in action_names

    # Execute update_po CTA with EMPTY params -> must default from cta_payload_json!
    upd_res = engine.execute_alert_cta(seeded_session, alert.id, action="update_po", params={})
    seeded_session.refresh(po_await)
    assert po_await.unit_price == 936.0
    assert po_await.qty == 12
    assert po_await.revised_eta is not None
    assert po_await.status == POStatus.PENDING_DISPATCH_APPROVAL.value
    assert upd_res["draft_comm_id"] is not None

    # Also verify cancel_po CTA on a second exception alert
    alert2 = ActionItem(
        type="PO_EXCEPTION",
        severity="HIGH",
        urgency_score=70,
        po_id=po_await.id,
        part_id=po_await.part_id,
        title="Cancel test",
        summary="Cancel PO via CTA",
        suggested_actions_json=[{"action": "cancel_po", "label": "Cancel PO"}],
        cta_payload_json={"po_id": po_await.id},
        status=ActionItemStatus.OPEN.value,
        created_at= po_await.created_at,
    )
    seeded_session.add(alert2)
    seeded_session.flush()

    cancel_res = engine.execute_alert_cta(
        seeded_session,
        alert2.id,
        action="cancel_po",
        params={"note": "Cancelled due to excessive price hike"},
    )
    seeded_session.refresh(po_await)
    seeded_session.refresh(alert2)
    assert cancel_res["po_status"] == POStatus.CANCELLED.value
    assert po_await.status == POStatus.CANCELLED.value
    assert alert2.status == ActionItemStatus.RESOLVED.value
    assert orig_qty == 15


def test_quarantine_sla_timer_anchors_on_max_of_invoice_and_grn_timestamps(seeded_session):
    """Verify P9: When a mismatched invoice arrives 72h BEFORE the physical GRN is logged,
    the quarantine SLA timer anchors at max(invoice.received_at, grn.received_at) — i.e., when
    quarantine actually began — and does NOT fire the 48h reminder early.
    """
    llm = SmartMockLLM()
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))

    po_ack = seeded_session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.status == POStatus.ACKNOWLEDGED.value)
    )
    assert po_ack is not None

    # 1. Invoice arrives at T=0 (before GRN) with overbilled price -> PENDING_GRN
    submit_invoice_and_match(
        seeded_session,
        po_id=po_ack.id,
        invoice_number="INV-EARLY-72H",
        extracted_qty=po_ack.qty,
        extracted_unit_price=po_ack.unit_price + 200.0,
        extracted_total=po_ack.qty * (po_ack.unit_price + 200.0),
    )

    # 2. Advance clock 72h (3 days), then log physical GRN -> quarantine begins NOW at T=72h
    engine.advance_clock_and_watch(seeded_session, hours=72)
    engine.record_grn(seeded_session, po_id=po_ack.id, received_qty=po_ack.qty)
    seeded_session.refresh(po_ack)
    assert po_ack.status == POStatus.QUARANTINED_INVOICE_MISMATCH.value

    # 3. Advance +24h (24h after quarantine start, 96h after invoice arrival):
    #    Must NOT fire 48h reminder or 96h escalation!
    r_24h = engine.advance_clock_and_watch(seeded_session, hours=24)
    quar_events_24h = [
        e for e in r_24h["sla_events"] if e.po_id == po_ack.id
    ]
    assert quar_events_24h == []

    # 4. Advance another +25h (49h after GRN / quarantine start) -> NOW 48h reminder fires!
    r_49h = engine.advance_clock_and_watch(seeded_session, hours=25)
    quar_events_49h = [
        e.event_type for e in r_49h["sla_events"] if e.po_id == po_ack.id
    ]
    assert quar_events_49h == ["QUARANTINE_REMINDER_DRAFTED"]
