from typing import TypeVar

from pydantic import BaseModel

from backend.agents.lex import (
    EmailDraftInput,
    EmailDraftResult,
    EmailIntent,
    EmailTriageInput,
    EmailTriageResult,
    LexAgent,
    OpenPOContext,
)
from backend.agents.llm_client import LLMResult
from backend.core.enums import EmailCategory
from backend.observability import list_traces

T = TypeVar("T", bound=BaseModel)


class StubLLMClient:
    def __init__(self, canned: BaseModel) -> None:
        self.canned = canned

    def generate_structured(
        self,
        *,
        prompt: str,
        response_schema: type[T],
        system_instruction: str | None = None,
        temperature: float = 0.0,
        pdf_bytes: bytes | None = None,
    ) -> LLMResult[T]:
        assert isinstance(self.canned, response_schema)
        return LLMResult(
            parsed=self.canned,
            raw_text=self.canned.model_dump_json(),
            input_tokens=420,
            output_tokens=110,
            latency_ms=35,
            cost_inr=0.0054,
            model_name="stub-gemini",
        )


def _sample_triage_input() -> EmailTriageInput:
    return EmailTriageInput(
        sender_persona="sup3",
        sender_name="Gamma Fasteners & Valves",
        subject="Re: PO-1037",
        body="Confirmed, we will deliver 12 units on schedule.",
        current_date="2026-11-20",
        open_pos=[
            OpenPOContext(
                po_number="PO-1037",
                part_sku="PART-002",
                part_name="Industrial Servo Drive 2kW",
                qty=12,
                unit_price=14500.0,
                status="SENT_AWAITING_ACK",
                quoted_lead_time_days=14,
            )
        ],
    )


def test_triage_high_confidence_logs_trace(seeded_session):
    canned = EmailTriageResult(
        matched_po_number="PO-1037",
        intent=EmailIntent.ACKNOWLEDGED,
        confidence=0.95,
        proposed_eta_date="2026-12-03",
        summary_for_manager="Gamma confirmed PO-1037 for 12 units.",
        suggested_action="No action required.",
        reasoning="Explicit confirmation of PO-1037.",
    )
    lex = LexAgent(llm_client=StubLLMClient(canned))
    res = lex.triage_email(seeded_session, _sample_triage_input(), linked_comm_id=99)

    assert res.intent == EmailIntent.ACKNOWLEDGED
    assert res.matched_po_number == "PO-1037"

    traces = list_traces(seeded_session, agent_name="lex", task_type="email_triage")
    assert len(traces) == 1
    assert traces[0].input_tokens == 420
    assert traces[0].output_tokens == 110
    assert traces[0].confidence == 0.95
    assert traces[0].linked_entity_id == 99


def test_triage_low_confidence_forces_ambiguous_guardrail(seeded_session):
    canned = EmailTriageResult(
        matched_po_number="PO-1037",
        intent=EmailIntent.PRICE_CHANGE,
        confidence=0.72,
        proposed_unit_price=16000.0,
        summary_for_manager="Supplier may be hinting at a price change.",
        suggested_action="Review manually.",
        reasoning="Wording is vague.",
    )
    lex = LexAgent(llm_client=StubLLMClient(canned))
    res = lex.triage_email(seeded_session, _sample_triage_input())

    assert res.intent == EmailIntent.AMBIGUOUS
    assert "Guardrail Escalation" in res.reasoning


def test_triage_unmatched_po_forces_ambiguous_guardrail(seeded_session):
    canned = EmailTriageResult(
        matched_po_number="PO-9999",
        intent=EmailIntent.ACKNOWLEDGED,
        confidence=0.96,
        summary_for_manager="Confirmed unknown PO.",
        suggested_action="Verify PO number.",
        reasoning="Mentioned PO-9999.",
    )
    lex = LexAgent(llm_client=StubLLMClient(canned))
    res = lex.triage_email(seeded_session, _sample_triage_input())

    assert res.intent == EmailIntent.AMBIGUOUS
    assert "does not match an open PO" in res.reasoning


def test_draft_email_enforces_manager_cc_and_po_subject(seeded_session):
    canned = EmailDraftResult(
        to_personas=["sup2"],
        cc_personas=[],
        subject="Delivery Follow-up",
        body="Could you confirm delivery status?",
    )
    lex = LexAgent(llm_client=StubLLMClient(canned))
    res = lex.draft_email(
        seeded_session,
        EmailDraftInput(
            category=EmailCategory.PRE_ETA_CHECKIN,
            po_number="PO-1038",
            part_sku="PART-007",
            part_name="Optical Limit Switch",
            qty=25,
            unit_price=480.0,
            recipient_persona="sup2",
            recipient_name="Beta Hydraulics & Seals",
            current_date="2026-11-20",
        ),
        linked_po_id=38,
    )

    assert res.to_personas == ["sup2"]
    assert "inventory_manager" in res.cc_personas
    assert "PO-1038" in res.subject

    traces = list_traces(seeded_session, agent_name="lex", task_type="email_draft")
    assert len(traces) == 1
    assert traces[0].linked_entity_id == 38
