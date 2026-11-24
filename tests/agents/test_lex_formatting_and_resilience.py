from types import SimpleNamespace
from typing import TypeVar

import pytest
from pydantic import BaseModel

from backend.agents.lex import (
    AdditionalPOUpdate,
    EmailDraftInput,
    EmailDraftResult,
    EmailIntent,
    EmailTriageInput,
    EmailTriageResult,
    LexAgent,
    OpenPOContext,
)
from backend.agents.llm_client import GeminiLLMClient, LLMResult
from backend.core.enums import EmailCategory, PersonaKey, POStatus
from backend.mailroom.schemas import OutboundMessage
from backend.mailroom.service import send_email
from backend.orchestrator.workflows import WorkflowEngine
from backend.po.service import create_po

T = TypeVar("T", bound=BaseModel)


class _TransientAPIError(Exception):
    def __init__(self, code: int, message: str = "transient") -> None:
        super().__init__(message)
        self.code = code


class _ClientError(Exception):
    def __init__(self, code: int, message: str = "bad request") -> None:
        super().__init__(message)
        self.code = code


def test_gemini_client_retries_transient_errors_with_exponential_backoff(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("backend.agents.llm_client.time.sleep", lambda s: sleeps.append(s))

    attempts = {"count": 0}
    canned = EmailDraftResult(
        to_personas=["sup1"],
        cc_personas=["inventory_manager"],
        subject="[PO-1001] Test",
        body="Dear Supplier,\n\nTest body.\n\nBest regards,\n**Lex**\nAI Supply Chain Assistant",
    )

    class _FakeModels:
        def generate_content(self, *, model, contents, config):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise _TransientAPIError(503, "Service Unavailable")
            return SimpleNamespace(
                text=canned.model_dump_json(),
                parsed=canned,
                usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=50),
            )

    client = GeminiLLMClient(
        api_key="test-key",
        timeout_seconds=12.0,
        max_retries=3,
        backoff_base_seconds=0.25,
    )
    client._client = SimpleNamespace(models=_FakeModels())

    result = client.generate_structured(
        prompt="hello",
        response_schema=EmailDraftResult,
        image_attachments=[("image/png", b"png-bytes")],
    )

    assert attempts["count"] == 3
    assert sleeps == [0.25, 0.5]
    assert result.parsed.subject == "[PO-1001] Test"


def test_gemini_client_fails_fast_on_non_transient_error(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("backend.agents.llm_client.time.sleep", lambda s: sleeps.append(s))

    attempts = {"count": 0}

    class _FakeModels:
        def generate_content(self, *, model, contents, config):
            attempts["count"] += 1
            raise _ClientError(400, "Invalid argument")

    client = GeminiLLMClient(api_key="test-key", max_retries=3, backoff_base_seconds=0.5)
    client._client = SimpleNamespace(models=_FakeModels())

    with pytest.raises(_ClientError):
        client.generate_structured(prompt="hello", response_schema=EmailDraftResult)

    assert attempts["count"] == 1
    assert sleeps == []


def test_gemini_client_passes_image_attachments_and_pdf_parts():
    captured_contents: list[object] = []
    canned = EmailTriageResult(
        matched_po_number="PO-1001",
        intent=EmailIntent.GENERAL_REPLY,
        confidence=0.95,
        summary_for_manager="Damaged box photo attached.",
        suggested_action="Review photo.",
        reasoning="Photo shows crushed corner.",
    )

    class _FakeModels:
        def generate_content(self, *, model, contents, config):
            captured_contents.extend(contents)
            return SimpleNamespace(
                text=canned.model_dump_json(),
                parsed=canned,
                usage_metadata=SimpleNamespace(prompt_token_count=200, candidates_token_count=40),
            )

    client = GeminiLLMClient(api_key="test-key")
    client._client = SimpleNamespace(models=_FakeModels())

    client.generate_structured(
        prompt="triage this",
        response_schema=EmailTriageResult,
        pdf_bytes=b"%PDF-1.4...",
        image_attachments=[("image/jpeg", b"jpg-data"), ("image/png", b"png-data")],
    )

    assert len(captured_contents) == 4  # 1 PDF + 2 images + 1 prompt
    assert captured_contents[-1] == "triage this"


def test_draft_email_enforces_structured_formatting_bolding_and_currency(seeded_session):
    class _WallOfTextLLM:
        def generate_structured(
            self,
            *,
            prompt: str,
            response_schema: type[T],
            system_instruction: str | None = None,
            temperature: float = 0.0,
            pdf_bytes: bytes | None = None,
            image_attachments: list[tuple[str, bytes]] | None = None,
        ) -> LLMResult[T]:
            canned = EmailDraftResult(
                to_personas=["sup1"],
                cc_personas=[],
                subject="Order Dispatch",
                body=(
                    "Dear Apex Precision Components, please accept our purchase order for 20 units "
                    "of BRG-402 at $450.00/unit. Kindly acknowledge receipt and share your "
                    "expected dispatch schedule at the earliest convenience."
                ),
            )
            return LLMResult(
                parsed=canned,  # type: ignore[arg-type]
                raw_text=canned.model_dump_json(),
                input_tokens=100,
                output_tokens=50,
                latency_ms=10,
                cost_inr=0.001,
                model_name="stub",
            )

    lex = LexAgent(llm_client=_WallOfTextLLM())
    draft = lex.draft_email(
        seeded_session,
        EmailDraftInput(
            category=EmailCategory.PO_DISPATCH,
            po_number="PO-2042",
            part_sku="BRG-402",
            part_name="Deep Groove Bearing",
            qty=20,
            unit_price=450.0,
            currency="INR",
            recipient_persona="sup1",
            recipient_name="Apex Precision Components",
            current_date="2026-10-01",
        ),
    )

    assert "[PO-2042]" in draft.subject
    assert "inventory_manager" in draft.cc_personas
    assert draft.body.startswith("Dear Apex Precision Components,\n\n")
    assert "\n\n" in draft.body
    assert "**PO-2042**" in draft.body
    assert "**BRG-402**" in draft.body
    assert "$" not in draft.body
    assert "₹" in draft.body
    assert "Best regards,\n**Lex**\nAI Supply Chain Assistant" in draft.body


def test_additional_po_updates_price_change_without_price_forces_ambiguous(seeded_session):
    class _MultiPOLlm:
        def generate_structured(
            self,
            *,
            prompt: str,
            response_schema: type[T],
            system_instruction: str | None = None,
            temperature: float = 0.0,
            pdf_bytes: bytes | None = None,
            image_attachments: list[tuple[str, bytes]] | None = None,
        ) -> LLMResult[T]:
            canned = EmailTriageResult(
                matched_po_number="PO-1001",
                intent=EmailIntent.ACKNOWLEDGED,
                confidence=0.95,
                proposed_eta_date="2026-10-10",
                summary_for_manager="Confirmed PO-1001; hinted price review on PO-1002.",
                suggested_action="Confirm PO-1001 and review PO-1002.",
                reasoning="PO-1001 confirmed, PO-1002 hedged.",
                additional_po_updates=[
                    AdditionalPOUpdate(
                        matched_po_number="PO-1002",
                        intent=EmailIntent.PRICE_CHANGE,
                        confidence=0.92,
                        proposed_unit_price=None,
                        proposed_qty=None,
                    )
                ],
            )
            return LLMResult(
                parsed=canned,  # type: ignore[arg-type]
                raw_text=canned.model_dump_json(),
                input_tokens=100,
                output_tokens=50,
                latency_ms=10,
                cost_inr=0.001,
                model_name="stub",
            )

    lex = LexAgent(llm_client=_MultiPOLlm())
    res = lex.triage_email(
        seeded_session,
        EmailTriageInput(
            sender_persona="sup1",
            sender_name="Apex",
            subject="PO-1001 & PO-1002",
            body="PO-1001 confirmed; checking price on PO-1002.",
            current_date="2026-10-01",
            open_pos=[
                OpenPOContext(
                    po_number="PO-1001",
                    part_sku="BRG-402",
                    part_name="Bearing",
                    qty=10,
                    unit_price=100.0,
                    status="SENT_AWAITING_ACK",
                    quoted_lead_time_days=7,
                ),
                OpenPOContext(
                    po_number="PO-1002",
                    part_sku="FLT-108",
                    part_name="Filter",
                    qty=10,
                    unit_price=200.0,
                    status="SENT_AWAITING_ACK",
                    quoted_lead_time_days=7,
                ),
            ],
        ),
    )

    assert res.intent == EmailIntent.ACKNOWLEDGED
    assert len(res.additional_po_updates) == 1
    assert res.additional_po_updates[0].intent == EmailIntent.AMBIGUOUS


def test_workflow_passes_images_and_gracefully_falls_back_on_llm_failure(seeded_session, tmp_path):
    img_path = tmp_path / "damage_photo.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\nfake-png-bytes")

    captured_images: list[list[tuple[str, bytes]] | None] = []

    class _FailingThenWorkingLLM:
        def __init__(self, should_fail: bool) -> None:
            self.should_fail = should_fail

        def generate_structured(
            self,
            *,
            prompt: str,
            response_schema: type[T],
            system_instruction: str | None = None,
            temperature: float = 0.0,
            pdf_bytes: bytes | None = None,
            image_attachments: list[tuple[str, bytes]] | None = None,
        ) -> LLMResult[T]:
            captured_images.append(image_attachments)
            if self.should_fail:
                raise _TransientAPIError(503, "Gemini unavailable")
            canned = EmailTriageResult(
                matched_po_number=None,
                intent=EmailIntent.GENERAL_REPLY,
                confidence=0.91,
                summary_for_manager="Supplier attached damaged box photo.",
                suggested_action="Inspect photo.",
                reasoning="Image shows crushed carton.",
            )
            return LLMResult(
                parsed=canned,  # type: ignore[arg-type]
                raw_text=canned.model_dump_json(),
                input_tokens=100,
                output_tokens=30,
                latency_ms=10,
                cost_inr=0.001,
                model_name="stub",
            )

    from sqlalchemy import select

    from backend.core.models import ActionItem, Part, Supplier

    part = seeded_session.scalar(select(Part).limit(1))
    supplier = seeded_session.scalar(select(Supplier).where(Supplier.persona_key == "sup1"))
    assert part is not None and supplier is not None

    po = create_po(
        seeded_session,
        part_id=part.id,
        supplier_id=supplier.id,
        qty=10,
        unit_price=100.0,
    )
    po.status = POStatus.SENT_AWAITING_ACK.value
    seeded_session.flush()

    # 1. Image attachment bytes passed to triage_email
    engine_ok = WorkflowEngine(lex_agent=LexAgent(llm_client=_FailingThenWorkingLLM(False)))
    email_ok = send_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP1.value,
            to_personas=[PersonaKey.LEX.value],
            subject=f"Photo for {po.po_number}",
            body="See attached carton photo.",
            po_id=po.id,
            attachment_paths=[str(img_path)],
        ),
    )
    res_ok = engine_ok.process_inbound_supplier_email(seeded_session, email_ok)
    assert res_ok["alert_id"] is not None
    assert captured_images[0] == [("image/png", b"\x89PNG\r\n\x1a\nfake-png-bytes")]

    # 2. LLM failure degrades gracefully to AMBIGUOUS_EMAIL with zero PO mutation
    engine_fail = WorkflowEngine(lex_agent=LexAgent(llm_client=_FailingThenWorkingLLM(True)))
    email_fail = send_email(
        seeded_session,
        OutboundMessage(
            from_persona=PersonaKey.SUP1.value,
            to_personas=[PersonaKey.LEX.value],
            subject=f"Re: {po.po_number}",
            body="Confirmed.",
            po_id=po.id,
        ),
    )
    res_fail = engine_fail.process_inbound_supplier_email(seeded_session, email_fail)
    assert res_fail["triage"].intent == EmailIntent.AMBIGUOUS
    assert res_fail["alert_id"] is not None
    alert = seeded_session.get(ActionItem, res_fail["alert_id"])
    assert alert is not None
    assert alert.type == "AMBIGUOUS_EMAIL"
    assert po.status == POStatus.SENT_AWAITING_ACK.value
