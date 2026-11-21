from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.agents.lex import (
    EmailDraftInput,
    EmailIntent,
    EmailTriageInput,
    EmailTriageResult,
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
    GRNStatus,
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
from backend.inventory import (
    ConsumeResult,
    clear_quarantined_stock,
    consume_stock,
    update_rop_and_replenishment,
)
from backend.mailroom import (
    OutboundMessage,
    StoredEmail,
    annotate_triage,
    approve_draft,
    get_thread,
    inspect_pdf_readability,
    reject_draft,
    save_draft,
    send_email,
)
from backend.mailroom.transport import SUPPLIER_PERSONAS
from backend.observability import record_approval_outcome
from backend.orchestrator.escalation_workflow import escalate_po_to_procurement_workflow
from backend.orchestrator.invoice_workflow import (
    DAMAGED_GOODS_CTA_ACTIONS,
    NET_SETTLEMENT_CTA_ACTIONS,
    _draft_supplier_general_email,
    execute_damaged_goods_cta,
    execute_net_settlement_cta,
    handle_damaged_grn_created,
    handle_invoice_match_outcome,
    process_pdf_invoice_email,
)
from backend.orchestrator.logistics_workflow import (
    create_supplier_memory_alert,
    handle_followup_logistics_update,
    handle_po_acknowledgment_logistics,
    has_logistics_payload,
    parse_iso_date_utc,
)
from backend.orchestrator.sla_watcher import SLAEvent, auto_resolve_po_alerts, run_sla_watcher
from backend.orchestrator.stock_request_workflow import (
    STOCK_REQUEST_CTA_ACTIONS,
    execute_stock_request_cta,
    mark_stock_requests_awaiting_min_qty,
    process_inbound_making_email,
)
from backend.planning import (
    compute_demand_forecast,
    evaluate_part_recommendation,
    recalculate_all,
)
from backend.po import (
    ThreeWayMatchResult,
    append_po_note,
    cancel_po,
    compute_urgency_score,
    create_action_item,
    create_po,
    get_po,
    is_grn_damaged,
    record_goods_receipt,
    resolve_action_item,
    switch_po_to_backup_supplier,
    transition_po,
    update_po_terms,
)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}

EXCEPTION_INTENTS = {
    EmailIntent.PRICE_CHANGE,
    EmailIntent.LEAD_TIME_CHANGE,
    EmailIntent.PARTIAL_AVAILABILITY,
    EmailIntent.OUT_OF_STOCK,
}


def _parse_iso_date_utc(date_str: str | None) -> datetime | None:
    return parse_iso_date_utc(date_str)


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


def _term_currency(session: Session, part_id: int, supplier_id: int) -> str:
    term = session.scalar(
        select(PartSupplierTerm).where(
            PartSupplierTerm.part_id == part_id,
            PartSupplierTerm.supplier_id == supplier_id,
        )
    )
    return term.currency if term is not None and term.currency else "INR"


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


def _save_learned_memory_note(
    session: Session, supplier_id: int, note_text: str | None
) -> SupplierMemoryNote | None:
    if not note_text or not note_text.strip():
        return None
    cleaned = note_text.strip()
    existing = _supplier_memory_texts(session, supplier_id)
    if any(n.strip().lower() == cleaned.lower() for n in existing):
        return None
    sim_now = clock.now(session)
    note_row = SupplierMemoryNote(
        supplier_id=supplier_id,
        note=cleaned,
        source=SupplierMemorySource.LEX_LEARNED.value,
        created_at=sim_now,
        updated_at=sim_now,
    )
    session.add(note_row)
    session.flush()
    return note_row


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
        required_cc_personas: list[str] | None = None,
    ) -> StoredEmail:
        sim_now = clock.now(session)
        currency = _term_currency(session, part.id, supplier.id)
        req_cc = list(required_cc_personas or [])
        if PersonaKey.INVENTORY_MANAGER.value not in req_cc:
            req_cc.append(PersonaKey.INVENTORY_MANAGER.value)
        if (
            category == EmailCategory.EXPEDITE_REQUEST
            and PersonaKey.PROCUREMENT.value not in req_cc
        ):
            req_cc.append(PersonaKey.PROCUREMENT.value)

        draft_res = self.lex.draft_email(
            session,
            EmailDraftInput(
                category=category,
                po_number=po.po_number,
                part_sku=part.sku,
                part_name=part.name,
                qty=po.qty,
                unit_price=po.unit_price,
                currency=currency,
                recipient_persona=supplier.persona_key,
                recipient_name=supplier.name,
                current_date=sim_now.date().isoformat(),
                thread_subject=thread_subject,
                supplier_memory_notes=_supplier_memory_texts(session, supplier.id),
                required_cc_personas=req_cc,
                context_facts={"currency": currency, **context_facts},
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
                part_criticality=part.criticality,
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
            part_obj = session.get(Part, burn.part_id)
            alert = create_action_item(
                session,
                type="ACCELERATED_BURN",
                severity=AlertSeverity.CRITICAL,
                part_criticality=part_obj.criticality if part_obj else None,
                days_to_stockout=0.0,
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
        elif stored.from_persona == PersonaKey.MAKING.value and PersonaKey.LEX.value in recipients:
            triage_outcome = process_inbound_making_email(session, self, stored)

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
            currency = _term_currency(session, part.id, supplier.id)
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
                    currency=currency,
                    status=po.status,
                    quoted_lead_time_days=quoted_lt,
                    promised_eta=promised,
                    revised_eta=revised,
                    promised_dispatch_date=(
                        clock.ensure_utc(po.promised_dispatch_date).date().isoformat()
                        if po.promised_dispatch_date
                        else None
                    ),
                    actual_dispatch_date=(
                        clock.ensure_utc(po.actual_dispatch_date).date().isoformat()
                        if po.actual_dispatch_date
                        else None
                    ),
                    carrier_name=po.carrier_name,
                    tracking_number=po.tracking_number,
                    dispatch_details_status=po.dispatch_details_status,
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

        all_paths = list(email.attachment_paths or [])
        if email.attachment_path and email.attachment_path not in all_paths:
            all_paths.insert(0, email.attachment_path)
        pdf_paths = [p for p in all_paths if p.lower().endswith(".pdf")]
        image_paths = [p for p in all_paths if Path(p).suffix.lower() in IMAGE_SUFFIXES]

        # Inspect PDF readability before passing to Gemini triage (F-1.2 & F-3.2)
        triage_pdf_bytes: bytes | None = None
        unreadable_pdf_detected = False
        for raw_pdf in pdf_paths:
            pdf_file = Path(raw_pdf)
            if not pdf_file.is_file():
                unreadable_pdf_detected = True
                break
            raw_bytes = pdf_file.read_bytes()
            if inspect_pdf_readability(raw_bytes) is not None:
                unreadable_pdf_detected = True
                break
            if triage_pdf_bytes is None and len(raw_bytes) <= 10 * 1024 * 1024:
                triage_pdf_bytes = raw_bytes

        triage_input = EmailTriageInput(
            sender_persona=email.from_persona,
            sender_name=supplier.name,
            subject=email.subject,
            body=email.body,
            has_pdf_attachment=bool(pdf_paths),
            has_image_attachment=bool(image_paths),
            attachment_filenames=[Path(p).name for p in all_paths],
            current_date=sim_now.date().isoformat(),
            open_pos=open_po_contexts,
            thread_history=thread_history,
            supplier_memory_notes=_supplier_memory_texts(session, supplier.id),
        )
        try:
            triage_result: EmailTriageResult = self.lex.triage_email(
                session,
                triage_input,
                linked_comm_id=email.id,
                pdf_bytes=triage_pdf_bytes,
            )
        except AssertionError:
            raise
        except Exception:
            if triage_pdf_bytes is None:
                raise
            # Corrupt PDF failed multimodal triage -> escalate via invoice/PDF review safely
            unreadable_pdf_detected = True
            triage_result = self.lex.triage_email(
                session,
                triage_input,
                linked_comm_id=email.id,
                pdf_bytes=None,
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

        learned_note = _save_learned_memory_note(
            session, supplier.id, triage_result.new_memory_note
        )
        if learned_note is not None:
            create_supplier_memory_alert(
                session,
                supplier=supplier,
                note=learned_note,
                po=matched_po,
                comm_id=email.id,
            )

        # If an attached PDF is locked/corrupt OR email is an invoice submission -> PDF pipeline
        should_run_invoice_pipeline = (
            unreadable_pdf_detected
            or triage_result.intent == EmailIntent.INVOICE_SUBMISSION
            or (
                bool(pdf_paths)
                and triage_result.intent == EmailIntent.AMBIGUOUS
                and matched_po is None
            )
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

        # Route primary PO triage + any additional PO updates (F-2.3 multi-PO email splitting)
        triage_items: list[EmailTriageResult] = [triage_result]
        for add in triage_result.additional_po_updates:
            triage_items.append(
                EmailTriageResult(
                    matched_po_number=add.matched_po_number,
                    intent=add.intent,
                    confidence=add.confidence,
                    proposed_unit_price=add.proposed_unit_price,
                    proposed_currency=add.proposed_currency,
                    proposed_qty=add.proposed_qty,
                    proposed_eta_date=add.proposed_eta_date,
                    promised_dispatch_date=add.promised_dispatch_date,
                    actual_dispatch_date=add.actual_dispatch_date,
                    carrier_name=add.carrier_name,
                    tracking_number=add.tracking_number,
                    details_promised_later=add.details_promised_later,
                    details_promised_by_date=add.details_promised_by_date,
                    delay_reason=add.delay_reason,
                    summary_for_manager=(
                        add.summary_for_manager or triage_result.summary_for_manager
                    ),
                    suggested_action=add.suggested_action or triage_result.suggested_action,
                    reasoning=triage_result.reasoning,
                )
            )

        outcomes: list[dict[str, Any]] = []
        for item_triage in triage_items:
            item_po = (
                po_by_number.get(item_triage.matched_po_number)
                if item_triage.matched_po_number
                else None
            )
            outcomes.append(
                self._route_single_po_triage(
                    session,
                    email=email,
                    supplier=supplier,
                    triage_result=item_triage,
                    matched_po=item_po,
                    image_paths=image_paths,
                    sim_now=sim_now,
                )
            )

        primary = outcomes[0]
        session.flush()
        response: dict[str, Any] = {
            "triage": triage_result,
            "matched_po_id": primary["matched_po_id"],
            "alert_id": primary["alert_id"],
            "draft_comm_id": primary["draft_comm_id"],
        }
        if len(outcomes) > 1:
            response["additional_outcomes"] = outcomes[1:]
            response["alert_ids"] = [o["alert_id"] for o in outcomes if o["alert_id"]]
            response["draft_comm_ids"] = [
                o["draft_comm_id"] for o in outcomes if o["draft_comm_id"]
            ]
        return response

    def _route_single_po_triage(
        self,
        session: Session,
        *,
        email: StoredEmail,
        supplier: Supplier,
        triage_result: EmailTriageResult,
        matched_po: PurchaseOrder | None,
        image_paths: list[str],
        sim_now: datetime,
    ) -> dict[str, Any]:
        created_alert: ActionItem | None = None
        created_draft: StoredEmail | None = None
        image_names = [Path(p).name for p in image_paths]
        image_clause = (
            f" Evidentiary image attachment(s): {', '.join(image_names)}."
            if image_names
            else ""
        )

        # Route intent with deterministic guardrails
        if triage_result.intent == EmailIntent.AMBIGUOUS or matched_po is None:
            if triage_result.intent == EmailIntent.GENERAL_REPLY and matched_po is None:
                created_alert = create_action_item(
                    session,
                    type="SUPPLIER_ATTACHMENT_REVIEW" if image_names else "SUPPLIER_MESSAGE",
                    severity=AlertSeverity.HIGH if image_names else AlertSeverity.INFO,
                    title=(
                        f"Damage / photo attachment from {supplier.name}: {email.subject}"
                        if image_names
                        else f"Message from {supplier.name}: {email.subject}"
                    ),
                    summary=f"{triage_result.summary_for_manager}{image_clause}",
                    suggested_actions=(
                        [{"action": "add_note", "label": "Log Inspection Note"}]
                        if image_names
                        else []
                    ),
                    cta_payload={"comm_id": email.id, "image_attachments": image_paths},
                )
            else:
                # F-4.1: Generate optional clarification draft on AMBIGUOUS escalation
                part_for_draft = (
                    session.get(Part, matched_po.part_id) if matched_po is not None else None
                )
                try:
                    created_draft = _draft_supplier_general_email(
                        session,
                        self,
                        supplier=supplier,
                        po=matched_po,
                        part=part_for_draft,
                        thread_id=email.thread_id,
                        thread_subject=email.subject,
                        context_facts={
                            "purpose": "clarification",
                            "ambiguity_summary": triage_result.summary_for_manager,
                            "reasoning": triage_result.reasoning,
                            "requested_action": (
                                "Please confirm the exact PO number, firm unit price (in INR), "
                                "quantity, and delivery ETA so we can proceed."
                            ),
                        },
                    )
                except AssertionError:
                    raise
                except Exception:
                    created_draft = None

                suggested = []
                if created_draft is not None:
                    suggested.extend(
                        [
                            {"action": "approve_draft", "label": "Approve & Send Clarification"},
                            {"action": "reject_draft", "label": "Reject Draft"},
                        ]
                    )
                suggested.append({"action": "add_note", "label": "Log Review Note"})
                created_alert = create_action_item(
                    session,
                    type="AMBIGUOUS_EMAIL",
                    severity=AlertSeverity.HIGH,
                    title=f"Manual review needed: email from {supplier.name}",
                    summary=(
                        f"{triage_result.summary_for_manager}{image_clause} "
                        f"(Confidence: {triage_result.confidence:.0%}. No PO state was changed.)"
                    ),
                    po_id=matched_po.id if matched_po else None,
                    part_id=matched_po.part_id if matched_po else None,
                    draft_comm_id=created_draft.id if created_draft else None,
                    suggested_actions=suggested,
                    cta_payload={
                        "comm_id": email.id,
                        "reasoning": triage_result.reasoning,
                        "draft_comm_id": created_draft.id if created_draft else None,
                        "image_attachments": image_paths,
                    },
                )
            return {
                "matched_po_id": matched_po.id if matched_po else None,
                "alert_id": created_alert.id if created_alert else None,
                "draft_comm_id": created_draft.id if created_draft else None,
            }

        part = session.get(Part, matched_po.part_id)
        assert part is not None
        po_currency = _term_currency(session, part.id, supplier.id)
        proposed_curr = (triage_result.proposed_currency or "").strip().upper()
        currency_mismatch = bool(proposed_curr and proposed_curr != po_currency)

        # F-2.1: Foreign-currency mismatch guardrail (blocks blind INR price overwrite)
        if currency_mismatch:
            if matched_po.status in (
                POStatus.SENT_AWAITING_ACK.value,
                POStatus.UNRESPONSIVE_ESCALATED.value,
                POStatus.ACKNOWLEDGED.value,
                POStatus.DELAYED_CRITICAL.value,
            ):
                transition_po(
                    session,
                    matched_po,
                    POStatus.EXCEPTION_REVIEW,
                    note=(
                        f"Foreign currency quote ({proposed_curr} vs {po_currency}) raised: "
                        f"{triage_result.summary_for_manager}"
                    ),
                )
            backup = _backup_supplier_info(session, part.id, supplier.id)
            suggested = [
                {"action": "request_inr_quote", "label": f"Request {po_currency} Quote"},
            ]
            if backup.get("backup_supplier_id"):
                suggested.append(
                    {
                        "action": "switch_backup_supplier",
                        "label": f"Switch to {backup['backup_supplier_name']}",
                    }
                )
            suggested.append({"action": "cancel_po", "label": "Cancel PO"})
            suggested.append({"action": "add_note", "label": "Add Note"})

            quoted_price_str = (
                f"{triage_result.proposed_unit_price:,.2f} {proposed_curr}"
                if triage_result.proposed_unit_price is not None
                else proposed_curr
            )
            created_alert = create_action_item(
                session,
                type="CURRENCY_MISMATCH",
                severity=AlertSeverity.HIGH,
                part_criticality=part.criticality,
                title=(
                    f"Currency mismatch ({proposed_curr} vs {po_currency}): "
                    f"{matched_po.po_number} ({part.sku})"
                ),
                summary=(
                    f"Supplier quoted {quoted_price_str} vs PO currency {po_currency} "
                    f"(₹{matched_po.unit_price:,.2f}/unit) — FX conversion or {po_currency} "
                    "re-quote required before updating PO price. "
                    f"{triage_result.summary_for_manager}{image_clause}"
                ),
                po_id=matched_po.id,
                part_id=part.id,
                suggested_actions=suggested,
                cta_payload={
                    "po_id": matched_po.id,
                    "comm_id": email.id,
                    "currency_mismatch": True,
                    "po_currency": po_currency,
                    "proposed_currency": proposed_curr,
                    "proposed_unit_price": triage_result.proposed_unit_price,
                    "proposed_qty": triage_result.proposed_qty,
                    "proposed_eta_date": triage_result.proposed_eta_date,
                    "image_attachments": image_paths,
                    **backup,
                },
            )
            return {
                "matched_po_id": matched_po.id,
                "alert_id": created_alert.id,
                "draft_comm_id": None,
            }

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
                ack_res = handle_po_acknowledgment_logistics(
                    session,
                    self,
                    po=matched_po,
                    part=part,
                    supplier=supplier,
                    email=email,
                    triage_result=triage_result,
                )
                created_alert = ack_res["alert"]
                created_draft = ack_res["draft"]
            else:
                follow_res = handle_followup_logistics_update(
                    session,
                    po=matched_po,
                    part=part,
                    supplier=supplier,
                    email=email,
                    triage_result=triage_result,
                    create_plain_message_fallback=True,
                )
                created_alert = follow_res["alert"]

        elif triage_result.intent in EXCEPTION_INTENTS:
            # F-2.2: Permit post-ack reversals (ACKNOWLEDGED / DELAYED_CRITICAL -> EXCEPTION_REVIEW)
            if matched_po.status in (
                POStatus.SENT_AWAITING_ACK.value,
                POStatus.UNRESPONSIVE_ESCALATED.value,
                POStatus.ACKNOWLEDGED.value,
                POStatus.DELAYED_CRITICAL.value,
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

            # Deterministic price variance & stockout risk calculations (PRD.md §4 Module 2 Scen 2)
            forecast = compute_demand_forecast(session, part, sim_now=sim_now)
            avg_daily_demand = forecast.effective_daily_demand
            projected_days_of_supply = (
                round(part.on_hand_stock / avg_daily_demand, 2)
                if avg_daily_demand > 0
                else 999.0
            )
            projected_stockout_date = (
                (sim_now + timedelta(days=projected_days_of_supply)).date().isoformat()
                if avg_daily_demand > 0
                else None
            )

            proposed_eta_dt = (
                _parse_iso_date_utc(triage_result.proposed_eta_date)
                or matched_po.revised_eta
                or matched_po.promised_eta
            )
            if proposed_eta_dt is not None:
                raw_days_to_eta = (
                    clock.ensure_utc(proposed_eta_dt) - sim_now
                ).total_seconds() / 86400.0
                days_to_proposed_eta: float | None = max(0.0, round(raw_days_to_eta, 2))
                days_short_of_eta = round(
                    max(0.0, days_to_proposed_eta - projected_days_of_supply), 2
                )
            else:
                days_to_proposed_eta = None
                days_short_of_eta = 0.0

            # F-4.2: Accurate unit-price & combined price+qty total spend delta
            old_price = matched_po.unit_price
            old_qty = matched_po.qty
            new_price = (
                triage_result.proposed_unit_price
                if triage_result.proposed_unit_price is not None
                else old_price
            )
            new_qty = (
                triage_result.proposed_qty
                if triage_result.proposed_qty is not None
                else old_qty
            )
            price_variance_inr = round(new_price - old_price, 2)
            price_variance_pct = (
                round((price_variance_inr / old_price) * 100.0, 2) if old_price > 0 else 0.0
            )
            price_only_impact_inr = round((new_price - old_price) * new_qty, 2)
            total_cost_impact_inr = round((new_price * new_qty) - (old_price * old_qty), 2)

            impact_notes: list[str] = []
            if triage_result.proposed_unit_price is not None:
                sign = "+" if price_variance_inr >= 0 else ""
                tot_sign = "+" if total_cost_impact_inr >= 0 else ""
                if triage_result.proposed_qty is not None and new_qty != old_qty:
                    po_sign = "+" if price_only_impact_inr >= 0 else ""
                    impact_notes.append(
                        f"Price: ₹{old_price:,.2f} → ₹{new_price:,.2f} "
                        f"({sign}₹{price_variance_inr:,.2f} / {sign}{price_variance_pct:.1f}%); "
                        f"Qty: {old_qty} → {new_qty} "
                        f"(price-only {po_sign}₹{price_only_impact_inr:,.2f}, "
                        f"total PO spend impact {tot_sign}₹{total_cost_impact_inr:,.2f})."
                    )
                else:
                    impact_notes.append(
                        f"Price: ₹{old_price:,.2f} → ₹{new_price:,.2f} "
                        f"({sign}₹{price_variance_inr:,.2f} / {sign}{price_variance_pct:.1f}%, "
                        f"total impact {tot_sign}₹{total_cost_impact_inr:,.2f})."
                    )
            elif triage_result.proposed_qty is not None and new_qty != old_qty:
                tot_sign = "+" if total_cost_impact_inr >= 0 else ""
                impact_notes.append(
                    f"Qty: {old_qty} → {new_qty} "
                    f"(total PO spend impact {tot_sign}₹{total_cost_impact_inr:,.2f})."
                )

            if projected_stockout_date is not None:
                if days_short_of_eta > 0:
                    impact_notes.append(
                        f"Stockout projected {projected_stockout_date} "
                        f"({projected_days_of_supply:.1f}d supply vs "
                        f"{days_to_proposed_eta:.1f}d to ETA "
                        f"— {days_short_of_eta:.1f}d short)."
                    )
                else:
                    impact_notes.append(
                        f"Projected stockout date: {projected_stockout_date} "
                        f"({projected_days_of_supply:.1f}d of supply on hand)."
                    )

            full_summary = " ".join(
                [triage_result.summary_for_manager + image_clause, *impact_notes]
            ).strip()

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
            suggested.append({"action": "cancel_po", "label": "Cancel PO"})
            suggested.append({"action": "add_note", "label": "Add Note"})

            severity = (
                AlertSeverity.CRITICAL
                if (
                    part.criticality == Criticality.CRITICAL.value
                    or triage_result.intent == EmailIntent.OUT_OF_STOCK
                    or days_short_of_eta > 0
                )
                else AlertSeverity.HIGH
            )
            intent_label = triage_result.intent.value.replace("_", " ").title()
            created_alert = create_action_item(
                session,
                type="PO_EXCEPTION",
                severity=severity,
                part_criticality=part.criticality,
                days_to_stockout=projected_days_of_supply,
                title=f"{intent_label}: {matched_po.po_number} ({part.sku})",
                summary=full_summary,
                po_id=matched_po.id,
                part_id=part.id,
                suggested_actions=suggested,
                cta_payload={
                    "po_id": matched_po.id,
                    "intent": triage_result.intent.value,
                    "proposed_unit_price": triage_result.proposed_unit_price,
                    "proposed_qty": triage_result.proposed_qty,
                    "proposed_eta_date": triage_result.proposed_eta_date,
                    "price_variance_inr": price_variance_inr,
                    "price_variance_pct": price_variance_pct,
                    "price_only_impact_inr": price_only_impact_inr,
                    "total_cost_impact_inr": total_cost_impact_inr,
                    "avg_daily_demand": avg_daily_demand,
                    "projected_days_of_supply": projected_days_of_supply,
                    "projected_stockout_date": projected_stockout_date,
                    "days_to_proposed_eta": days_to_proposed_eta,
                    "days_short_of_eta": days_short_of_eta,
                    "image_attachments": image_paths,
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

            # Deterministic Stockout Risk calculation (PRD.md §4 Module 2 Scen 1A)
            forecast = compute_demand_forecast(session, part, sim_now=sim_now)
            avg_daily_demand = forecast.effective_daily_demand
            projected_days_of_supply = (
                round(part.on_hand_stock / avg_daily_demand, 2)
                if avg_daily_demand > 0
                else 999.0
            )
            projected_stockout_date = (
                (sim_now + timedelta(days=projected_days_of_supply)).date().isoformat()
                if avg_daily_demand > 0
                else None
            )
            days_to_new_eta = max(
                0.0, round((revised_eta - sim_now).total_seconds() / 86400.0, 2)
            )
            buffer_days = 3.0
            days_short_of_eta = round(
                max(0.0, days_to_new_eta - projected_days_of_supply), 2
            )
            stockout_risk = (
                days_to_new_eta >= (projected_days_of_supply - buffer_days)
                if avg_daily_demand > 0
                else (part.on_hand_stock <= part.rop)
            )
            is_critical_impact = (
                part.criticality == Criticality.CRITICAL.value or stockout_risk
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
                    required_cc_personas=[
                        PersonaKey.INVENTORY_MANAGER.value,
                        PersonaKey.PROCUREMENT.value,
                    ],
                    context_facts={
                        "promised_eta": promised_str,
                        "revised_eta": revised_eta.date().isoformat(),
                        "delay_reason": triage_result.delay_reason,
                        "part_criticality": part.criticality,
                        "on_hand_stock": part.on_hand_stock,
                        "rop": part.rop,
                        "avg_daily_demand": avg_daily_demand,
                        "projected_days_of_supply": projected_days_of_supply,
                        "projected_stockout_date": projected_stockout_date,
                        "days_to_new_eta": days_to_new_eta,
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
                stockout_note = (
                    f"projected stockout {projected_stockout_date} "
                    f"({projected_days_of_supply:.1f}d supply vs "
                    f"{days_to_new_eta:.1f}d to revised ETA)"
                    if projected_stockout_date
                    else f"on-hand: {part.on_hand_stock}, ROP: {part.rop}"
                )
                created_alert = create_action_item(
                    session,
                    type="CRITICAL_DELAY",
                    severity=AlertSeverity.CRITICAL,
                    part_criticality=part.criticality,
                    days_to_stockout=projected_days_of_supply,
                    title=(
                        f"CRITICAL delay on {matched_po.po_number} ({part.sku}) "
                        "— Escalate to Procurement"
                    ),
                    summary=(
                        f"{supplier.name} delayed {matched_po.po_number} to "
                        f"{revised_eta.date().isoformat()} ({stockout_note}).{image_clause} "
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
                        "avg_daily_demand": avg_daily_demand,
                        "projected_days_of_supply": projected_days_of_supply,
                        "projected_stockout_date": projected_stockout_date,
                        "days_to_new_eta": days_to_new_eta,
                        "days_short_of_eta": days_short_of_eta,
                        "image_attachments": image_paths,
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
                    severity=AlertSeverity.HIGH if image_names else AlertSeverity.INFO,
                    part_criticality=part.criticality,
                    days_to_stockout=projected_days_of_supply,
                    title=f"Minor delay noted on {matched_po.po_number} ({part.sku})",
                    summary=(
                        f"{supplier.name} revised ETA for {matched_po.po_number} to "
                        f"{revised_eta.date().isoformat()}. Part is {part.criticality} with "
                        f"sufficient stock ({projected_days_of_supply:.1f}d supply vs "
                        f"{days_to_new_eta:.1f}d to ETA); no expedite required.{image_clause}"
                    ),
                    po_id=matched_po.id,
                    part_id=part.id,
                    suggested_actions=(
                        [{"action": "add_note", "label": "Log Inspection Note"}]
                        if image_names
                        else []
                    ),
                    cta_payload={
                        "po_id": matched_po.id,
                        "revised_eta": revised_eta.date().isoformat(),
                        "avg_daily_demand": avg_daily_demand,
                        "projected_days_of_supply": projected_days_of_supply,
                        "projected_stockout_date": projected_stockout_date,
                        "days_to_new_eta": days_to_new_eta,
                        "image_attachments": image_paths,
                    },
                )

            if has_logistics_payload(triage_result):
                handle_followup_logistics_update(
                    session,
                    po=matched_po,
                    part=part,
                    supplier=supplier,
                    email=email,
                    triage_result=triage_result,
                    create_plain_message_fallback=False,
                )

        elif triage_result.intent == EmailIntent.GENERAL_REPLY:
            if image_names:
                created_alert = create_action_item(
                    session,
                    type="SUPPLIER_ATTACHMENT_REVIEW",
                    severity=AlertSeverity.HIGH,
                    part_criticality=part.criticality,
                    title=f"Damage / photo attachment on {matched_po.po_number} ({part.sku})",
                    summary=f"{triage_result.summary_for_manager}{image_clause}",
                    po_id=matched_po.id,
                    part_id=part.id,
                    suggested_actions=[{"action": "add_note", "label": "Log Inspection Note"}],
                    cta_payload={
                        "po_id": matched_po.id,
                        "comm_id": email.id,
                        "image_attachments": image_paths,
                    },
                )
            else:
                follow_res = handle_followup_logistics_update(
                    session,
                    po=matched_po,
                    part=part,
                    supplier=supplier,
                    email=email,
                    triage_result=triage_result,
                    create_plain_message_fallback=True,
                )
                created_alert = follow_res["alert"]

        return {
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
        """Extract PDF invoice(s) via Lex, run 3-way match, and drive quarantine/release."""
        return process_pdf_invoice_email(
            session,
            self,
            email=email,
            supplier=supplier,
            matched_po=matched_po,
            po_by_number=po_by_number,
            triage_result=triage_result,
        )

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
        return handle_invoice_match_outcome(
            session,
            self,
            po=po,
            part=part,
            supplier=supplier,
            invoice=invoice,
            match_result=match_result,
            thread_id=thread_id,
            thread_subject=thread_subject,
        )

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
        auto_resolve_po_alerts(
            session,
            po_id,
            (
                "OVERDUE_PO",
                "PRE_ETA_CHECKIN_DRAFT",
                "MISSING_DISPATCH_DETAILS",
                "MISSING_DISPATCH_DETAILS_DUE",
                "DISPATCH_DETAILS_DEFERRED",
            ),
        )

        alert_id: int | None = None
        draft_comm_id: int | None = None
        po = session.get(PurchaseOrder, po_id)
        assert po is not None
        part = session.get(Part, po.part_id)
        supplier = session.get(Supplier, po.supplier_id)

        if match_result is not None:
            latest_invoice = session.scalar(
                select(Invoice)
                .where(Invoice.po_id == po_id)
                .order_by(Invoice.version.desc(), Invoice.id.desc())
                .limit(1)
            )
            if part is not None and supplier is not None and latest_invoice is not None:
                outcome = handle_invoice_match_outcome(
                    session,
                    self,
                    po=po,
                    part=part,
                    supplier=supplier,
                    invoice=latest_invoice,
                    match_result=match_result,
                )
                alert_id = outcome["alert_id"]
                draft_comm_id = outcome["draft_comm_id"]
        elif is_grn_damaged(grn.condition) and part is not None:
            damaged_alert = handle_damaged_grn_created(
                session,
                po=po,
                part=part,
                grn=grn,
            )
            alert_id = damaged_alert.id

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
                primary.urgency_score = compute_urgency_score(
                    severity,
                    part_criticality=rec.criticality,
                )
                primary.title = title
                primary.summary = rec.reason
                primary.suggested_actions_json = suggested
                primary.cta_payload_json = cta_payload
                alerts_updated += 1
                for dup in existing_alerts[1:]:
                    resolve_action_item(session, dup.id)
                    alerts_resolved += 1
            else:
                create_action_item(
                    session,
                    type="ROP_RECOMMENDATION",
                    severity=severity,
                    part_criticality=rec.criticality,
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

        if sent_email.category == EmailCategory.STOCK_REQUEST_CLARIFICATION.value:
            mark_stock_requests_awaiting_min_qty(session, sent_email.thread_id)

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

    def _clear_quarantined_stock_for_cancelled_po(
        self,
        session: Session,
        po: PurchaseOrder,
        *,
        was_quarantined: bool,
    ) -> int:
        if not was_quarantined:
            return 0
        held_qty = int(
            session.scalar(
                select(func.coalesce(func.sum(GoodsReceipt.received_qty), 0)).where(
                    GoodsReceipt.po_id == po.id,
                    GoodsReceipt.status == GRNStatus.HELD_QUARANTINE.value,
                )
            )
            or 0
        )
        if held_qty > 0:
            clear_quarantined_stock(session, po.part_id, held_qty)
            append_po_note(
                session,
                po,
                f"Cleared {held_qty} quarantined units upon PO cancellation.",
            )
        return held_qty

    def cancel_po_workflow(
        self,
        session: Session,
        po_id: int,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Cancel an open PO, clear any quarantined stock held against it, and resolve PO alerts."""
        po = get_po(session, po_id)
        was_quarantined = po.status == POStatus.QUARANTINED_INVOICE_MISMATCH.value
        cancel_po(session, po_id, note=note)
        cleared_qty = self._clear_quarantined_stock_for_cancelled_po(
            session, po, was_quarantined=was_quarantined
        )
        resolved_alerts = auto_resolve_po_alerts(session, po.id)
        session.flush()
        return {
            "po_id": po.id,
            "po_number": po.po_number,
            "po_status": po.status,
            "quarantined_cleared_qty": cleared_qty,
            "resolved_alerts": resolved_alerts,
        }

    def switch_to_backup_supplier(
        self,
        session: Session,
        po_id: int,
        *,
        backup_supplier_id: int | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """Cancel original PO (cleaning up quarantine & alerts) and create + draft backup PO."""
        orig_before = get_po(session, po_id)
        was_quarantined = orig_before.status == POStatus.QUARANTINED_INVOICE_MISMATCH.value

        orig_po, new_po = switch_po_to_backup_supplier(
            session,
            po_id,
            backup_supplier_id=backup_supplier_id,
            note=note,
        )
        cleared_qty = self._clear_quarantined_stock_for_cancelled_po(
            session, orig_po, was_quarantined=was_quarantined
        )
        auto_resolve_po_alerts(session, orig_po.id)

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
        alert = create_action_item(
            session,
            type="PO_DISPATCH_APPROVAL",
            severity=AlertSeverity.HIGH,
            part_criticality=part.criticality,
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
        session.flush()
        return {
            "cancelled_po_id": orig_po.id,
            "cancelled_po": orig_po,
            "new_po": new_po,
            "new_po_id": new_po.id,
            "new_po_number": new_po.po_number,
            "draft_comm_id": draft.id,
            "alert_id": alert.id,
            "quarantined_cleared_qty": cleared_qty,
        }

    def escalate_to_procurement(
        self,
        session: Session,
        po_id: int,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        """Escalate an open PO to Procurement via internal email and alert."""
        return escalate_po_to_procurement_workflow(session, po_id=po_id, reason=reason)

    def set_clock_and_watch(
        self,
        session: Session,
        target: datetime,
        *,
        allow_reset: bool = False,
    ) -> dict[str, Any]:
        target_utc = clock.ensure_utc(target)
        current = clock.now(session)
        if target_utc < current and not allow_reset:
            raise ValueError(
                "Cannot move simulated clock backwards unless allow_reset=True"
            )
        new_now = clock.set_time(session, target_utc)
        sla_events: list[SLAEvent] = (
            run_sla_watcher(session, self.lex) if target_utc >= current else []
        )
        return {
            "sim_now": new_now,
            "sla_events": sla_events,
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
        cta = item.cta_payload_json or {}

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
            if (
                item.type == "CURRENCY_MISMATCH" or cta.get("currency_mismatch")
            ) and params.get("unit_price") is None:
                raise ValueError(
                    "Cannot overwrite INR PO unit_price with a foreign-currency quote. "
                    "Use 'request_inr_quote' or supply an explicit INR unit_price."
                )
            po_id = int(params.get("po_id") or cta.get("po_id") or item.po_id or 0)
            if not po_id:
                raise ValueError("No po_id linked to this alert")
            qty_val = (
                params.get("qty")
                if params.get("qty") is not None
                else cta.get("proposed_qty")
            )
            price_val = (
                params.get("unit_price")
                if params.get("unit_price") is not None
                else cta.get("proposed_unit_price")
            )
            eta_raw = (
                params.get("revised_eta")
                or params.get("proposed_eta_date")
                or cta.get("proposed_eta_date")
                or cta.get("revised_eta")
            )
            revised_eta = _parse_iso_date_utc(eta_raw)
            po = update_po_terms(
                session,
                po_id,
                qty=int(qty_val) if qty_val is not None else None,
                unit_price=float(price_val) if price_val is not None else None,
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
                    part_criticality=part.criticality,
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

        if action == "request_inr_quote":
            po_id = int(params.get("po_id") or cta.get("po_id") or item.po_id or 0)
            if not po_id:
                raise ValueError("No po_id linked to currency mismatch alert")
            po = get_po(session, po_id)
            part = session.get(Part, po.part_id)
            supplier = session.get(Supplier, po.supplier_id)
            assert part is not None and supplier is not None
            po_curr = str(cta.get("po_currency") or _term_currency(session, part.id, supplier.id))
            prop_curr = str(cta.get("proposed_currency") or "foreign currency")
            prop_price = cta.get("proposed_unit_price")
            draft = _draft_supplier_general_email(
                session,
                self,
                supplier=supplier,
                po=po,
                part=part,
                thread_id=None,
                thread_subject=f"[{po.po_number}] {po_curr} Quote Required",
                context_facts={
                    "purpose": "inr_quote_request",
                    "po_currency": po_curr,
                    "quoted_currency": prop_curr,
                    "quoted_unit_price": prop_price,
                    "agreed_unit_price_inr": po.unit_price,
                    "requested_action": (
                        f"We cannot process {prop_curr} billing for {po.po_number}. "
                        f"Please confirm pricing in {po_curr} "
                        f"(agreed PO rate ₹{po.unit_price:,.2f}/unit) "
                        "or provide an official INR re-quote."
                    ),
                },
            )
            quote_alert = create_action_item(
                session,
                type="INR_QUOTE_REQUEST_DRAFT",
                severity=AlertSeverity.HIGH,
                part_criticality=part.criticality,
                title=f"Approve {po_curr} re-quote request for {po.po_number} ({supplier.name})",
                summary=(
                    f"Lex drafted a reply rejecting {prop_curr} billing on {po.po_number} "
                    f"and requesting an {po_curr} quote."
                ),
                po_id=po.id,
                part_id=part.id,
                draft_comm_id=draft.id,
                suggested_actions=[
                    {"action": "approve_draft", "label": "Approve & Send INR Quote Request"},
                    {"action": "reject_draft", "label": "Reject Draft"},
                ],
                cta_payload={"draft_comm_id": draft.id, "po_id": po.id},
            )
            resolve_action_item(session, item.id)
            return {
                "po_id": po.id,
                "draft_comm_id": draft.id,
                "alert_id": quote_alert.id,
            }

        if action == "escalate_procurement":
            po_id = int(params.get("po_id") or cta.get("po_id") or item.po_id or 0)
            if not po_id:
                raise ValueError("po_id is required")
            reason = str(params.get("note") or params.get("reason") or item.summary or "")
            outcome = self.escalate_to_procurement(session, po_id, reason=reason)
            return {
                "po_id": outcome["po"].id,
                "comm_id": outcome["email"].id,
                "alert_id": outcome["alert"].id,
            }

        if action == "switch_backup_supplier":
            po_id = int(params.get("po_id") or cta.get("po_id") or item.po_id or 0)
            backup_supplier_id = (
                params.get("backup_supplier_id") or cta.get("backup_supplier_id")
            )
            if not po_id:
                raise ValueError("po_id is required")
            outcome = self.switch_to_backup_supplier(
                session,
                po_id,
                backup_supplier_id=int(backup_supplier_id) if backup_supplier_id else None,
                note=str(params.get("note") or ""),
            )
            resolve_action_item(session, item.id)
            return {
                "cancelled_po_id": outcome["cancelled_po_id"],
                "new_po_id": outcome["new_po_id"],
                "new_po_number": outcome["new_po_number"],
                "draft_comm_id": outcome["draft_comm_id"],
                "alert_id": outcome["alert_id"],
                "quarantined_cleared_qty": outcome["quarantined_cleared_qty"],
            }

        if action == "cancel_po":
            po_id = int(params.get("po_id") or cta.get("po_id") or item.po_id or 0)
            if not po_id:
                raise ValueError("po_id is required")
            outcome = self.cancel_po_workflow(
                session,
                po_id,
                note=params.get("note"),
            )
            resolve_action_item(session, item.id)
            return outcome

        if action == "approve_rop":
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

        if action in STOCK_REQUEST_CTA_ACTIONS:
            return execute_stock_request_cta(
                session,
                self,
                item,
                action=action,
                params=params,
            )

        if action in DAMAGED_GOODS_CTA_ACTIONS:
            return execute_damaged_goods_cta(
                session,
                self,
                item,
                action=action,
                params=params,
            )

        if action in NET_SETTLEMENT_CTA_ACTIONS:
            return execute_net_settlement_cta(
                session,
                self,
                item,
                action=action,
                params=params,
            )

        raise ValueError(f"Unsupported alert CTA action: '{action}'")
