import json
import re

from sqlalchemy.orm import Session

from backend.agents.base import BaseAgent
from backend.agents.lex.prompts import (
    DRAFT_SYSTEM_PROMPT,
    INVOICE_EXTRACTION_SYSTEM_PROMPT,
    REMINDER_WAKEUP_SYSTEM_PROMPT,
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
    ReminderWakeupInput,
    ReminderWakeupResult,
    StockRequestTriageInput,
    StockRequestTriageResult,
)
from backend.agents.lex.stock_request import run_stock_request_triage
from backend.agents.llm_client import GeminiLLMClient, LLMClientProtocol
from backend.agents.tools import ToolRegistry
from backend.core.enums import EmailCategory, PersonaKey
from backend.observability.tracer import AgentTracer

CONFIDENCE_THRESHOLD = 0.85
VALID_PERSONA_VALUES = {p.value for p in PersonaKey}
_STANDARD_SIGNATURE = "Best regards,\n**Lex**\nAI Supply Chain Assistant"
_GREETING_RE = re.compile(r"^(dear|hello|hi|greetings)\b[^\n,]*,\s*(.+)$", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9*₹])")


def _bold_token(text: str, token: str) -> str:
    clean = token.strip()
    if not clean:
        return text
    pattern = re.compile(rf"(?<!\*)\b{re.escape(clean)}\b(?!\*)")
    return pattern.sub(f"**{clean}**", text)


def _format_and_verify_email_body(body: str, draft_input: EmailDraftInput) -> str:
    """Enforce structured Markdown business-email formatting, PO/SKU bolding, and currency."""
    text = (body or "").replace("\r\n", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)

    if not text:
        recipient = draft_input.recipient_name.strip() or "Team"
        text = f"Dear {recipient},\n\nPlease find the update below."

    # Ensure standalone salutation line followed by blank line
    lines = text.split("\n")
    first_line = lines[0].strip()
    if not re.match(r"^(dear|hello|hi|greetings)\b", first_line, re.IGNORECASE):
        recipient = draft_input.recipient_name.strip() or "Team"
        text = f"Dear {recipient},\n\n{text}"
    else:
        m = _GREETING_RE.match(first_line)
        if m and m.group(2).strip():
            salutation = first_line[: first_line.index(",") + 1].strip()
            rest_first = m.group(2).strip()
            if rest_first and rest_first[0].islower():
                rest_first = rest_first[0].upper() + rest_first[1:]
            remaining = "\n".join(lines[1:]).strip()
            rest_block = f"{rest_first}\n{remaining}".strip() if remaining else rest_first
            text = f"{salutation}\n\n{rest_block}"
        elif len(lines) > 1 and lines[1].strip() != "":
            text = f"{first_line}\n\n" + "\n".join(lines[1:]).strip()

    # Break single wall-of-text paragraphs into readable \n\n paragraphs
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    expanded: list[str] = []
    for para in paragraphs:
        if len(para) > 180 and "\n" not in para and not para.startswith("- "):
            sentences = _SENTENCE_SPLIT_RE.split(para)
            if len(sentences) >= 2:
                mid = max(1, len(sentences) // 2)
                expanded.append(" ".join(sentences[:mid]).strip())
                expanded.append(" ".join(sentences[mid:]).strip())
                continue
        expanded.append(para)
    text = "\n\n".join(expanded)

    # Currency grounding: prevent USD/$ leakage on INR POs
    if draft_input.currency.strip().upper() == "INR":
        text = re.sub(r"\bUSD\s*", "INR ", text, flags=re.IGNORECASE)
        text = text.replace("$", "₹")

    # Bold PO number & SKU; ensure PO number is grounded if provided
    po_ref = draft_input.po_number.strip()
    if po_ref and po_ref.upper() != "N/A":
        if po_ref not in text:
            text = f"{text}\n\n- **PO Reference:** **{po_ref}**"
        else:
            text = _bold_token(text, po_ref)

    sku = draft_input.part_sku.strip()
    if sku and sku.upper() != "GENERAL":
        text = _bold_token(text, sku)

    # Ensure standard sign-off block
    tail_lower = text.split("\n\n")[-1].lower()
    if not any(sig in tail_lower for sig in ("best regards", "regards,", "sincerely,", "lex")):
        text = f"{text}\n\n{_STANDARD_SIGNATURE}"

    return re.sub(r"\n{3,}", "\n\n", text).strip()


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
        image_attachments: list[tuple[str, bytes]] | None = None,
        tool_registry: ToolRegistry | None = None,
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
            img_note = f" | Images={len(image_attachments)}" if image_attachments else ""
            span.input_summary = (
                f"From {triage_input.sender_persona} | Subject: {triage_input.subject}{img_note}"
            )
            span.raw_prompt = prompt

            llm_kwargs: dict[str, object] = {
                "prompt": prompt,
                "response_schema": EmailTriageResult,
                "system_instruction": TRIAGE_SYSTEM_PROMPT,
                "temperature": 0.0,
                "pdf_bytes": pdf_bytes,
            }
            if image_attachments:
                llm_kwargs["image_attachments"] = image_attachments
            if tool_registry is not None and hasattr(self.llm, "generate_with_tools"):
                llm_res = self.llm.generate_with_tools(
                    **llm_kwargs,  # type: ignore[arg-type]
                    tool_registry=tool_registry,
                )
                span.tool_calls.extend(llm_res.tool_calls)
            else:
                llm_res = self.llm.generate_structured(**llm_kwargs)  # type: ignore[arg-type]
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
                            "details_promised_days": first.details_promised_days,
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
                    or (
                        upd_intent == EmailIntent.PRICE_CHANGE
                        and upd.proposed_unit_price is None
                        and upd.proposed_qty is None
                    )
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
        tool_registry: ToolRegistry | None = None,
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

            if tool_registry is not None and hasattr(self.llm, "generate_with_tools"):
                llm_res = self.llm.generate_with_tools(
                    prompt=prompt,
                    response_schema=EmailDraftResult,
                    tool_registry=tool_registry,
                    system_instruction=DRAFT_SYSTEM_PROMPT,
                    temperature=0.2,
                )
                span.tool_calls.extend(llm_res.tool_calls)
            else:
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

            formatted_body = _format_and_verify_email_body(result.body, draft_input)

            result = result.model_copy(
                update={
                    "to_personas": clean_to,
                    "cc_personas": clean_cc,
                    "subject": subject,
                    "body": formatted_body,
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

    def inspect_reminder_wakeup(
        self,
        session: Session,
        wakeup_input: ReminderWakeupInput,
        *,
        tool_registry: ToolRegistry | None = None,
        linked_po_id: int | None = None,
    ) -> ReminderWakeupResult:
        """Inspect live PO/GRN/thread state on scheduled LexReminder wakeup."""
        prompt = json.dumps(wakeup_input.model_dump(mode="json"), indent=2)
        trigger = (
            f"Reminder wakeup #{wakeup_input.reminder_id} for "
            f"{wakeup_input.po_number} ({wakeup_input.part_sku})"
        )

        with AgentTracer.span(
            session,
            agent_name=self.agent_name,
            task_type="reminder_wakeup",
            trigger_summary=trigger,
            linked_entity_type="po" if (linked_po_id or wakeup_input.po_id) else None,
            linked_entity_id=linked_po_id or wakeup_input.po_id,
        ) as span:
            span.input_summary = (
                f"PO={wakeup_input.po_number} | Status={wakeup_input.po_status} | "
                f"GRNs={wakeup_input.grn_count} | Missing={wakeup_input.missing_dispatch_fields}"
            )
            span.raw_prompt = prompt

            if tool_registry is not None and hasattr(self.llm, "generate_with_tools"):
                llm_res = self.llm.generate_with_tools(
                    prompt=prompt,
                    response_schema=ReminderWakeupResult,
                    tool_registry=tool_registry,
                    system_instruction=REMINDER_WAKEUP_SYSTEM_PROMPT,
                    temperature=0.0,
                )
                span.tool_calls.extend(llm_res.tool_calls)
            else:
                llm_res = self.llm.generate_structured(
                    prompt=prompt,
                    response_schema=ReminderWakeupResult,
                    system_instruction=REMINDER_WAKEUP_SYSTEM_PROMPT,
                    temperature=0.0,
                )
            result = llm_res.parsed

            # Deterministic safety guard: never draft follow-up if GRN logged or details complete
            if (
                wakeup_input.grn_count > 0
                or not wakeup_input.missing_dispatch_fields
                or wakeup_input.po_status in ("CLOSED", "CANCELLED")
            ) and result.should_draft_followup:
                result = result.model_copy(
                    update={
                        "should_draft_followup": False,
                        "reasoning": (
                            f"[Guardrail Override: PO={wakeup_input.po_status}, "
                            f"GRNs={wakeup_input.grn_count}, "
                            f"Missing={wakeup_input.missing_dispatch_fields}] {result.reasoning}"
                        ).strip(),
                    }
                )

            span.output_summary = (
                f"DraftFollowup={result.should_draft_followup} | {result.summary_for_manager}"
            )
            span.raw_response = llm_res.raw_text
            span.input_tokens = llm_res.input_tokens
            span.output_tokens = llm_res.output_tokens
            span.cost_inr = llm_res.cost_inr

        return result
