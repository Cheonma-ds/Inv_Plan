import json
from datetime import timedelta
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
from backend.core import clock
from backend.core.db import get_session
from backend.core.enums import (
    ActionItemStatus,
    AlertSeverity,
    CommStatus,
    DispatchDetailsStatus,
    EmailCategory,
    PersonaKey,
    POStatus,
)
from backend.core.models import ActionItem, PurchaseOrder, SupplierMemoryNote
from backend.mailroom import OutboundMessage, list_drafts, list_inbox
from backend.main import create_app
from backend.orchestrator.deps import get_workflow_engine
from backend.orchestrator.sla_watcher import auto_resolve_po_alerts
from backend.orchestrator.workflows import WorkflowEngine

T = TypeVar("T", bound=BaseModel)


class _LogisticsMockLLM:
    """Deterministic LLM stub for PO logistics triage and draft compliance tests."""

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
        if response_schema is EmailTriageResult:
            assert self.next_triage is not None, "Set next_triage before triage"
            res = self.next_triage
            return LLMResult(
                parsed=res,  # type: ignore[arg-type]
                raw_text=res.model_dump_json(),
                input_tokens=340,
                output_tokens=115,
                latency_ms=22,
                cost_inr=0.004,
                model_name="mock-gemini",
            )

        if response_schema is EmailDraftResult:
            po_num = payload["po_number"]
            cat = payload["category"]
            recipient = payload["recipient_persona"]
            missing = payload.get("context_facts", {}).get("missing_fields", [])
            missing_str = ", ".join(missing) if missing else "dispatch details"
            result = EmailDraftResult(
                to_personas=[recipient],
                cc_personas=[PersonaKey.INVENTORY_MANAGER.value],
                subject=f"[{po_num}] {cat.replace('_', ' ').title()}",
                body=(
                    f"Hello {payload['recipient_name']}, regarding {po_num}: "
                    f"please share {missing_str}."
                ),
            )
            return LLMResult(
                parsed=result,  # type: ignore[arg-type]
                raw_text=result.model_dump_json(),
                input_tokens=290,
                output_tokens=85,
                latency_ms=16,
                cost_inr=0.003,
                model_name="mock-gemini",
            )

        raise AssertionError(f"Unexpected schema {response_schema}")


def test_ack_with_complete_logistics_creates_info_alert_and_no_missing_draft(
    seeded_session,
) -> None:
    po_await = seeded_session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.status == POStatus.SENT_AWAITING_ACK.value)
    )
    assert po_await is not None

    llm = _LogisticsMockLLM(
        next_triage=EmailTriageResult(
            matched_po_number=po_await.po_number,
            intent=EmailIntent.ACKNOWLEDGED,
            confidence=0.97,
            proposed_eta_date="2026-12-03",
            promised_dispatch_date="2026-11-23",
            carrier_name="BlueDart",
            tracking_number="BD-550192",
            details_promised_later=False,
            summary_for_manager=f"Confirmed {po_await.po_number} with BlueDart BD-550192.",
            suggested_action="Monitor delivery.",
            reasoning="All dispatch fields included in ack.",
        )
    )
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))

    res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP3,
            to_personas=[PersonaKey.LEX],
            cc_personas=[PersonaKey.INVENTORY_MANAGER],
            subject=f"Re: {po_await.po_number}",
            body="Confirmed! Dispatching 2026-11-23 via BlueDart tracking BD-550192.",
            po_id=po_await.id,
        ),
    )

    seeded_session.refresh(po_await)
    assert po_await.status == POStatus.ACKNOWLEDGED.value
    assert po_await.acknowledged_at is not None
    assert po_await.promised_dispatch_date is not None
    assert po_await.carrier_name == "BlueDart"
    assert po_await.tracking_number == "BD-550192"
    assert po_await.dispatch_details_status == DispatchDetailsStatus.COMPLETE.value
    assert po_await.dispatch_details_due_at is None
    assert po_await.missing_dispatch_fields_json == []

    triage_out = res["triage"]
    assert triage_out["draft_comm_id"] is None
    alert = seeded_session.get(ActionItem, triage_out["alert_id"])
    assert alert is not None
    assert alert.type == "PO_ACKNOWLEDGED_COMPLETE"
    assert alert.severity == AlertSeverity.INFO.value
    assert "BlueDart" in alert.summary
    assert "BD-550192" in alert.summary


def test_ack_deferred_2d_sla_expiry_nudge_and_followup_completion(seeded_session) -> None:
    po_await = seeded_session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.status == POStatus.SENT_AWAITING_ACK.value)
    )
    assert po_await is not None

    llm = _LogisticsMockLLM(
        next_triage=EmailTriageResult(
            matched_po_number=po_await.po_number,
            intent=EmailIntent.ACKNOWLEDGED,
            confidence=0.95,
            proposed_eta_date="2026-12-03",
            carrier_name="BlueDart",
            details_promised_later=True,
            details_promised_by_date="2026-11-22",
            summary_for_manager=f"Confirmed {po_await.po_number}; tracking promised in 2 days.",
            suggested_action="Wait 2 days for tracking details.",
            reasoning="Supplier promised BlueDart tracking in 2 days.",
        )
    )
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    sim_start = clock.now(seeded_session)

    # 1. Ack with deferred details -> DEFERRED_2D + INFO alert + zero draft
    ack_res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP3,
            to_personas=[PersonaKey.LEX],
            cc_personas=[PersonaKey.INVENTORY_MANAGER],
            subject=f"Re: {po_await.po_number}",
            body="Confirmed! Will share BlueDart tracking and dispatch date in 2 days.",
            po_id=po_await.id,
        ),
    )
    seeded_session.refresh(po_await)
    assert po_await.status == POStatus.ACKNOWLEDGED.value
    assert po_await.dispatch_details_status == DispatchDetailsStatus.DEFERRED_2D.value
    assert po_await.dispatch_details_due_at is not None
    assert clock.ensure_utc(po_await.dispatch_details_due_at) == sim_start + timedelta(days=2)
    assert po_await.missing_dispatch_fields_json == ["promised_dispatch_date", "tracking_number"]
    assert ack_res["triage"]["draft_comm_id"] is None

    deferred_alert = seeded_session.get(ActionItem, ack_res["triage"]["alert_id"])
    assert deferred_alert is not None
    assert deferred_alert.type == "DISPATCH_DETAILS_DEFERRED"
    assert deferred_alert.severity == AlertSeverity.INFO.value
    assert "2-day" in deferred_alert.summary

    # 2. Advance +1 day -> 2-day timer must NOT fire early
    day1 = engine.advance_clock_and_watch(seeded_session, days=1)
    assert not any(
        e.po_id == po_await.id and e.event_type == "DISPATCH_DETAILS_NUDGE_DRAFTED"
        for e in day1["sla_events"]
    )

    # 3. Advance +1 more day (+2d total) -> TIMED_OUT + DISPATCH_DETAILS_NUDGE draft + HIGH alert
    day2 = engine.advance_clock_and_watch(seeded_session, days=1)
    nudge_events = [
        e
        for e in day2["sla_events"]
        if e.po_id == po_await.id and e.event_type == "DISPATCH_DETAILS_NUDGE_DRAFTED"
    ]
    assert len(nudge_events) == 1
    seeded_session.refresh(po_await)
    assert po_await.dispatch_details_status == DispatchDetailsStatus.TIMED_OUT.value

    nudge_draft_id = nudge_events[0].draft_comm_id
    nudge_alert_id = nudge_events[0].action_item_id
    assert nudge_draft_id is not None and nudge_alert_id is not None
    drafts = {d.id: d for d in list_drafts(seeded_session, PersonaKey.LEX.value)}
    assert drafts[nudge_draft_id].category == EmailCategory.DISPATCH_DETAILS_NUDGE.value
    assert drafts[nudge_draft_id].status == CommStatus.DRAFT_PENDING_APPROVAL.value

    nudge_alert = seeded_session.get(ActionItem, nudge_alert_id)
    assert nudge_alert is not None
    assert nudge_alert.type == "MISSING_DISPATCH_DETAILS_DUE"
    assert nudge_alert.severity == AlertSeverity.HIGH.value

    # 4. Follow-up supplier reply provides tracking & actual dispatch -> COMPLETE + resolves alerts
    llm.next_triage = EmailTriageResult(
        matched_po_number=po_await.po_number,
        intent=EmailIntent.GENERAL_REPLY,
        confidence=0.96,
        actual_dispatch_date="2026-11-22",
        carrier_name="BlueDart",
        tracking_number="BD-8821",
        details_promised_later=False,
        summary_for_manager=f"Shipped {po_await.po_number} via BlueDart BD-8821.",
        suggested_action="Track shipment.",
        reasoning="Follow-up message contains actual dispatch date and tracking.",
    )
    follow_res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP3,
            to_personas=[PersonaKey.LEX],
            cc_personas=[PersonaKey.INVENTORY_MANAGER],
            subject=f"Re: {po_await.po_number} — Tracking",
            body="Shipped today (2026-11-22) via BlueDart, tracking BD-8821.",
            po_id=po_await.id,
        ),
    )

    seeded_session.refresh(po_await)
    seeded_session.refresh(nudge_alert)
    seeded_session.refresh(deferred_alert)
    assert po_await.dispatch_details_status == DispatchDetailsStatus.COMPLETE.value
    assert po_await.missing_dispatch_fields_json == []
    assert po_await.tracking_number == "BD-8821"
    assert po_await.actual_dispatch_date is not None
    assert nudge_alert.status == ActionItemStatus.RESOLVED.value
    assert deferred_alert.status == ActionItemStatus.RESOLVED.value

    updated_alert = seeded_session.get(ActionItem, follow_res["triage"]["alert_id"])
    assert updated_alert is not None
    assert updated_alert.type == "PO_LOGISTICS_UPDATED"
    assert updated_alert.severity == AlertSeverity.INFO.value
    assert "BD-8821" in updated_alert.summary


def test_ack_missing_details_immediate_draft_and_repeated_deferral_guard(
    seeded_session,
) -> None:
    po_await = seeded_session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.status == POStatus.SENT_AWAITING_ACK.value)
    )
    assert po_await is not None

    llm = _LogisticsMockLLM(
        next_triage=EmailTriageResult(
            matched_po_number=po_await.po_number,
            intent=EmailIntent.ACKNOWLEDGED,
            confidence=0.95,
            proposed_eta_date="2026-12-04",
            details_promised_later=False,
            summary_for_manager=f"Confirmed {po_await.po_number} without dispatch details.",
            suggested_action="Request missing dispatch details.",
            reasoning="Ack lacks dispatch date, carrier, and tracking.",
        )
    )
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))

    ack_res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP3,
            to_personas=[PersonaKey.LEX],
            subject=f"Re: {po_await.po_number}",
            body="Confirmed PO-1037, will deliver Dec 4.",
            po_id=po_await.id,
        ),
    )

    seeded_session.refresh(po_await)
    assert po_await.dispatch_details_status == DispatchDetailsStatus.AWAITING_IMMEDIATE.value
    assert po_await.dispatch_details_due_at is None
    assert po_await.missing_dispatch_fields_json == [
        "promised_dispatch_date",
        "carrier_name",
        "tracking_number",
    ]
    draft_id = ack_res["triage"]["draft_comm_id"]
    alert_id = ack_res["triage"]["alert_id"]
    assert draft_id is not None and alert_id is not None

    draft_row = {d.id: d for d in list_drafts(seeded_session, PersonaKey.LEX.value)}[draft_id]
    assert draft_row.category == EmailCategory.DISPATCH_DETAILS_REQUEST.value

    missing_alert = seeded_session.get(ActionItem, alert_id)
    assert missing_alert is not None
    assert missing_alert.type == "MISSING_DISPATCH_DETAILS"
    assert missing_alert.severity == AlertSeverity.MEDIUM.value

    # Approve draft via CTA -> sends email & resolves alert
    engine.execute_alert_cta(seeded_session, alert_id, action="approve_draft")
    seeded_session.refresh(missing_alert)
    assert missing_alert.status == ActionItemStatus.RESOLVED.value
    assert any(m.id == draft_id for m in list_inbox(seeded_session, PersonaKey.SUP3.value))

    # Force TIMED_OUT and verify repeated "will send in 2 days" does NOT restart DEFERRED_2D loop
    po_await.dispatch_details_status = DispatchDetailsStatus.TIMED_OUT.value
    seeded_session.flush()
    llm.next_triage = EmailTriageResult(
        matched_po_number=po_await.po_number,
        intent=EmailIntent.GENERAL_REPLY,
        confidence=0.93,
        details_promised_later=True,
        summary_for_manager="Supplier again promised tracking in 2 days.",
        suggested_action="Escalate manually.",
        reasoning="Repeated deferral.",
    )
    rep_res = engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP3,
            to_personas=[PersonaKey.LEX],
            subject=f"Re: {po_await.po_number}",
            body="Sorry, we will send tracking in 2 days.",
            po_id=po_await.id,
        ),
    )
    seeded_session.refresh(po_await)
    assert po_await.dispatch_details_status == DispatchDetailsStatus.TIMED_OUT.value
    assert po_await.dispatch_details_due_at is None
    rep_alert = seeded_session.get(ActionItem, rep_res["triage"]["alert_id"])
    assert rep_alert is not None
    assert rep_alert.severity == AlertSeverity.HIGH.value
    assert rep_alert.cta_payload_json.get("repeated_deferral") is True


def test_mandatory_memory_note_alert_and_auto_resolve_preserves_notifications(
    seeded_session,
) -> None:
    po_await = seeded_session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.status == POStatus.SENT_AWAITING_ACK.value)
    )
    assert po_await is not None

    llm = _LogisticsMockLLM(
        next_triage=EmailTriageResult(
            matched_po_number=po_await.po_number,
            intent=EmailIntent.ACKNOWLEDGED,
            confidence=0.96,
            proposed_eta_date="2026-12-03",
            promised_dispatch_date="2026-11-23",
            carrier_name="DTDC",
            tracking_number="DT-7001",
            new_memory_note="Requires PO number on packing slip for dock clearance.",
            summary_for_manager=f"Confirmed {po_await.po_number} via DTDC DT-7001.",
            suggested_action="None.",
            reasoning="Complete ack with durable supplier note.",
        )
    )
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=llm))
    engine.send_mailroom_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP3,
            to_personas=[PersonaKey.LEX],
            subject=f"Re: {po_await.po_number}",
            body="Confirmed, dispatching Nov 23 via DTDC DT-7001. Always include PO# on slip.",
            po_id=po_await.id,
        ),
    )

    mem_notes = seeded_session.scalars(
        select(SupplierMemoryNote).where(SupplierMemoryNote.supplier_id == po_await.supplier_id)
    ).all()
    assert any("packing slip" in m.note for m in mem_notes)

    mem_alert = seeded_session.scalar(
        select(ActionItem).where(
            ActionItem.po_id == po_await.id,
            ActionItem.type == "SUPPLIER_MEMORY_LEARNED",
        )
    )
    ack_info_alert = seeded_session.scalar(
        select(ActionItem).where(
            ActionItem.po_id == po_await.id,
            ActionItem.type == "PO_ACKNOWLEDGED_COMPLETE",
        )
    )
    assert mem_alert is not None and mem_alert.severity == AlertSeverity.INFO.value
    assert ack_info_alert is not None and ack_info_alert.severity == AlertSeverity.INFO.value

    # Create an actionable alert on the same PO and verify generic auto_resolve_po_alerts
    # resolves the actionable alert while preserving Lex informational notifications.
    actionable = ActionItem(
        type="PO_EXCEPTION",
        severity=AlertSeverity.HIGH.value,
        urgency_score=70,
        po_id=po_await.id,
        part_id=po_await.part_id,
        title="Actionable test alert",
        summary="Should resolve on generic auto_resolve_po_alerts.",
        status=ActionItemStatus.OPEN.value,
        created_at=clock.now(seeded_session),
    )
    seeded_session.add(actionable)
    seeded_session.flush()

    resolved_count = auto_resolve_po_alerts(seeded_session, po_await.id)
    assert resolved_count == 1
    seeded_session.refresh(actionable)
    seeded_session.refresh(mem_alert)
    seeded_session.refresh(ack_info_alert)
    assert actionable.status == ActionItemStatus.RESOLVED.value
    assert mem_alert.status == ActionItemStatus.OPEN.value
    assert ack_info_alert.status == ActionItemStatus.OPEN.value


def test_sla_watcher_deferred_2d_expiry_also_fires_on_overdue_po(seeded_session) -> None:
    po_ack = seeded_session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.status == POStatus.ACKNOWLEDGED.value)
    )
    assert po_ack is not None
    po_ack.status = POStatus.OVERDUE.value
    po_ack.dispatch_details_status = DispatchDetailsStatus.DEFERRED_2D.value
    po_ack.dispatch_details_due_at = clock.now(seeded_session) + timedelta(days=2)
    po_ack.missing_dispatch_fields_json = ["tracking_number"]
    seeded_session.flush()

    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=_LogisticsMockLLM()))
    res = engine.advance_clock_and_watch(seeded_session, days=2)
    assert any(
        e.po_id == po_ack.id and e.event_type == "DISPATCH_DETAILS_NUDGE_DRAFTED"
        for e in res["sla_events"]
    )
    seeded_session.refresh(po_ack)
    assert po_ack.dispatch_details_status == DispatchDetailsStatus.TIMED_OUT.value


def test_http_pos_serialization_and_escalate_to_procurement_endpoint(seeded_session) -> None:
    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=_LogisticsMockLLM()))
    app = create_app()
    app.dependency_overrides[get_session] = lambda: seeded_session
    app.dependency_overrides[get_workflow_engine] = lambda: engine
    client = TestClient(app)

    pos_list_resp = client.get("/api/pos")
    assert pos_list_resp.status_code == 200
    pos_list = pos_list_resp.json()
    assert len(pos_list) > 0

    open_po = next(
        p for p in pos_list if p["status"] == POStatus.SENT_AWAITING_ACK.value
    )
    for field in (
        "acknowledged_at",
        "promised_dispatch_date",
        "actual_dispatch_date",
        "carrier_name",
        "tracking_number",
        "dispatch_details_status",
        "dispatch_details_due_at",
        "missing_dispatch_fields",
        "escalated_to_procurement_at",
        "procurement_escalation_note",
        "closed_at",
    ):
        assert field in open_po

    detail_resp = client.get(f"/api/pos/{open_po['id']}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["dispatch_details_status"] == open_po["dispatch_details_status"]

    # Escalate open PO to Procurement
    esc_resp = client.post(
        f"/api/pos/{open_po['id']}/escalate-procurement",
        json={"reason": "Supplier unresponsive after follow-ups; missing tracking details"},
    )
    assert esc_resp.status_code == 200
    esc_body = esc_resp.json()
    assert esc_body["po"]["escalated_to_procurement_at"] is not None
    assert "missing tracking details" in esc_body["po"]["procurement_escalation_note"]
    assert "Escalated to Procurement" in esc_body["po"]["notes"]

    # Delivered immediately to Procurement inbox (from inventory_manager CC lex)
    proc_inbox = client.get("/api/mailroom/procurement/inbox")
    assert proc_inbox.status_code == 200
    matching_emails = [m for m in proc_inbox.json() if m["id"] == esc_body["comm_id"]]
    assert len(matching_emails) == 1
    email = matching_emails[0]
    assert email["from_persona"] == PersonaKey.INVENTORY_MANAGER.value
    assert PersonaKey.PROCUREMENT.value in email["to_personas"]
    assert PersonaKey.LEX.value in email["cc_personas"]
    assert email["category"] == EmailCategory.PROCUREMENT_ESCALATION.value
    assert open_po["po_number"] in email["subject"]

    # Alert created with assigned_team="Procurement"
    alert = seeded_session.get(ActionItem, esc_body["alert_id"])
    assert alert is not None
    assert alert.type == "PROCUREMENT_ESCALATION"
    assert alert.severity == AlertSeverity.HIGH.value
    assert alert.assigned_team == "Procurement"

    # Terminal (CLOSED) PO escalation returns 400; missing PO returns 404
    closed_po = next(p for p in pos_list if p["status"] == POStatus.CLOSED.value)
    bad_closed = client.post(
        f"/api/pos/{closed_po['id']}/escalate-procurement",
        json={"reason": "Should fail"},
    )
    assert bad_closed.status_code == 400

    bad_missing = client.post(
        "/api/pos/999999/escalate-procurement",
        json={"reason": "Missing"},
    )
    assert bad_missing.status_code == 404
