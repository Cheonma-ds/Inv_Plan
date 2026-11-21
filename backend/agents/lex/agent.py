import json

from sqlalchemy.orm import Session

from backend.agents.base import BaseAgent
from backend.agents.lex.prompts import (
    DRAFT_SYSTEM_PROMPT,
    INVOICE_EXTRACTION_SYSTEM_PROMPT,
    TRIAGE_SYSTEM_PROMPT,
)
from backend.agents.lex.schemas import (
    AdditionalPOUpdate,
    EmailDraftInput,
    EmailDraftResult,
    EmailIntent,
    EmailTriageInput,
    EmailTriageResult,
    InvoiceExtractionResult,
    PDFDocumentType,
    StockRequestTriageInput,
    StockRequestTriageResult,
)
from backend.agents.lex.stock_request import run_stock_request_triage
from backend.agents.llm_client import GeminiLLMClient, LLMClientProtocol
from backend.core.enums import EmailCategory, PersonaKey
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
        pdf_bytes: bytes | None = None,
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
                pdf_bytes=pdf_bytes,
            )
            result = llm_res.parsed

            # Normalize additional_po_updates if top-level matched_po_number was omitted
            open_po_numbers = {po.po_number for po in triage_input.open_pos}
            raw_additional = list(result.additional_po_updates or [])
            if (
                not result.matched_po_number
                and result.intent == EmailIntent.AMBIGUOUS
                and raw_additional
            ):
                first = raw_additional[0]
                if (
                    first.matched_po_number in open_po_numbers
                    and first.confidence >= CONFIDENCE_THRESHOLD
                ):
                    result = result.model_copy(
                        update={
                            "matched_po_number": first.matched_po_number,
                            "intent": first.intent,
                            "confidence": first.confidence,
                            "proposed_unit_price": first.proposed_unit_price,
                            "proposed_currency": first.proposed_currency,
                            "proposed_qty": first.proposed_qty,
                            "proposed_eta_date": first.proposed_eta_date,
                            "promised_dispatch_date": first.promised_dispatch_date,
                            "actual_dispatch_date": first.actual_dispatch_date,
                            "carrier_name": first.carrier_name,
                            "tracking_number": first.tracking_number,
                            "details_promised_later": first.details_promised_later,
                            "details_promised_by_date": first.details_promised_by_date,
                            "delay_reason": first.delay_reason,
                            "summary_for_manager": (
                                first.summary_for_manager or result.summary_for_manager
                            ),
                            "suggested_action": first.suggested_action or result.suggested_action,
                        }
                    )
                    raw_additional = raw_additional[1:]

            # Sanitize & deduplicate additional_po_updates with per-PO guardrails
            seen_pos = {result.matched_po_number} if result.matched_po_number else set()
            clean_additional: list[AdditionalPOUpdate] = []
            for upd in raw_additional:
                po_ref = upd.matched_po_number.strip()
                if not po_ref or po_ref in seen_pos:
                    continue
                seen_pos.add(po_ref)
                upd_intent = upd.intent
                if (
                    upd.confidence < CONFIDENCE_THRESHOLD
                    or po_ref not in open_po_numbers
                ):
                    upd_intent = EmailIntent.AMBIGUOUS
                clean_additional.append(
                    upd.model_copy(update={"matched_po_number": po_ref, "intent": upd_intent})
                )

            # Hard Guardrail: confidence < 0.85 or unmatched PO forces safe AMBIGUOUS escalation
            forced_reasons: list[str] = []

            if (
                result.intent == EmailIntent.PRICE_CHANGE
                and result.proposed_unit_price is None
                and result.proposed_qty is None
            ):
                forced_reasons.append(
                    "PRICE_CHANGE classified without any proposed numeric price or quantity"
                )

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
                        "additional_po_updates": clean_additional,
                    }
                )
            elif clean_additional != result.additional_po_updates:
                result = result.model_copy(update={"additional_po_updates": clean_additional})

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

            clean_cc: list[str] = []
            for p in result.cc_personas:
                if p in VALID_PERSONA_VALUES and p not in clean_to and p not in clean_cc:
                    clean_cc.append(p)

            required_cc = list(draft_input.required_cc_personas or [])
            if PersonaKey.INVENTORY_MANAGER.value not in required_cc:
                required_cc.append(PersonaKey.INVENTORY_MANAGER.value)
            if draft_input.category == EmailCategory.EXPEDITE_REQUEST:
                if PersonaKey.PROCUREMENT.value not in required_cc:
                    required_cc.append(PersonaKey.PROCUREMENT.value)

            for req_p in required_cc:
                if (
                    req_p in VALID_PERSONA_VALUES
                    and req_p not in clean_to
                    and req_p not in clean_cc
                ):
                    clean_cc.append(req_p)

            subject = result.subject.strip()
            po_ref = draft_input.po_number.strip()
            if po_ref and po_ref.upper() != "N/A" and po_ref not in subject:
                subject = f"[{po_ref}] {subject}"
            elif (
                not po_ref
                and draft_input.part_sku
                and draft_input.part_sku.upper() != "GENERAL"
                and draft_input.part_sku not in subject
            ):
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

            # Normalize referenced_po_numbers (deduplicated, preserving order)
            refs: list[str] = []
            for raw_ref in [result.po_number, *(result.referenced_po_numbers or [])]:
                cleaned_ref = (raw_ref or "").strip()
                if cleaned_ref and cleaned_ref.upper() != "N/A" and cleaned_ref not in refs:
                    refs.append(cleaned_ref)

            # Guardrail: empty line items (except CREDIT_NOTE) or missing invoice number
            guardrail_notes: list[str] = []
            adjusted_confidence = result.confidence
            if (
                result.document_type != PDFDocumentType.CREDIT_NOTE
                and not result.line_items
            ):
                guardrail_notes.append("No line items extracted from PDF")
                adjusted_confidence = min(adjusted_confidence, 0.5)
            if not result.invoice_number.strip():
                guardrail_notes.append("Missing invoice_number in PDF")
                adjusted_confidence = min(adjusted_confidence, 0.5)

            updates: dict[str, object] = {}
            if refs != list(result.referenced_po_numbers or []):
                updates["referenced_po_numbers"] = refs
            if guardrail_notes:
                prefix = f"[Guardrail: {'; '.join(guardrail_notes)}]"
                merged_notes = f"{prefix} {result.notes or ''}".strip()
                updates["confidence"] = adjusted_confidence
                updates["notes"] = merged_notes
            if updates:
                result = result.model_copy(update=updates)

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
