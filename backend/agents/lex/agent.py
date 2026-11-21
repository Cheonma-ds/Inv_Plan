import json

from sqlalchemy.orm import Session

from backend.agents.base import BaseAgent
from backend.agents.lex.prompts import (
    DRAFT_SYSTEM_PROMPT,
    INVOICE_EXTRACTION_SYSTEM_PROMPT,
    TRIAGE_SYSTEM_PROMPT,
)
from backend.agents.lex.schemas import (
    EmailDraftInput,
    EmailDraftResult,
    EmailIntent,
    EmailTriageInput,
    EmailTriageResult,
    InvoiceExtractionResult,
    StockRequestTriageInput,
    StockRequestTriageResult,
)
from backend.agents.lex.stock_request import run_stock_request_triage
from backend.agents.llm_client import GeminiLLMClient, LLMClientProtocol
from backend.core.enums import PersonaKey
from backend.observability.tracer import AgentTracer

CONFIDENCE_THRESHOLD = 0.85
VALID_PERSONA_VALUES = {p.value for p in PersonaKey}


class LexAgent(BaseAgent):
    """Autonomous Supply Chain Assistant (Lex). Pure reasoning — zero direct DB mutations."""

    agent_name = "lex"
    eval_suite_path = "backend/evals/lex.yaml"

    def __init__(self, llm_client: LLMClientProtocol | None = None) -> None:
        self.llm: LLMClientProtocol = llm_client or GeminiLLMClient()

    def triage_email(
        self,
        session: Session,
        triage_input: EmailTriageInput,
        *,
        linked_comm_id: int | None = None,
    ) -> EmailTriageResult:
        prompt = json.dumps(triage_input.model_dump(mode="json"), indent=2)
        trigger = f"Inbound email from {triage_input.sender_name}: {triage_input.subject}"

        with AgentTracer.span(
            session,
            agent_name=self.agent_name,
            task_type="email_triage",
            trigger_summary=trigger,
            linked_entity_type="communication" if linked_comm_id else None,
            linked_entity_id=linked_comm_id,
        ) as span:
            span.input_summary = (
                f"From {triage_input.sender_persona} | Subject: {triage_input.subject}"
            )
            span.raw_prompt = prompt

            llm_res = self.llm.generate_structured(
                prompt=prompt,
                response_schema=EmailTriageResult,
                system_instruction=TRIAGE_SYSTEM_PROMPT,
                temperature=0.0,
            )
            result = llm_res.parsed

            # Hard Guardrail: confidence < 0.85 or unmatched PO forces safe AMBIGUOUS escalation
            open_po_numbers = {po.po_number for po in triage_input.open_pos}
            forced_reasons: list[str] = []

            if result.confidence < CONFIDENCE_THRESHOLD and result.intent != EmailIntent.AMBIGUOUS:
                forced_reasons.append(
                    f"Confidence {result.confidence:.2f} below {CONFIDENCE_THRESHOLD:.2f} threshold"
                )

            unmatched_po = (
                not result.matched_po_number or result.matched_po_number not in open_po_numbers
            )
            if (
                result.intent not in (EmailIntent.GENERAL_REPLY, EmailIntent.AMBIGUOUS)
                and unmatched_po
            ):
                forced_reasons.append(
                    f"PO reference '{result.matched_po_number}' does not match an open PO"
                )

            if forced_reasons:
                reason_text = "; ".join(forced_reasons)
                new_reason = f"[Guardrail Escalation: {reason_text}] {result.reasoning}".strip()
                result = result.model_copy(
                    update={
                        "intent": EmailIntent.AMBIGUOUS,
                        "reasoning": new_reason,
                    }
                )

            span.output_summary = (
                f"Intent={result.intent.value} | PO={result.matched_po_number or 'NONE'} | "
                f"Conf={result.confidence:.2f} | {result.summary_for_manager}"
            )
            span.confidence = result.confidence
            span.raw_response = llm_res.raw_text
            span.input_tokens = llm_res.input_tokens
            span.output_tokens = llm_res.output_tokens
            span.cost_inr = llm_res.cost_inr

        return result

    def triage_stock_request(
        self,
        session: Session,
        triage_input: StockRequestTriageInput,
        *,
        linked_comm_id: int | None = None,
    ) -> StockRequestTriageResult:
        return run_stock_request_triage(
            session,
            llm=self.llm,
            agent_name=self.agent_name,
            triage_input=triage_input,
            confidence_threshold=CONFIDENCE_THRESHOLD,
            linked_comm_id=linked_comm_id,
        )

    def draft_email(
        self,
        session: Session,
        draft_input: EmailDraftInput,
        *,
        linked_po_id: int | None = None,
    ) -> EmailDraftResult:
        prompt = json.dumps(draft_input.model_dump(mode="json"), indent=2)
        ref = draft_input.po_number.strip() or draft_input.part_sku
        trigger = f"Draft {draft_input.category.value} for {ref}"

        with AgentTracer.span(
            session,
            agent_name=self.agent_name,
            task_type="email_draft",
            trigger_summary=trigger,
            linked_entity_type="po" if linked_po_id else None,
            linked_entity_id=linked_po_id,
        ) as span:
            span.input_summary = (
                f"Category={draft_input.category.value} | Ref={ref} | "
                f"To={draft_input.recipient_persona}"
            )
            span.raw_prompt = prompt

            llm_res = self.llm.generate_structured(
                prompt=prompt,
                response_schema=EmailDraftResult,
                system_instruction=DRAFT_SYSTEM_PROMPT,
                temperature=0.2,
            )
            result = llm_res.parsed

            # Sanitize recipients & enforce manager visibility CC on supplier/making messages
            clean_to = [p for p in result.to_personas if p in VALID_PERSONA_VALUES]
            if not clean_to:
                clean_to = [draft_input.recipient_persona]

            clean_cc = [
                p
                for p in result.cc_personas
                if p in VALID_PERSONA_VALUES and p not in clean_to
            ]
            if (
                PersonaKey.INVENTORY_MANAGER.value not in clean_to
                and PersonaKey.INVENTORY_MANAGER.value not in clean_cc
            ):
                clean_cc.append(PersonaKey.INVENTORY_MANAGER.value)

            subject = result.subject.strip()
            po_ref = draft_input.po_number.strip()
            if po_ref and po_ref.upper() != "N/A" and po_ref not in subject:
                subject = f"[{po_ref}] {subject}"
            elif not po_ref and draft_input.part_sku and draft_input.part_sku not in subject:
                subject = f"[{draft_input.part_sku}] {subject}"

            result = result.model_copy(
                update={
                    "to_personas": clean_to,
                    "cc_personas": clean_cc,
                    "subject": subject,
                }
            )

            span.output_summary = (
                f"Draft '{result.subject}' -> {result.to_personas} (CC: {result.cc_personas})"
            )
            span.raw_response = llm_res.raw_text
            span.input_tokens = llm_res.input_tokens
            span.output_tokens = llm_res.output_tokens
            span.cost_inr = llm_res.cost_inr

        return result

    def extract_invoice(
        self,
        session: Session,
        pdf_bytes: bytes,
        *,
        filename: str = "invoice.pdf",
        supplier_name: str | None = None,
        linked_comm_id: int | None = None,
        linked_po_id: int | None = None,
    ) -> InvoiceExtractionResult:
        """Multimodal PDF invoice extraction (temp 0.0). Extracts numbers only — zero match math."""
        prompt_payload = {
            "task": (
                "Extract all invoice header and line-item fields verbatim "
                "from the attached PDF."
            ),
            "filename": filename,
            "sender_supplier_hint": supplier_name,
        }
        prompt = json.dumps(prompt_payload, indent=2)
        trigger = f"PDF invoice extraction: {filename}"

        with AgentTracer.span(
            session,
            agent_name=self.agent_name,
            task_type="invoice_extraction",
            trigger_summary=trigger,
            linked_entity_type="communication"
            if linked_comm_id
            else ("po" if linked_po_id else None),
            linked_entity_id=linked_comm_id or linked_po_id,
        ) as span:
            span.input_summary = f"PDF={filename} ({len(pdf_bytes)} bytes)"
            span.raw_prompt = prompt

            llm_res = self.llm.generate_structured(
                prompt=prompt,
                response_schema=InvoiceExtractionResult,
                system_instruction=INVOICE_EXTRACTION_SYSTEM_PROMPT,
                temperature=0.0,
                pdf_bytes=pdf_bytes,
            )
            result = llm_res.parsed

            # Guardrail: empty line items or missing invoice number cannot pass high-confidence gate
            guardrail_notes: list[str] = []
            adjusted_confidence = result.confidence
            if not result.line_items:
                guardrail_notes.append("No line items extracted from PDF")
                adjusted_confidence = min(adjusted_confidence, 0.5)
            if not result.invoice_number.strip():
                guardrail_notes.append("Missing invoice_number in PDF")
                adjusted_confidence = min(adjusted_confidence, 0.5)

            if guardrail_notes:
                prefix = f"[Guardrail: {'; '.join(guardrail_notes)}]"
                merged_notes = f"{prefix} {result.notes or ''}".strip()
                result = result.model_copy(
                    update={"confidence": adjusted_confidence, "notes": merged_notes}
                )

            span.output_summary = (
                f"Invoice={result.invoice_number or 'UNKNOWN'} | PO={result.po_number or 'NONE'} | "
                f"Lines={len(result.line_items)} | Subtotal={result.subtotal:.2f} | "
                f"Conf={result.confidence:.2f}"
            )
            span.confidence = result.confidence
            span.raw_response = llm_res.raw_text
            span.input_tokens = llm_res.input_tokens
            span.output_tokens = llm_res.output_tokens
            span.cost_inr = llm_res.cost_inr

        return result
