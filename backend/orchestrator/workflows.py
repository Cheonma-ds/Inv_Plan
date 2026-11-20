import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.agents.lex import (
    CONFIDENCE_THRESHOLD,
    EmailDraftInput,
    EmailIntent,
    EmailTriageInput,
    EmailTriageResult,
    InvoiceExtractionResult,
    LexAgent,
    OpenPOContext,
    ThreadMessageContext,
)
from backend.core import clock
from backend.core.enums import (
    ActionItemStatus,
    AlertSeverity,
    ApprovalOutcome,
    Criticality,
    EmailCategory,
    PersonaKey,
    POStatus,
    SupplierMemorySource,
)
from backend.core.models import (
    ActionItem,
    GoodsReceipt,
    Invoice,
    Part,
    PartSupplierTerm,
    PurchaseOrder,
    Supplier,
    SupplierMemoryNote,
)
from backend.inventory import ConsumeResult, consume_stock, update_rop_and_replenishment
from backend.mailroom import (
    OutboundMessage,
    StoredEmail,
    annotate_triage,
    approve_draft,
    get_thread,
    reject_draft,
    save_draft,
    send_email,
)
from backend.mailroom.transport import SUPPLIER_PERSONAS
from backend.observability import record_approval_outcome
from backend.orchestrator.sla_watcher import SLAEvent, auto_resolve_po_alerts, run_sla_watcher
from backend.planning import evaluate_part_recommendation, recalculate_all
from backend.po import (
    ThreeWayMatchResult,
    append_po_note,
    create_action_item,
    create_po,
    record_goods_receipt,
    resolve_action_item,
    submit_invoice_and_match,
    switch_po_to_backup_supplier,
    transition_po,
    update_po_terms,
)

EXCEPTION_INTENTS = {
    EmailIntent.PRICE_CHANGE,
    EmailIntent.LEAD_TIME_CHANGE,
    EmailIntent.PARTIAL_AVAILABILITY,
    EmailIntent.OUT_OF_STOCK,
}


def _parse_iso_date_utc(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        cleaned = date_str.strip()
        if len(cleaned) == 10:
            dt = datetime.fromisoformat(f"{cleaned}T17:00:00+00:00")
        else:
            dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _supplier_memory_texts(session: Session, supplier_id: int) -> list[str]:
    rows = session.scalars(
        select(SupplierMemoryNote.note)
        .where(SupplierMemoryNote.supplier_id == supplier_id)
        .order_by(SupplierMemoryNote.id.asc())
    ).all()
    return list(rows)


def _quoted_lead_time_days(session: Session, part_id: int, supplier_id: int) -> int:
    term = session.scalar(
        select(PartSupplierTerm).where(
            PartSupplierTerm.part_id == part_id,
            PartSupplierTerm.supplier_id == supplier_id,
        )
    )
    return term.quoted_lead_time_days if term is not None else 14


def _backup_supplier_info(
    session: Session, part_id: int, current_supplier_id: int
) -> dict[str, Any]:
    term = session.scalar(
        select(PartSupplierTerm)
        .where(
            PartSupplierTerm.part_id == part_id,
            PartSupplierTerm.supplier_id != current_supplier_id,
        )
        .order_by(PartSupplierTerm.supplier_rank.asc())
        .limit(1)
    )
    if term is None:
        return {}
    sup = session.get(Supplier, term.supplier_id)
    return {
        "backup_supplier_id": term.supplier_id,
        "backup_supplier_name": sup.name if sup else None,
        "backup_unit_price": term.agreed_unit_price,
        "backup_lead_time_days": term.quoted_lead_time_days,
    }


def _save_learned_memory_note(session: Session, supplier_id: int, note_text: str | None) -> None:
    if not note_text or not note_text.strip():
        return
    cleaned = note_text.strip()
    existing = _supplier_memory_texts(session, supplier_id)
    if any(n.strip().lower() == cleaned.lower() for n in existing):
        return
    sim_now = clock.now(session)
    session.add(
        SupplierMemoryNote(
            supplier_id=supplier_id,
            note=cleaned,
            source=SupplierMemorySource.LEX_LEARNED.value,
            created_at=sim_now,
            updated_at=sim_now,
        )
    )
    session.flush()


class WorkflowEngine:
    """Single orchestration layer composing inventory, po, mailroom, observability, and Lex."""

    def __init__(self, lex_agent: LexAgent | None = None) -> None:
        self.lex = lex_agent or LexAgent()

    def _create_lex_draft(
        self,
        session: Session,
        *,
        po: PurchaseOrder,
        part: Part,
        supplier: Supplier,
        category: EmailCategory,
        context_facts: dict[str, Any],
        thread_id: str | None = None,
        thread_subject: str | None = None,
    ) -> StoredEmail:
        sim_now = clock.now(session)
        draft_res = self.lex.draft_email(
            session,
            EmailDraftInput(
                category=category,
                po_number=po.po_number,
                part_sku=part.sku,
                part_name=part.name,
                qty=po.qty,
                unit_price=po.unit_price,
                recipient_persona=supplier.persona_key,
                recipient_name=supplier.name,
                current_date=sim_now.date().isoformat(),
                thread_subject=thread_subject,
                supplier_memory_notes=_supplier_memory_texts(session, supplier.id),
                context_facts=context_facts,
            ),
            linked_po_id=po.id,
        )
        return save_draft(
            session,
            OutboundMessage(
                from_persona=PersonaKey.LEX,
                to_personas=[PersonaKey(p) for p in draft_res.to_personas],
                cc_personas=[PersonaKey(p) for p in draft_res.cc_personas],
                subject=draft_res.subject,
                body=draft_res.body,
                po_id=po.id,
                thread_id=thread_id,
                category=category,
            ),
        )

    def withdraw_stock(
        self,
        session: Session,
        *,
        part_id: int,
        qty: int,
        note: str = "",
    ) -> dict[str, Any]:
        consume_res: ConsumeResult = consume_stock(session, part_id, qty, note=note)
        created_po: PurchaseOrder | None = None
        draft_email: StoredEmail | None = None
        created_alerts: list[ActionItem] = []

        if consume_res.reorder_triggered is not None:
            trig = consume_res.reorder_triggered
            created_po = create_po(
                session,
                part_id=trig.part_id,
                supplier_id=trig.primary_supplier_id,
                qty=trig.order_qty,
                unit_price=trig.agreed_unit_price,
                initial_status=POStatus.PENDING_DISPATCH_APPROVAL,
                notes=(
                    f"Auto-triggered replenishment "
                    f"(IP={trig.inventory_position_before} < ROP={trig.rop})."
                ),
            )
            part = session.get(Part, trig.part_id)
            supplier = session.get(Supplier, trig.primary_supplier_id)
            assert part is not None and supplier is not None

            draft_email = self._create_lex_draft(
                session,
                po=created_po,
                part=part,
                supplier=supplier,
                category=EmailCategory.PO_DISPATCH,
                context_facts={
                    "order_qty": created_po.qty,
                    "unit_price": created_po.unit_price,
                    "total_value": round(created_po.qty * created_po.unit_price, 2),
                    "quoted_lead_time_days": trig.quoted_lead_time_days,
                    "part_criticality": part.criticality,
                },
            )
            alert = create_action_item(
                session,
                type="PO_DISPATCH_APPROVAL",
                severity=(
                    AlertSeverity.HIGH
                    if part.criticality == Criticality.CRITICAL.value
                    else AlertSeverity.MEDIUM
                ),
                title=(
                    f"Approve PO dispatch: {created_po.po_number} "
                    f"({part.sku}, {created_po.qty} units)"
                ),
                summary=(
                    f"Stock for {part.name} ({part.sku}) fell below ROP ({part.rop}). "
                    f"Created {created_po.po_number} for {created_po.qty} units to {supplier.name} "
                    f"(₹{created_po.unit_price:,.2f}/unit). Approve Lex's dispatch email."
                ),
                po_id=created_po.id,
                part_id=part.id,
                draft_comm_id=draft_email.id,
                suggested_actions=[
                    {"action": "approve_draft", "label": "Approve & Send PO"},
                    {"action": "reject_draft", "label": "Reject Draft"},
                ],
                cta_payload={"draft_comm_id": draft_email.id, "po_id": created_po.id},
            )
            created_alerts.append(alert)

        if consume_res.accelerated_burn is not None:
            burn = consume_res.accelerated_burn
            earliest_po_id = burn.open_po_ids[0] if burn.open_po_ids else None
            alert = create_action_item(
                session,
                type="ACCELERATED_BURN",
                severity=AlertSeverity.CRITICAL,
                title=f"Accelerated burn: {burn.sku} hit 0 on-hand units",
                summary=(
                    f"{burn.sku} reached 0 on-hand units with {burn.days_until_eta:.1f} days "
                    "until the earliest open PO arrives. No duplicate PO was raised; "
                    "expedite review is recommended."
                ),
                po_id=earliest_po_id,
                part_id=burn.part_id,
                suggested_actions=[{"action": "add_note", "label": "Log Expedite Decision"}],
                cta_payload={"po_id": earliest_po_id, "part_id": burn.part_id},
            )
            created_alerts.append(alert)

        return {
            "consume": consume_res,
            "created_po": created_po,
            "draft_email": draft_email,
            "alerts": created_alerts,
        }

    def send_mailroom_email(
        self,
        session: Session,
        msg: OutboundMessage,
    ) -> dict[str, Any]:
        stored = send_email(session, msg, allow_agent_send=False)
        triage_outcome: dict[str, Any] | None = None

        recipients = set(stored.to_personas) | set(stored.cc_personas)
        if stored.from_persona in SUPPLIER_PERSONAS and PersonaKey.LEX.value in recipients:
            triage_outcome = self.process_inbound_supplier_email(session, stored)

        return {
            "email": stored,
            "triage": triage_outcome,
        }

    def process_inbound_supplier_email(
        self,
        session: Session,
        email: StoredEmail,
    ) -> dict[str, Any]:
        sim_now = clock.now(session)
        supplier = session.scalar(
            select(Supplier).where(Supplier.persona_key == email.from_persona)
        )
        if supplier is None:
            return {"status": "ignored", "reason": f"Unknown supplier persona {email.from_persona}"}

        open_po_rows = session.scalars(
            select(PurchaseOrder)
            .where(
                PurchaseOrder.supplier_id == supplier.id,
                PurchaseOrder.status.notin_((POStatus.CLOSED.value, POStatus.CANCELLED.value)),
            )
            .order_by(PurchaseOrder.id.desc())
        ).all()

        open_po_contexts: list[OpenPOContext] = []
        po_by_number: dict[str, PurchaseOrder] = {}
        for po in open_po_rows:
            part = session.get(Part, po.part_id)
            if part is None:
                continue
            po_by_number[po.po_number] = po
            quoted_lt = _quoted_lead_time_days(session, part.id, supplier.id)
            promised = (
                clock.ensure_utc(po.promised_eta).date().isoformat() if po.promised_eta else None
            )
            revised = (
                clock.ensure_utc(po.revised_eta).date().isoformat() if po.revised_eta else None
            )
            open_po_contexts.append(
                OpenPOContext(
                    po_number=po.po_number,
                    part_sku=part.sku,
                    part_name=part.name,
                    qty=po.qty,
                    unit_price=po.unit_price,
                    status=po.status,
                    quoted_lead_time_days=quoted_lt,
                    promised_eta=promised,
                    revised_eta=revised,
                )
            )

        thread_emails = get_thread(session, email.thread_id)
        thread_history = [
            ThreadMessageContext(
                from_persona=m.from_persona,
                subject=m.subject,
                body=m.body,
                timestamp=m.timestamp.isoformat(),
            )
            for m in thread_emails
            if m.id != email.id
        ]

        triage_result: EmailTriageResult = self.lex.triage_email(
            session,
            EmailTriageInput(
                sender_persona=email.from_persona,
                sender_name=supplier.name,
                subject=email.subject,
                body=email.body,
                has_pdf_attachment=bool(email.attachment_path),
                current_date=sim_now.date().isoformat(),
                open_pos=open_po_contexts,
                thread_history=thread_history,
                supplier_memory_notes=_supplier_memory_texts(session, supplier.id),
            ),
            linked_comm_id=email.id,
        )

        matched_po = (
            po_by_number.get(triage_result.matched_po_number)
            if triage_result.matched_po_number
            else None
        )

        annotate_triage(
            session,
            email.id,
            classification=triage_result.intent.value,
            extracted_json=triage_result.model_dump(mode="json"),
            confidence=triage_result.confidence,
            po_id=matched_po.id if matched_po else email.po_id,
        )

        _save_learned_memory_note(session, supplier.id, triage_result.new_memory_note)

        created_alert: ActionItem | None = None
        created_draft: StoredEmail | None = None

        # If a PDF attachment is present and triage either classified INVOICE_SUBMISSION or
        # escalated AMBIGUOUS solely because the email body omitted the PO number, run the
        # multimodal PDF invoice extraction pipeline first so the PDF PO reference can be used.
        should_run_invoice_pipeline = triage_result.intent == EmailIntent.INVOICE_SUBMISSION or (
            bool(email.attachment_path)
            and triage_result.intent == EmailIntent.AMBIGUOUS
            and matched_po is None
        )
        if should_run_invoice_pipeline:
            return self._process_pdf_invoice_email(
                session,
                email=email,
                supplier=supplier,
                matched_po=matched_po,
                po_by_number=po_by_number,
                triage_result=triage_result,
            )

        # Route intent with deterministic guardrails
        if triage_result.intent == EmailIntent.AMBIGUOUS or matched_po is None:
            if triage_result.intent == EmailIntent.GENERAL_REPLY and matched_po is None:
                created_alert = create_action_item(
                    session,
                    type="SUPPLIER_MESSAGE",
                    severity=AlertSeverity.INFO,
                    title=f"Message from {supplier.name}: {email.subject}",
                    summary=triage_result.summary_for_manager,
                    suggested_actions=[],
                    cta_payload={"comm_id": email.id},
                )
            else:
                created_alert = create_action_item(
                    session,
                    type="AMBIGUOUS_EMAIL",
                    severity=AlertSeverity.HIGH,
                    title=f"Manual review needed: email from {supplier.name}",
                    summary=(
                        f"{triage_result.summary_for_manager} "
                        f"(Confidence: {triage_result.confidence:.0%}. No PO state was changed.)"
                    ),
                    po_id=matched_po.id if matched_po else None,
                    suggested_actions=[{"action": "add_note", "label": "Log Review Note"}],
                    cta_payload={"comm_id": email.id, "reasoning": triage_result.reasoning},
                )
            return {
                "triage": triage_result,
                "matched_po_id": matched_po.id if matched_po else None,
                "alert_id": created_alert.id if created_alert else None,
                "draft_comm_id": None,
            }

        part = session.get(Part, matched_po.part_id)
        assert part is not None

        if triage_result.intent == EmailIntent.ACKNOWLEDGED:
            if matched_po.status in (
                POStatus.SENT_AWAITING_ACK.value,
                POStatus.UNRESPONSIVE_ESCALATED.value,
            ):
                quoted_lt = _quoted_lead_time_days(session, part.id, supplier.id)
                promised_eta = _parse_iso_date_utc(triage_result.proposed_eta_date) or (
                    clock.ensure_utc(matched_po.sent_at or sim_now) + timedelta(days=quoted_lt)
                )
                transition_po(
                    session,
                    matched_po,
                    POStatus.ACKNOWLEDGED,
                    promised_eta=promised_eta,
                    note=f"Supplier acknowledged via email #{email.id}.",
                )
                matched_po.next_sla_check_at = None
                auto_resolve_po_alerts(
                    session,
                    matched_po.id,
                    ("ACK_REMINDER_DRAFT", "UNRESPONSIVE_SUPPLIER"),
                )

        elif triage_result.intent in EXCEPTION_INTENTS:
            if matched_po.status in (
                POStatus.SENT_AWAITING_ACK.value,
                POStatus.UNRESPONSIVE_ESCALATED.value,
            ):
                transition_po(
                    session,
                    matched_po,
                    POStatus.EXCEPTION_REVIEW,
                    note=(
                        f"Supplier raised {triage_result.intent.value}: "
                        f"{triage_result.summary_for_manager}"
                    ),
                )
            backup = _backup_supplier_info(session, part.id, supplier.id)
            suggested = []
            if triage_result.intent != EmailIntent.OUT_OF_STOCK:
                suggested.append({"action": "update_po", "label": "Accept & Update PO"})
            if backup.get("backup_supplier_id"):
                suggested.append(
                    {
                        "action": "switch_backup_supplier",
                        "label": f"Switch to {backup['backup_supplier_name']}",
                    }
                )
            suggested.append({"action": "add_note", "label": "Add Note"})

            severity = (
                AlertSeverity.CRITICAL
                if (
                    part.criticality == Criticality.CRITICAL.value
                    or triage_result.intent == EmailIntent.OUT_OF_STOCK
                )
                else AlertSeverity.HIGH
            )
            intent_label = triage_result.intent.value.replace("_", " ").title()
            created_alert = create_action_item(
                session,
                type="PO_EXCEPTION",
                severity=severity,
                title=f"{intent_label}: {matched_po.po_number} ({part.sku})",
                summary=triage_result.summary_for_manager,
                po_id=matched_po.id,
                part_id=part.id,
                suggested_actions=suggested,
                cta_payload={
                    "po_id": matched_po.id,
                    "intent": triage_result.intent.value,
                    "proposed_unit_price": triage_result.proposed_unit_price,
                    "proposed_qty": triage_result.proposed_qty,
                    "proposed_eta_date": triage_result.proposed_eta_date,
                    **backup,
                },
            )

        elif triage_result.intent == EmailIntent.POST_ACK_DELAY:
            base_eta = clock.ensure_utc(
                matched_po.revised_eta or matched_po.promised_eta or sim_now
            )
            revised_eta = _parse_iso_date_utc(triage_result.proposed_eta_date) or (
                base_eta + timedelta(days=7)
            )
            is_critical_impact = (
                part.criticality == Criticality.CRITICAL.value or part.on_hand_stock <= part.rop
            )

            if is_critical_impact:
                delay_detail = triage_result.delay_reason or triage_result.summary_for_manager
                if matched_po.status == POStatus.ACKNOWLEDGED.value:
                    transition_po(
                        session,
                        matched_po,
                        POStatus.DELAYED_CRITICAL,
                        revised_eta=revised_eta,
                        note=f"Critical post-ack delay reported: {delay_detail}",
                    )
                else:
                    matched_po.revised_eta = revised_eta
                    append_po_note(
                        session,
                        matched_po,
                        f"Revised ETA updated to {revised_eta.date().isoformat()}: {delay_detail}",
                    )

                promised_str = (
                    clock.ensure_utc(matched_po.promised_eta).date().isoformat()
                    if matched_po.promised_eta
                    else None
                )
                created_draft = self._create_lex_draft(
                    session,
                    po=matched_po,
                    part=part,
                    supplier=supplier,
                    category=EmailCategory.EXPEDITE_REQUEST,
                    thread_id=email.thread_id,
                    thread_subject=email.subject,
                    context_facts={
                        "promised_eta": promised_str,
                        "revised_eta": revised_eta.date().isoformat(),
                        "delay_reason": triage_result.delay_reason,
                        "part_criticality": part.criticality,
                        "on_hand_stock": part.on_hand_stock,
                        "rop": part.rop,
                    },
                )
                backup = _backup_supplier_info(session, part.id, supplier.id)
                suggested = [
                    {"action": "approve_draft", "label": "Approve & Send Expedite Request"}
                ]
                if backup.get("backup_supplier_id"):
                    suggested.append(
                        {
                            "action": "switch_backup_supplier",
                            "label": f"Switch to {backup['backup_supplier_name']}",
                        }
                    )
                created_alert = create_action_item(
                    session,
                    type="CRITICAL_DELAY",
                    severity=AlertSeverity.CRITICAL,
                    title=(
                        f"CRITICAL delay on {matched_po.po_number} ({part.sku}) "
                        "— Escalate to Procurement"
                    ),
                    summary=(
                        f"{supplier.name} delayed {matched_po.po_number} to "
                        f"{revised_eta.date().isoformat()} "
                        f"(on-hand: {part.on_hand_stock}, ROP: {part.rop}). "
                        "Lex drafted an expedite request for approval."
                    ),
                    po_id=matched_po.id,
                    part_id=part.id,
                    assigned_team="Procurement",
                    draft_comm_id=created_draft.id,
                    suggested_actions=suggested,
                    cta_payload={
                        "draft_comm_id": created_draft.id,
                        "po_id": matched_po.id,
                        "revised_eta": revised_eta.date().isoformat(),
                        **backup,
                    },
                )
            else:
                matched_po.revised_eta = revised_eta
                append_po_note(
                    session,
                    matched_po,
                    f"Minor delay accepted (revised ETA {revised_eta.date().isoformat()}).",
                )
                created_alert = create_action_item(
                    session,
                    type="MINOR_DELAY_ACCEPTED",
                    severity=AlertSeverity.INFO,
                    title=f"Minor delay noted on {matched_po.po_number} ({part.sku})",
                    summary=(
                        f"{supplier.name} revised ETA for {matched_po.po_number} to "
                        f"{revised_eta.date().isoformat()}. Part is {part.criticality} with "
                        f"sufficient stock ({part.on_hand_stock} on hand > ROP {part.rop}); "
                        "no expedite required."
                    ),
                    po_id=matched_po.id,
                    part_id=part.id,
                    suggested_actions=[],
                    cta_payload={
                        "po_id": matched_po.id,
                        "revised_eta": revised_eta.date().isoformat(),
                    },
                )

        elif triage_result.intent == EmailIntent.GENERAL_REPLY:
            created_alert = create_action_item(
                session,
                type="SUPPLIER_MESSAGE",
                severity=AlertSeverity.INFO,
                title=f"Supplier update on {matched_po.po_number}",
                summary=triage_result.summary_for_manager,
                po_id=matched_po.id,
                part_id=part.id,
                suggested_actions=[],
                cta_payload={"po_id": matched_po.id, "comm_id": email.id},
            )

        session.flush()
        return {
            "triage": triage_result,
            "matched_po_id": matched_po.id,
            "alert_id": created_alert.id if created_alert else None,
            "draft_comm_id": created_draft.id if created_draft else None,
        }

    def _process_pdf_invoice_email(
        self,
        session: Session,
        *,
        email: StoredEmail,
        supplier: Supplier,
        matched_po: PurchaseOrder | None,
        po_by_number: dict[str, PurchaseOrder],
        triage_result: EmailTriageResult,
    ) -> dict[str, Any]:
        """Extract PDF invoice via Lex, run 3-way match, and drive quarantine/release."""
        if not email.attachment_path:
            alert = create_action_item(
                session,
                type="INVOICE_MISSING_PDF",
                severity=AlertSeverity.MEDIUM,
                title=(
                    "Invoice email missing PDF attachment"
                    + (f" ({matched_po.po_number})" if matched_po else "")
                ),
                summary=(
                    f"{supplier.name} sent an invoice email without a PDF attachment. "
                    "No PO state was modified; request a PDF invoice before matching."
                ),
                po_id=matched_po.id if matched_po else None,
                part_id=matched_po.part_id if matched_po else None,
                suggested_actions=[{"action": "add_note", "label": "Log Review Note"}],
                cta_payload={"comm_id": email.id, "po_id": matched_po.id if matched_po else None},
            )
            session.flush()
            return {
                "triage": triage_result,
                "matched_po_id": matched_po.id if matched_po else None,
                "alert_id": alert.id,
                "draft_comm_id": None,
                "invoice_extraction": None,
                "invoice_id": None,
                "match_result": None,
            }

        pdf_file = Path(email.attachment_path)
        if not pdf_file.is_file():
            alert = create_action_item(
                session,
                type="INVOICE_EXTRACTION_REVIEW",
                severity=AlertSeverity.HIGH,
                title=f"Unreadable invoice PDF from {supplier.name}",
                summary=(
                    f"Attachment '{email.attachment_path}' could not be read from disk. "
                    "No PO state was modified."
                ),
                po_id=matched_po.id if matched_po else None,
                part_id=matched_po.part_id if matched_po else None,
                suggested_actions=[{"action": "add_note", "label": "Log Review Note"}],
                cta_payload={"comm_id": email.id, "attachment_path": email.attachment_path},
            )
            session.flush()
            return {
                "triage": triage_result,
                "matched_po_id": matched_po.id if matched_po else None,
                "alert_id": alert.id,
                "draft_comm_id": None,
                "invoice_extraction": None,
                "invoice_id": None,
                "match_result": None,
            }

        extraction: InvoiceExtractionResult = self.lex.extract_invoice(
            session,
            pdf_file.read_bytes(),
            filename=pdf_file.name,
            supplier_name=supplier.name,
            linked_comm_id=email.id,
            linked_po_id=matched_po.id if matched_po else None,
        )

        # Resolve target PO and guard against conflicting PO references between email & PDF
        target_po = matched_po
        po_conflict_reason: str | None = None
        if extraction.po_number:
            pdf_po = po_by_number.get(extraction.po_number)
            if target_po is None:
                target_po = pdf_po
            elif extraction.po_number != target_po.po_number:
                po_conflict_reason = (
                    f"Email referenced {target_po.po_number}, but PDF invoice references "
                    f"{extraction.po_number}."
                )

        annotate_triage(
            session,
            email.id,
            classification=EmailIntent.INVOICE_SUBMISSION.value
            if extraction.confidence >= CONFIDENCE_THRESHOLD and target_po is not None
            else EmailIntent.AMBIGUOUS.value,
            extracted_json={
                "triage": triage_result.model_dump(mode="json"),
                "invoice_extraction": extraction.model_dump(mode="json"),
            },
            confidence=min(triage_result.confidence, extraction.confidence)
            if matched_po is not None
            else extraction.confidence,
            po_id=target_po.id if target_po else email.po_id,
        )

        # Guardrail: confidence < 0.85, unresolved PO, PO conflict, or empty lines -> zero mutation
        escalation_reasons: list[str] = []
        if extraction.confidence < CONFIDENCE_THRESHOLD:
            escalation_reasons.append(
                f"PDF extraction confidence {extraction.confidence:.0%} is below "
                f"{CONFIDENCE_THRESHOLD:.0%}"
            )
        if po_conflict_reason:
            escalation_reasons.append(po_conflict_reason)
        if target_po is None:
            escalation_reasons.append(
                f"Could not match PDF PO ref '{extraction.po_number or 'NONE'}' to an open PO "
                f"for {supplier.name}"
            )
        if not extraction.line_items:
            escalation_reasons.append("No billable line items were extracted from the PDF")

        if escalation_reasons:
            alert = create_action_item(
                session,
                type="INVOICE_EXTRACTION_REVIEW",
                severity=AlertSeverity.HIGH,
                title=f"Manual invoice review needed ({supplier.name})",
                summary=(
                    f"Lex could not safely auto-match PDF '{pdf_file.name}': "
                    f"{'; '.join(escalation_reasons)}. Zero PO state changes were made."
                ),
                po_id=target_po.id if target_po else None,
                part_id=target_po.part_id if target_po else None,
                suggested_actions=[{"action": "add_note", "label": "Log Review Note"}],
                cta_payload={
                    "comm_id": email.id,
                    "attachment_path": email.attachment_path,
                    "invoice_number": extraction.invoice_number,
                    "pdf_po_number": extraction.po_number,
                    "confidence": extraction.confidence,
                    "reasons": escalation_reasons,
                },
            )
            session.flush()
            return {
                "triage": triage_result,
                "matched_po_id": target_po.id if target_po else None,
                "alert_id": alert.id,
                "draft_comm_id": None,
                "invoice_extraction": extraction,
                "invoice_id": None,
                "match_result": None,
            }

        assert target_po is not None
        part = session.get(Part, target_po.part_id)
        assert part is not None

        if len(extraction.line_items) == 1:
            extracted_qty = extraction.line_items[0].qty
            extracted_unit_price = extraction.line_items[0].unit_price
        else:
            extracted_qty = sum(li.qty for li in extraction.line_items)
            unique_prices = {round(li.unit_price, 4) for li in extraction.line_items}
            extracted_unit_price = (
                extraction.line_items[0].unit_price
                if len(unique_prices) == 1
                else (
                    round(extraction.subtotal / extracted_qty, 2) if extracted_qty > 0 else 0.0
                )
            )

        invoice, match_result = submit_invoice_and_match(
            session,
            po_id=target_po.id,
            invoice_number=extraction.invoice_number,
            extracted_qty=extracted_qty,
            extracted_unit_price=extracted_unit_price,
            extracted_total=extraction.subtotal,
            pdf_path=email.attachment_path,
            comm_id=email.id,
        )

        outcome = self._handle_invoice_match_outcome(
            session,
            po=target_po,
            part=part,
            supplier=supplier,
            invoice=invoice,
            match_result=match_result,
            thread_id=email.thread_id,
            thread_subject=email.subject,
        )
        session.flush()
        return {
            "triage": triage_result,
            "matched_po_id": target_po.id,
            "alert_id": outcome["alert_id"],
            "draft_comm_id": outcome["draft_comm_id"],
            "invoice_extraction": extraction,
            "invoice_id": invoice.id,
            "match_result": match_result,
        }

    def _handle_invoice_match_outcome(
        self,
        session: Session,
        *,
        po: PurchaseOrder,
        part: Part,
        supplier: Supplier,
        invoice: Invoice,
        match_result: ThreeWayMatchResult | None,
        thread_id: str | None = None,
        thread_subject: str | None = None,
    ) -> dict[str, Any]:
        """Create alerts and Lex discrepancy drafts based on deterministic 3-way match outcome."""
        if match_result is None:
            alert = create_action_item(
                session,
                type="INVOICE_PENDING_GRN",
                severity=AlertSeverity.INFO,
                title=f"Invoice {invoice.invoice_number} held pending GRN ({po.po_number})",
                summary=(
                    f"Extracted invoice {invoice.invoice_number} (v{invoice.version}: "
                    f"{invoice.extracted_qty} units @ ₹{invoice.extracted_unit_price:,.2f}, "
                    f"subtotal ₹{invoice.extracted_total:,.2f}) for {po.po_number}. "
                    "Awaiting physical Goods Receipt before 3-way match."
                ),
                po_id=po.id,
                part_id=part.id,
                suggested_actions=[],
                cta_payload={
                    "po_id": po.id,
                    "invoice_id": invoice.id,
                    "invoice_number": invoice.invoice_number,
                },
            )
            return {"alert_id": alert.id, "draft_comm_id": None}

        if match_result.matched:
            auto_resolve_po_alerts(
                session,
                po.id,
                (
                    "INVOICE_MISMATCH_QUARANTINE",
                    "QUARANTINE_REMINDER",
                    "QUARANTINE_ESCALATED",
                    "OVERDUE_PO",
                    "INVOICE_PENDING_GRN",
                ),
            )
            grn = session.scalar(
                select(GoodsReceipt)
                .where(GoodsReceipt.po_id == po.id)
                .order_by(GoodsReceipt.id.desc())
                .limit(1)
            )
            released_qty = grn.received_qty if grn is not None else invoice.extracted_qty
            alert = create_action_item(
                session,
                type="INVOICE_MATCHED_CLOSED",
                severity=AlertSeverity.INFO,
                title=f"3-way match passed & stock released: {po.po_number} ({part.sku})",
                summary=(
                    f"Invoice {invoice.invoice_number} (v{invoice.version}) matched "
                    f"PO {po.po_number} and GRN ({released_qty} units @ ₹{po.unit_price:,.2f}). "
                    f"Released {released_qty} units to usable on-hand stock "
                    f"(now {part.on_hand_stock} on hand, {part.quarantined_stock} quarantined) "
                    "and closed PO."
                ),
                po_id=po.id,
                part_id=part.id,
                suggested_actions=[],
                cta_payload={
                    "po_id": po.id,
                    "invoice_id": invoice.id,
                    "invoice_number": invoice.invoice_number,
                    "released_qty": released_qty,
                },
            )
            return {"alert_id": alert.id, "draft_comm_id": None}

        # Match FAILED -> stock is quarantined; draft Lex correction request for manager approval
        grn = session.scalar(
            select(GoodsReceipt)
            .where(GoodsReceipt.po_id == po.id)
            .order_by(GoodsReceipt.id.desc())
            .limit(1)
        )
        grn_qty = grn.received_qty if grn is not None else po.qty
        price_diff = round(invoice.extracted_unit_price - po.unit_price, 2)
        total_price_impact = round(price_diff * invoice.extracted_qty, 2)
        qty_diff = invoice.extracted_qty - grn_qty

        draft = self._create_lex_draft(
            session,
            po=po,
            part=part,
            supplier=supplier,
            category=EmailCategory.INVOICE_DISCREPANCY,
            thread_id=thread_id,
            thread_subject=thread_subject or f"[{po.po_number}] Invoice Discrepancy",
            context_facts={
                "invoice_number": invoice.invoice_number,
                "invoice_version": invoice.version,
                "match_status": match_result.match_status.value,
                "discrepancy_summary": match_result.discrepancy_summary,
                "po_qty": po.qty,
                "po_unit_price": po.unit_price,
                "grn_received_qty": grn_qty,
                "invoice_qty": invoice.extracted_qty,
                "invoice_unit_price": invoice.extracted_unit_price,
                "invoice_subtotal": invoice.extracted_total,
                "price_diff_per_unit": price_diff,
                "total_price_impact": total_price_impact,
                "qty_diff": qty_diff,
                "quarantined_qty": grn_qty,
                "requested_action": (
                    "Please issue a revised PDF invoice matching the agreed PO quantity and "
                    "unit price so we can release quarantined stock and process payment."
                ),
            },
        )
        severity = (
            AlertSeverity.CRITICAL
            if (part.criticality == Criticality.CRITICAL.value or part.on_hand_stock <= part.rop)
            else AlertSeverity.HIGH
        )
        alert = create_action_item(
            session,
            type="INVOICE_MISMATCH_QUARANTINE",
            severity=severity,
            title=f"Stock Quarantined — Invoice Mismatch on {po.po_number} ({part.sku})",
            summary=(
                f"Invoice {invoice.invoice_number} (v{invoice.version}) from {supplier.name} "
                f"failed 3-way match ({match_result.match_status.value}): "
                f"{match_result.discrepancy_summary}. {grn_qty} units are held in quarantine "
                "(excluded from on-hand stock). Lex drafted a correction request for approval."
            ),
            po_id=po.id,
            part_id=part.id,
            draft_comm_id=draft.id,
            suggested_actions=[
                {"action": "approve_draft", "label": "Approve & Send Correction Request"},
                {"action": "reject_draft", "label": "Reject Draft"},
            ],
            cta_payload={
                "draft_comm_id": draft.id,
                "po_id": po.id,
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "match_status": match_result.match_status.value,
                "discrepancy_summary": match_result.discrepancy_summary,
                "total_price_impact": total_price_impact,
            },
        )
        return {"alert_id": alert.id, "draft_comm_id": draft.id}

    def record_grn(
        self,
        session: Session,
        *,
        po_id: int,
        received_qty: int,
        condition: str = "GOOD",
    ) -> dict[str, Any]:
        """Log a physical Goods Receipt and trigger invoice quarantine/release if pending."""
        grn, match_result = record_goods_receipt(
            session,
            po_id=po_id,
            received_qty=received_qty,
            condition=condition,
        )
        auto_resolve_po_alerts(session, po_id, ("OVERDUE_PO", "PRE_ETA_CHECKIN_DRAFT"))

        alert_id: int | None = None
        draft_comm_id: int | None = None
        if match_result is not None:
            po = session.get(PurchaseOrder, po_id)
            assert po is not None
            part = session.get(Part, po.part_id)
            supplier = session.get(Supplier, po.supplier_id)
            latest_invoice = session.scalar(
                select(Invoice)
                .where(Invoice.po_id == po_id)
                .order_by(Invoice.version.desc(), Invoice.id.desc())
                .limit(1)
            )
            if part is not None and supplier is not None and latest_invoice is not None:
                outcome = self._handle_invoice_match_outcome(
                    session,
                    po=po,
                    part=part,
                    supplier=supplier,
                    invoice=latest_invoice,
                    match_result=match_result,
                )
                alert_id = outcome["alert_id"]
                draft_comm_id = outcome["draft_comm_id"]

        session.flush()
        return {
            "grn": grn,
            "match_result": match_result,
            "alert_id": alert_id,
            "draft_comm_id": draft_comm_id,
        }

    def recalculate_planning(self, session: Session) -> dict[str, Any]:
        """Run deterministic Planning Intelligence and sync ROP_RECOMMENDATION alerts."""
        summary = recalculate_all(session)
        alerts_created = 0
        alerts_updated = 0
        alerts_resolved = 0

        for rec in summary.recommendations:
            existing_alerts = list(
                session.scalars(
                    select(ActionItem).where(
                        ActionItem.type == "ROP_RECOMMENDATION",
                        ActionItem.part_id == rec.part_id,
                        ActionItem.status == ActionItemStatus.OPEN.value,
                    )
                ).all()
            )
            if not rec.needs_update:
                for item in existing_alerts:
                    resolve_action_item(session, item.id)
                    alerts_resolved += 1
                continue

            severity = (
                AlertSeverity.HIGH
                if (
                    rec.criticality == Criticality.CRITICAL.value
                    and rec.recommended_rop > rec.current_rop
                )
                else AlertSeverity.MEDIUM
            )
            title = (
                f"Update ROP/S for {rec.sku}: "
                f"ROP {rec.current_rop} → {rec.recommended_rop}, "
                f"S {rec.current_replenishment_point} → {rec.recommended_replenishment_point}"
            )
            suggested = [
                {
                    "action": "approve_rop",
                    "label": (
                        f"Approve ROP {rec.recommended_rop} / "
                        f"S {rec.recommended_replenishment_point}"
                    ),
                }
            ]
            cta_payload = {
                "part_id": rec.part_id,
                "rop": rec.recommended_rop,
                "replenishment_point": rec.recommended_replenishment_point,
                "current_rop": rec.current_rop,
                "current_replenishment_point": rec.current_replenishment_point,
                "effective_lead_time_days": rec.effective_lead_time_days,
                "quoted_lead_time_days": rec.quoted_lead_time_days,
                "safety_stock": rec.safety_stock,
            }

            if existing_alerts:
                primary = existing_alerts[0]
                primary.severity = severity.value
                primary.title = title
                primary.summary = rec.reason
                primary.suggested_actions_json = json.dumps(suggested)
                primary.cta_payload_json = json.dumps(cta_payload)
                alerts_updated += 1
                for dup in existing_alerts[1:]:
                    resolve_action_item(session, dup.id)
                    alerts_resolved += 1
            else:
                create_action_item(
                    session,
                    type="ROP_RECOMMENDATION",
                    severity=severity,
                    title=title,
                    summary=rec.reason,
                    part_id=rec.part_id,
                    suggested_actions=suggested,
                    cta_payload=cta_payload,
                )
                alerts_created += 1

        session.flush()
        return {
            "summary": summary,
            "alerts_created": alerts_created,
            "alerts_updated": alerts_updated,
            "alerts_resolved": alerts_resolved,
        }

    def apply_rop_recommendation(
        self,
        session: Session,
        *,
        part_id: int,
        rop: int | None = None,
        replenishment_point: int | None = None,
    ) -> dict[str, Any]:
        """Apply recommended (or explicitly supplied) ROP & S values and resolve open ROP alerts."""
        rec = evaluate_part_recommendation(session, part_id)
        target_rop = rec.recommended_rop if rop is None else int(rop)
        target_s = (
            rec.recommended_replenishment_point
            if replenishment_point is None
            else int(replenishment_point)
        )
        part = update_rop_and_replenishment(
            session, part_id, rop=target_rop, replenishment_point=target_s
        )

        resolved_count = 0
        open_rop_alerts = list(
            session.scalars(
                select(ActionItem).where(
                    ActionItem.type == "ROP_RECOMMENDATION",
                    ActionItem.part_id == part_id,
                    ActionItem.status == ActionItemStatus.OPEN.value,
                )
            ).all()
        )
        for item in open_rop_alerts:
            resolve_action_item(session, item.id)
            resolved_count += 1

        session.flush()
        return {
            "part_id": part.part_id,
            "sku": part.sku,
            "rop": part.rop,
            "replenishment_point": part.replenishment_point,
            "resolved_alerts": resolved_count,
        }

    def approve_lex_draft(
        self,
        session: Session,
        comm_id: int,
        *,
        edited_subject: str | None = None,
        edited_body: str | None = None,
    ) -> dict[str, Any]:
        sent_email, outcome = approve_draft(
            session,
            comm_id,
            edited_subject=edited_subject,
            edited_body=edited_body,
        )
        policy = record_approval_outcome(
            session,
            agent_name=self.lex.agent_name,
            action_category=sent_email.category,
            outcome=outcome,
        )

        po_transitioned_to: str | None = None
        if sent_email.po_id is not None and sent_email.category in (
            EmailCategory.PO_DISPATCH.value,
            EmailCategory.REVISED_PO.value,
        ):
            po = session.get(PurchaseOrder, sent_email.po_id)
            if po is not None and po.status == POStatus.PENDING_DISPATCH_APPROVAL.value:
                part = session.get(Part, po.part_id)
                is_critical = part is not None and part.criticality == Criticality.CRITICAL.value
                transition_po(
                    session,
                    po,
                    POStatus.SENT_AWAITING_ACK,
                    note=f"PO email #{sent_email.id} approved ({outcome.value}) and dispatched.",
                )
                po.follow_up_count = 0
                po.next_sla_check_at = clock.now(session) + timedelta(
                    hours=24 if is_critical else 48
                )
                po_transitioned_to = po.status

        open_items = session.scalars(
            select(ActionItem).where(
                ActionItem.draft_comm_id == comm_id,
                ActionItem.status == ActionItemStatus.OPEN.value,
            )
        ).all()
        for item in open_items:
            resolve_action_item(session, item.id)

        session.flush()
        return {
            "email": sent_email,
            "approval_outcome": outcome.value,
            "policy_counts": {
                "approved_as_is": policy.approved_as_is_count,
                "approved_edited": policy.approved_edited_count,
                "rejected": policy.rejected_count,
            },
            "po_transitioned_to": po_transitioned_to,
        }

    def reject_lex_draft(self, session: Session, comm_id: int) -> dict[str, Any]:
        rejected = reject_draft(session, comm_id)
        policy = record_approval_outcome(
            session,
            agent_name=self.lex.agent_name,
            action_category=rejected.category,
            outcome=ApprovalOutcome.REJECTED,
        )
        open_items = session.scalars(
            select(ActionItem).where(
                ActionItem.draft_comm_id == comm_id,
                ActionItem.status == ActionItemStatus.OPEN.value,
            )
        ).all()
        for item in open_items:
            resolve_action_item(session, item.id)

        session.flush()
        return {
            "email": rejected,
            "approval_outcome": ApprovalOutcome.REJECTED.value,
            "policy_counts": {
                "approved_as_is": policy.approved_as_is_count,
                "approved_edited": policy.approved_edited_count,
                "rejected": policy.rejected_count,
            },
        }

    def advance_clock_and_watch(
        self,
        session: Session,
        *,
        hours: int = 0,
        days: int = 0,
    ) -> dict[str, Any]:
        new_now = clock.advance(session, hours=hours, days=days)
        sla_events: list[SLAEvent] = run_sla_watcher(session, self.lex)
        return {
            "sim_now": new_now,
            "sla_events": sla_events,
        }

    def jump_to_po_eta_and_watch(
        self,
        session: Session,
        po_id: int,
        *,
        offset_days: int = 0,
    ) -> dict[str, Any]:
        po = session.get(PurchaseOrder, po_id)
        if po is None:
            raise ValueError(f"PurchaseOrder {po_id} not found")
        target = po.revised_eta or po.promised_eta
        if target is None:
            raise ValueError(f"PurchaseOrder {po.po_number} does not have an ETA yet")
        target_utc = clock.ensure_utc(target) + timedelta(days=offset_days)
        current = clock.now(session)
        if target_utc < current:
            raise ValueError("Cannot jump simulated clock backwards in time")
        new_now = clock.set_time(session, target_utc)
        sla_events = run_sla_watcher(session, self.lex)
        return {
            "sim_now": new_now,
            "sla_events": sla_events,
        }

    def execute_alert_cta(
        self,
        session: Session,
        action_item_id: int,
        *,
        action: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = params or {}
        item = session.get(ActionItem, action_item_id)
        if item is None:
            raise ValueError(f"ActionItem {action_item_id} not found")

        if action == "approve_draft":
            draft_id = int(params.get("draft_comm_id") or item.draft_comm_id or 0)
            if not draft_id:
                raise ValueError("No draft_comm_id linked to this alert")
            return self.approve_lex_draft(
                session,
                draft_id,
                edited_subject=params.get("edited_subject"),
                edited_body=params.get("edited_body"),
            )

        if action == "reject_draft":
            draft_id = int(params.get("draft_comm_id") or item.draft_comm_id or 0)
            if not draft_id:
                raise ValueError("No draft_comm_id linked to this alert")
            return self.reject_lex_draft(session, draft_id)

        if action == "update_po":
            po_id = int(params.get("po_id") or item.po_id or 0)
            if not po_id:
                raise ValueError("No po_id linked to this alert")
            revised_eta = _parse_iso_date_utc(params.get("revised_eta"))
            po = update_po_terms(
                session,
                po_id,
                qty=params.get("qty"),
                unit_price=params.get("unit_price"),
                revised_eta=revised_eta,
                note=params.get("note"),
            )
            draft = None
            if po.status == POStatus.EXCEPTION_REVIEW.value:
                transition_po(
                    session,
                    po,
                    POStatus.PENDING_DISPATCH_APPROVAL,
                    note="Manager accepted revised PO terms; awaiting dispatch approval.",
                )
                part = session.get(Part, po.part_id)
                supplier = session.get(Supplier, po.supplier_id)
                assert part is not None and supplier is not None
                eta_iso = (
                    clock.ensure_utc(po.revised_eta).date().isoformat()
                    if po.revised_eta
                    else None
                )
                draft = self._create_lex_draft(
                    session,
                    po=po,
                    part=part,
                    supplier=supplier,
                    category=EmailCategory.REVISED_PO,
                    context_facts={
                        "revised_qty": po.qty,
                        "revised_unit_price": po.unit_price,
                        "revised_eta": eta_iso,
                    },
                )
                create_action_item(
                    session,
                    type="PO_DISPATCH_APPROVAL",
                    severity=AlertSeverity.MEDIUM,
                    title=f"Approve revised PO dispatch: {po.po_number}",
                    summary=(
                        f"Revised {po.po_number} ({po.qty} units @ ₹{po.unit_price:,.2f}). "
                        "Approve Lex's revised PO email."
                    ),
                    po_id=po.id,
                    part_id=po.part_id,
                    draft_comm_id=draft.id,
                    suggested_actions=[
                        {"action": "approve_draft", "label": "Approve & Send Revised PO"},
                        {"action": "reject_draft", "label": "Reject Draft"},
                    ],
                    cta_payload={"draft_comm_id": draft.id, "po_id": po.id},
                )
            resolve_action_item(session, item.id)
            return {
                "po_id": po.id,
                "po_status": po.status,
                "draft_comm_id": draft.id if draft else None,
            }

        if action == "switch_backup_supplier":
            po_id = int(params.get("po_id") or item.po_id or 0)
            backup_supplier_id = (
                params.get("backup_supplier_id")
                or (item.cta_payload_json or {}).get("backup_supplier_id")
            )
            if not po_id:
                raise ValueError("po_id is required")
            _orig, new_po = switch_po_to_backup_supplier(
                session,
                po_id,
                backup_supplier_id=int(backup_supplier_id) if backup_supplier_id else None,
            )
            part = session.get(Part, new_po.part_id)
            supplier = session.get(Supplier, new_po.supplier_id)
            assert part is not None and supplier is not None
            quoted_lt = _quoted_lead_time_days(session, part.id, supplier.id)
            draft = self._create_lex_draft(
                session,
                po=new_po,
                part=part,
                supplier=supplier,
                category=EmailCategory.PO_DISPATCH,
                context_facts={
                    "order_qty": new_po.qty,
                    "unit_price": new_po.unit_price,
                    "quoted_lead_time_days": quoted_lt,
                    "backup_failover": True,
                },
            )
            create_action_item(
                session,
                type="PO_DISPATCH_APPROVAL",
                severity=AlertSeverity.HIGH,
                title=f"Approve backup PO dispatch: {new_po.po_number} ({supplier.name})",
                summary=(
                    f"Switched order to backup supplier {supplier.name} under {new_po.po_number} "
                    f"({new_po.qty} units @ ₹{new_po.unit_price:,.2f}). Approve dispatch email."
                ),
                po_id=new_po.id,
                part_id=new_po.part_id,
                draft_comm_id=draft.id,
                suggested_actions=[
                    {"action": "approve_draft", "label": "Approve & Send Backup PO"},
                    {"action": "reject_draft", "label": "Reject Draft"},
                ],
                cta_payload={"draft_comm_id": draft.id, "po_id": new_po.id},
            )
            resolve_action_item(session, item.id)
            return {
                "cancelled_po_id": po_id,
                "new_po_id": new_po.id,
                "new_po_number": new_po.po_number,
                "draft_comm_id": draft.id,
            }

        if action == "approve_rop":
            raw_cta = item.cta_payload_json
            cta = json.loads(raw_cta) if isinstance(raw_cta, str) and raw_cta else (raw_cta or {})
            part_id = int(params.get("part_id") or cta.get("part_id") or item.part_id or 0)
            rop_val = params.get("rop", cta.get("rop"))
            s_val = params.get("replenishment_point", cta.get("replenishment_point"))
            if not part_id or rop_val is None or s_val is None:
                raise ValueError("part_id, rop, and replenishment_point are required")
            part = update_rop_and_replenishment(
                session, part_id, rop=int(rop_val), replenishment_point=int(s_val)
            )
            resolve_action_item(session, item.id)
            return {
                "part_id": part.part_id,
                "rop": part.rop,
                "replenishment_point": part.replenishment_point,
            }

        if action == "add_note":
            po_id = params.get("po_id") or item.po_id
            note_text = str(params.get("note") or "").strip()
            if po_id and note_text:
                po = session.get(PurchaseOrder, int(po_id))
                if po is not None:
                    append_po_note(session, po, note_text)
            if params.get("resolve", True):
                resolve_action_item(session, item.id)
            return {"po_id": po_id, "note_added": bool(note_text)}

        raise ValueError(f"Unsupported alert CTA action: '{action}'")
