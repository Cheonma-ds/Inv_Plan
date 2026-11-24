import math
import statistics
from datetime import date, datetime, timedelta, timezone
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
    CommStatus,
    Criticality,
    EmailCategory,
    GRNStatus,
    InventoryTxType,
    PersonaKey,
    POStatus,
)
from backend.core.models import (
    ActionItem,
    GoodsReceipt,
    InventoryTransaction,
    Invoice,
    Part,
    PartSupplierTerm,
    POCommunication,
    PurchaseOrder,
    Supplier,
    SupplierMemoryNote,
)
from backend.inventory import (
    ConsumeResult,
    clear_quarantined_stock,
    consume_stock,
    get_part_snapshot,
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
from backend.orchestrator.lex_tools import (
    LexToolTracker,
    build_lex_tool_registry,
    cancel_pending_lex_reminders_for_po,
    evaluate_memory_advisory_alerts,
    propose_and_save_memory_note,
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
    DAILY_EWMA_ALPHA,
    DailyEWMAResult,
    compute_demand_forecast,
    compute_effective_lead_time,
    compute_ewma_from_daily_series,
    compute_part_planning_metrics,
    compute_rop_recommendation,
    evaluate_part_recommendation,
    recalculate_all,
    reconstruct_observed_daily_series,
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
_IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _build_llm_failure_triage_fallback(exc: Exception) -> EmailTriageResult:
    """Graceful AMBIGUOUS fallback when LLM triage fails after retries."""
    return EmailTriageResult(
        matched_po_number=None,
        intent=EmailIntent.AMBIGUOUS,
        confidence=0.0,
        summary_for_manager=(
            "Automated AI triage failed after retries; email escalated for manual "
            "Inventory Manager review."
        ),
        suggested_action="Review the email and attachments manually; no PO state was modified.",
        reasoning=f"[LLM Failure Fallback: {type(exc).__name__}: {exc}]",
    )

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
    res = propose_and_save_memory_note(
        session,
        supplier_id=supplier_id,
        note=note_text,
        create_alert=False,
    )
    if res.get("action") == "created" and res.get("note_id"):
        return session.get(SupplierMemoryNote, int(res["note_id"]))
    return None


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
            tool_registry=build_lex_tool_registry(session),
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

    def _create_auto_replenishment_po(
        self,
        session: Session,
        *,
        part: Part,
        supplier_id: int,
        order_qty: int,
        unit_price: float,
        quoted_lead_time_days: int,
        inventory_position_before: int,
        eval_sim_now: datetime | None = None,
    ) -> tuple[PurchaseOrder, StoredEmail, ActionItem]:
        created_po = create_po(
            session,
            part_id=part.id,
            supplier_id=supplier_id,
            qty=order_qty,
            unit_price=unit_price,
            initial_status=POStatus.PENDING_DISPATCH_APPROVAL,
            notes=(
                f"Auto-triggered replenishment "
                f"(IP={inventory_position_before} < ROP={part.rop})."
            ),
        )
        supplier = session.get(Supplier, supplier_id)
        assert supplier is not None

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
                "quoted_lead_time_days": quoted_lead_time_days,
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
                {"action": "edit_po", "label": "Edit PO"},
            ],
            cta_payload={"draft_comm_id": draft_email.id, "po_id": created_po.id},
        )
        evaluate_memory_advisory_alerts(
            session,
            po=created_po,
            part=part,
            supplier=supplier,
            trigger_stage="po_created",
            sim_now=eval_sim_now,
        )
        return created_po, draft_email, alert

    def _sync_burn_and_stockout_alerts(
        self,
        session: Session,
        part: Part,
        *,
        sim_now: datetime,
    ) -> list[ActionItem]:
        """Emit deduplicated ACCELERATED_BURN (pre-zero) and STOCKOUT_ACTIVE (at 0) alerts."""
        created: list[ActionItem] = []

        if part.on_hand_stock == 0:
            existing_stockout = session.scalar(
                select(ActionItem).where(
                    ActionItem.type == "STOCKOUT_ACTIVE",
                    ActionItem.part_id == part.id,
                    ActionItem.status == ActionItemStatus.OPEN.value,
                )
            )
            if existing_stockout is None:
                earliest_po = session.scalar(
                    select(PurchaseOrder)
                    .where(
                        PurchaseOrder.part_id == part.id,
                        PurchaseOrder.status.notin_(
                            (POStatus.CLOSED.value, POStatus.CANCELLED.value)
                        ),
                    )
                    .order_by(PurchaseOrder.id.asc())
                    .limit(1)
                )
                po_clause = (
                    f" Open PO {earliest_po.po_number} is in progress."
                    if earliest_po is not None
                    else " No active replenishment PO exists yet."
                )
                alert = create_action_item(
                    session,
                    type="STOCKOUT_ACTIVE",
                    severity=AlertSeverity.CRITICAL,
                    part_criticality=part.criticality,
                    days_to_stockout=0.0,
                    title=f"Stockout active: {part.sku} hit 0 on-hand units",
                    summary=(
                        f"{part.name} ({part.sku}) has 0 units on hand.{po_clause} "
                        "Immediate replenishment or expedite action is required."
                    ),
                    po_id=earliest_po.id if earliest_po is not None else None,
                    part_id=part.id,
                    suggested_actions=[{"action": "add_note", "label": "Log Expedite Decision"}],
                    cta_payload={
                        "po_id": earliest_po.id if earliest_po is not None else None,
                        "part_id": part.id,
                    },
                )
                created.append(alert)
            return created

        metrics = compute_part_planning_metrics(session, part, sim_now=sim_now)
        if (
            metrics.earliest_po_eta is not None
            and metrics.predicted_stockout_date is not None
            and metrics.predicted_stockout_date < metrics.earliest_po_eta
        ):
            existing_burn = session.scalar(
                select(ActionItem).where(
                    ActionItem.type == "ACCELERATED_BURN",
                    ActionItem.part_id == part.id,
                    ActionItem.status == ActionItemStatus.OPEN.value,
                )
            )
            if existing_burn is None:
                dispatched_pos = list(
                    session.scalars(
                        select(PurchaseOrder)
                        .where(
                            PurchaseOrder.part_id == part.id,
                            PurchaseOrder.status.notin_(
                                (
                                    POStatus.PENDING_DISPATCH_APPROVAL.value,
                                    POStatus.CLOSED.value,
                                    POStatus.CANCELLED.value,
                                )
                            ),
                        )
                        .order_by(PurchaseOrder.id.asc())
                    ).all()
                )
                earliest_po = dispatched_pos[0] if dispatched_pos else None
                days_to_eta = max(
                    0.0,
                    (
                        clock.ensure_utc(metrics.earliest_po_eta)
                        - clock.ensure_utc(sim_now)
                    ).total_seconds()
                    / 86400.0,
                )
                days_to_so = metrics.days_to_stockout or 0.0
                severity = (
                    AlertSeverity.CRITICAL
                    if (
                        part.criticality == Criticality.CRITICAL.value
                        or days_to_so <= 3.0
                    )
                    else AlertSeverity.HIGH
                )
                alert = create_action_item(
                    session,
                    type="ACCELERATED_BURN",
                    severity=severity,
                    part_criticality=part.criticality,
                    days_to_stockout=days_to_so,
                    title=(
                        f"Accelerated burn: {part.sku} projected to stock out "
                        f"before PO arrival"
                    ),
                    summary=(
                        f"{part.sku} has {part.on_hand_stock} on hand ({days_to_so:.1f}d runway "
                        f"at {metrics.daily_burn_ewma:.2f}/day) and is projected to stock out "
                        f"on {metrics.predicted_stockout_date.date().isoformat()}, before the "
                        f"earliest open PO ETA ({metrics.earliest_po_eta.date().isoformat()}, "
                        f"{days_to_eta:.1f}d away)."
                    ),
                    po_id=earliest_po.id if earliest_po is not None else None,
                    part_id=part.id,
                    suggested_actions=[{"action": "add_note", "label": "Log Expedite Decision"}],
                    cta_payload={
                        "po_id": earliest_po.id if earliest_po is not None else None,
                        "part_id": part.id,
                        "days_to_stockout": round(days_to_so, 2),
                        "days_until_eta": round(days_to_eta, 2),
                    },
                )
                created.append(alert)
        return created

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

        part = session.get(Part, part_id)
        assert part is not None

        if consume_res.reorder_triggered is not None:
            trig = consume_res.reorder_triggered
            created_po, draft_email, alert = self._create_auto_replenishment_po(
                session,
                part=part,
                supplier_id=trig.primary_supplier_id,
                order_qty=trig.order_qty,
                unit_price=trig.agreed_unit_price,
                quoted_lead_time_days=trig.quoted_lead_time_days,
                inventory_position_before=trig.inventory_position_before,
            )
            created_alerts.append(alert)

        sim_now = clock.now(session)
        created_alerts.extend(
            self._sync_burn_and_stockout_alerts(session, part, sim_now=sim_now)
        )

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

        triage_image_attachments: list[tuple[str, bytes]] = []
        for raw_img in image_paths:
            img_file = Path(raw_img)
            if not img_file.is_file():
                continue
            img_bytes = img_file.read_bytes()
            if img_bytes and len(img_bytes) <= 10 * 1024 * 1024:
                mime = _IMAGE_MIME_TYPES.get(img_file.suffix.lower(), "image/jpeg")
                triage_image_attachments.append((mime, img_bytes))

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
        tracker = LexToolTracker()
        tool_registry = build_lex_tool_registry(
            session, context_comm_id=email.id, tracker=tracker
        )
        try:
            triage_result: EmailTriageResult = self.lex.triage_email(
                session,
                triage_input,
                linked_comm_id=email.id,
                pdf_bytes=triage_pdf_bytes,
                image_attachments=triage_image_attachments or None,
                tool_registry=tool_registry,
            )
        except AssertionError:
            raise
        except Exception as exc:
            if triage_pdf_bytes is not None:
                # Corrupt PDF failed multimodal triage -> retry text/image triage safely
                unreadable_pdf_detected = True
                try:
                    triage_result = self.lex.triage_email(
                        session,
                        triage_input,
                        linked_comm_id=email.id,
                        pdf_bytes=None,
                        image_attachments=triage_image_attachments or None,
                        tool_registry=tool_registry,
                    )
                except AssertionError:
                    raise
                except Exception as retry_exc:
                    triage_result = _build_llm_failure_triage_fallback(retry_exc)
            else:
                triage_result = _build_llm_failure_triage_fallback(exc)

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

        if (
            triage_result.new_memory_note
            and triage_result.new_memory_note.strip().lower()
            not in tracker.handled_memory_texts
        ):
            mem_res = propose_and_save_memory_note(
                session,
                supplier_id=supplier.id,
                note=triage_result.new_memory_note,
                reasoning=triage_result.reasoning,
                po=matched_po,
                comm_id=email.id,
                create_alert=False,
                tracker=tracker,
            )
            if mem_res.get("action") == "created" and mem_res.get("note_id"):
                learned_note = session.get(SupplierMemoryNote, int(mem_res["note_id"]))
                if learned_note is not None:
                    create_supplier_memory_alert(
                        session,
                        supplier=supplier,
                        note=learned_note,
                        po=matched_po,
                        comm_id=email.id,
                        reasoning=triage_result.reasoning,
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
                    details_promised_days=add.details_promised_days,
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
                    tracker=tracker,
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
        tracker: LexToolTracker | None = None,
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
                    tracker=tracker,
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
                    engine=self,
                    tracker=tracker,
                )
                created_alert = follow_res["alert"]
                created_draft = follow_res.get("draft")

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
                    engine=self,
                    tracker=tracker,
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
                    engine=self,
                    tracker=tracker,
                )
                created_alert = follow_res["alert"]
                created_draft = follow_res.get("draft")

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
        cancel_pending_lex_reminders_for_po(session, po_id, reason="Goods receipt logged")
        auto_resolve_po_alerts(
            session,
            po_id,
            (
                "OVERDUE_PO",
                "PRE_ETA_CHECKIN_DRAFT",
                "MISSING_DISPATCH_DETAILS",
                "MISSING_DISPATCH_DETAILS_DUE",
                "DISPATCH_DETAILS_DEFERRED",
                "DISPATCH_DEFERRAL_STOCKOUT_RISK",
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
                f"Update ROP/T for {rec.sku}: "
                f"ROP {rec.current_rop} → {rec.recommended_rop}, "
                f"T {rec.current_replenishment_point} → {rec.recommended_replenishment_point}"
            )
            suggested = [
                {
                    "action": "approve_rop",
                    "label": (
                        f"Approve ROP {rec.recommended_rop} / "
                        f"T {rec.recommended_replenishment_point}"
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
        cancel_pending_lex_reminders_for_po(session, po.id, reason="PO cancelled")
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
        cancel_pending_lex_reminders_for_po(
            session, orig_po.id, reason="Switched to backup supplier"
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
                "total_value": round(new_po.qty * new_po.unit_price, 2),
                "quoted_lead_time_days": quoted_lt,
                "part_criticality": part.criticality,
                "backup_failover": True,
                "instruction": (
                    "Initial PO placement to backup supplier. Issue this as a brand-new order "
                    "and request acknowledgement; do NOT check dispatch status."
                ),
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
                {"action": "edit_po", "label": "Edit PO"},
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

    def _get_part_supplier_term(
        self, session: Session, *, part_id: int, supplier_id: int
    ) -> tuple[Part, Supplier, PartSupplierTerm]:
        part = session.get(Part, part_id)
        if part is None:
            raise ValueError(f"Part {part_id} not found")
        supplier = session.get(Supplier, supplier_id)
        if supplier is None:
            raise ValueError(f"Supplier {supplier_id} not found")
        term = session.scalar(
            select(PartSupplierTerm).where(
                PartSupplierTerm.part_id == part_id,
                PartSupplierTerm.supplier_id == supplier_id,
            )
        )
        if term is None:
            raise ValueError(
                f"Supplier {supplier.name} is not configured for part {part.sku}"
            )
        return part, supplier, term

    def generate_manual_po_draft(
        self,
        session: Session,
        *,
        part_id: int,
        supplier_id: int,
        qty: int,
    ) -> dict[str, Any]:
        """Generate an editable Lex PO_DISPATCH email draft for a user-configured manual PO."""
        if qty <= 0:
            raise ValueError("Order quantity must be greater than 0")
        part, supplier, term = self._get_part_supplier_term(
            session, part_id=part_id, supplier_id=supplier_id
        )
        sim_now = clock.now(session)
        currency = term.currency or "INR"
        total_value = round(qty * term.agreed_unit_price, 2)
        draft_res = self.lex.draft_email(
            session,
            EmailDraftInput(
                category=EmailCategory.PO_DISPATCH,
                po_number="",
                part_sku=part.sku,
                part_name=part.name,
                qty=qty,
                unit_price=term.agreed_unit_price,
                currency=currency,
                recipient_persona=supplier.persona_key,
                recipient_name=supplier.name,
                current_date=sim_now.date().isoformat(),
                supplier_memory_notes=_supplier_memory_texts(session, supplier.id),
                required_cc_personas=[PersonaKey.INVENTORY_MANAGER.value, PersonaKey.LEX.value],
                context_facts={
                    "currency": currency,
                    "order_qty": qty,
                    "unit_price": term.agreed_unit_price,
                    "total_value": total_value,
                    "quoted_lead_time_days": term.quoted_lead_time_days,
                    "effective_lead_time_days": term.effective_lead_time_days,
                    "part_criticality": part.criticality,
                    "manual_po": True,
                    "instruction": (
                        "Initial PO placement for a new manual order. Do not invent a PO number "
                        "(the system will assign PO-XXXX on send); request supplier "
                        "acknowledgement and dispatch timeline."
                    ),
                },
            ),
        )
        return {
            "subject": draft_res.subject,
            "body": draft_res.body,
            "to_personas": draft_res.to_personas,
            "cc_personas": [PersonaKey.LEX.value, PersonaKey.INVENTORY_MANAGER.value],
            "unit_price": term.agreed_unit_price,
            "total_amount": total_value,
            "currency": currency,
        }

    def create_manual_po(
        self,
        session: Session,
        *,
        part_id: int,
        supplier_id: int,
        qty: int,
        subject: str,
        body: str,
    ) -> dict[str, Any]:
        """Create a manual PO, dispatch email to supplier (CC lex + manager), and start SLA."""
        if qty <= 0:
            raise ValueError("Order quantity must be greater than 0")
        clean_subject = subject.strip()
        clean_body = body.strip()
        if not clean_subject or not clean_body:
            raise ValueError("Email subject and body are required")

        part, supplier, term = self._get_part_supplier_term(
            session, part_id=part_id, supplier_id=supplier_id
        )
        sim_now = clock.now(session)
        po = create_po(
            session,
            part_id=part.id,
            supplier_id=supplier.id,
            qty=qty,
            unit_price=term.agreed_unit_price,
            initial_status=POStatus.PENDING_DISPATCH_APPROVAL,
            notes=(
                f"Manual PO created via Inventory & Parts "
                f"({qty} units @ ₹{term.agreed_unit_price:,.2f})."
            ),
        )
        promised_eta = sim_now + timedelta(days=term.quoted_lead_time_days)
        transition_po(
            session,
            po,
            POStatus.SENT_AWAITING_ACK,
            promised_eta=promised_eta,
            note="Manual PO created and dispatched to supplier.",
        )
        is_critical = part.criticality == Criticality.CRITICAL.value
        po.follow_up_count = 0
        po.next_sla_check_at = sim_now + timedelta(hours=24 if is_critical else 48)

        final_subject = (
            clean_subject
            if po.po_number in clean_subject
            else f"[{po.po_number}] {clean_subject}"
        )
        sent_email = send_email(
            session,
            OutboundMessage(
                from_persona=PersonaKey.INVENTORY_MANAGER,
                to_personas=[PersonaKey(supplier.persona_key)],
                cc_personas=[PersonaKey.LEX, PersonaKey.INVENTORY_MANAGER],
                subject=final_subject,
                body=clean_body,
                po_id=po.id,
                category=EmailCategory.PO_DISPATCH,
            ),
            allow_agent_send=False,
        )
        evaluate_memory_advisory_alerts(
            session,
            po=po,
            part=part,
            supplier=supplier,
            trigger_stage="po_created",
        )
        session.flush()
        return {"po": po, "email": sent_email}

    def dispatch_edited_po(
        self,
        session: Session,
        po_id: int,
        *,
        supplier_id: int | None = None,
        qty: int | None = None,
        unit_price: float | None = None,
        subject: str | None = None,
        body: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing PENDING_DISPATCH_APPROVAL PO, supersede drafts, and dispatch."""
        po = get_po(session, po_id)
        if po.status != POStatus.PENDING_DISPATCH_APPROVAL.value:
            raise ValueError(
                f"PO {po.po_number} must be in PENDING_DISPATCH_APPROVAL to dispatch-edited "
                f"(current status: {po.status})"
            )

        target_supplier_id = supplier_id if supplier_id is not None else po.supplier_id
        part, supplier, term = self._get_part_supplier_term(
            session, part_id=po.part_id, supplier_id=target_supplier_id
        )
        target_qty = qty if qty is not None else po.qty
        if target_qty <= 0:
            raise ValueError("Order quantity must be greater than 0")

        if unit_price is not None:
            if unit_price <= 0:
                raise ValueError("PO unit_price must be greater than 0")
            target_price = round(float(unit_price), 2)
        elif target_supplier_id != po.supplier_id:
            target_price = term.agreed_unit_price
        else:
            target_price = po.unit_price

        po.supplier_id = target_supplier_id
        po.qty = target_qty
        po.unit_price = target_price
        if note:
            append_po_note(session, po, note)

        # Supersede any existing unapproved DRAFT_PENDING_APPROVAL comms linked to this PO
        superseded_comm_ids: list[int] = []
        pending_comms = list(
            session.scalars(
                select(POCommunication).where(
                    POCommunication.po_id == po.id,
                    POCommunication.status == CommStatus.DRAFT_PENDING_APPROVAL.value,
                )
            ).all()
        )
        for comm in pending_comms:
            reject_draft(session, comm.id)
            superseded_comm_ids.append(comm.id)

        sim_now = clock.now(session)
        clean_subject = (subject or "").strip()
        clean_body = (body or "").strip()
        if not clean_subject or not clean_body:
            currency = term.currency or "INR"
            draft_res = self.lex.draft_email(
                session,
                EmailDraftInput(
                    category=EmailCategory.PO_DISPATCH,
                    po_number=po.po_number,
                    part_sku=part.sku,
                    part_name=part.name,
                    qty=target_qty,
                    unit_price=target_price,
                    currency=currency,
                    recipient_persona=supplier.persona_key,
                    recipient_name=supplier.name,
                    current_date=sim_now.date().isoformat(),
                    supplier_memory_notes=_supplier_memory_texts(session, supplier.id),
                    required_cc_personas=[
                        PersonaKey.INVENTORY_MANAGER.value,
                        PersonaKey.LEX.value,
                    ],
                    context_facts={
                        "currency": currency,
                        "order_qty": target_qty,
                        "unit_price": target_price,
                        "total_value": round(target_qty * target_price, 2),
                        "quoted_lead_time_days": term.quoted_lead_time_days,
                        "effective_lead_time_days": term.effective_lead_time_days,
                        "part_criticality": part.criticality,
                        "edited_dispatch": True,
                    },
                ),
            )
            if not clean_subject:
                clean_subject = draft_res.subject
            if not clean_body:
                clean_body = draft_res.body

        final_subject = (
            clean_subject
            if po.po_number in clean_subject
            else f"[{po.po_number}] {clean_subject}"
        )
        sent_email = send_email(
            session,
            OutboundMessage(
                from_persona=PersonaKey.INVENTORY_MANAGER,
                to_personas=[PersonaKey(supplier.persona_key)],
                cc_personas=[PersonaKey.LEX, PersonaKey.INVENTORY_MANAGER],
                subject=final_subject,
                body=clean_body,
                po_id=po.id,
                category=EmailCategory.PO_DISPATCH,
            ),
            allow_agent_send=False,
        )

        promised_eta = sim_now + timedelta(days=term.quoted_lead_time_days)
        transition_po(
            session,
            po,
            POStatus.SENT_AWAITING_ACK,
            promised_eta=promised_eta,
            note=(
                f"Edited PO dispatched to {supplier.name} "
                f"({target_qty} units @ ₹{target_price:,.2f})."
            ),
        )
        is_critical = part.criticality == Criticality.CRITICAL.value
        po.follow_up_count = 0
        po.next_sla_check_at = sim_now + timedelta(hours=24 if is_critical else 48)

        resolved_alerts = 0
        open_dispatch_alerts = list(
            session.scalars(
                select(ActionItem).where(
                    ActionItem.po_id == po.id,
                    ActionItem.type == "PO_DISPATCH_APPROVAL",
                    ActionItem.status == ActionItemStatus.OPEN.value,
                )
            ).all()
        )
        for alert in open_dispatch_alerts:
            resolve_action_item(session, alert.id)
            resolved_alerts += 1

        dismissed_dispatch_alerts = list(
            session.scalars(
                select(ActionItem).where(
                    ActionItem.po_id == po.id,
                    ActionItem.type == "PO_DISPATCH_APPROVAL",
                    ActionItem.status == ActionItemStatus.DISMISSED.value,
                    ActionItem.dismiss_resolved_at.is_(None),
                )
            ).all()
        )
        for alert in dismissed_dispatch_alerts:
            alert.dismiss_resolved_at = sim_now

        session.flush()
        return {
            "po": po,
            "email": sent_email,
            "resolved_alerts": resolved_alerts,
            "superseded_comm_ids": superseded_comm_ids,
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

    def settle_daily_burn_on_clock_advance(
        self,
        session: Session,
        *,
        prev_now: datetime,
        new_now: datetime,
    ) -> list[dict[str, Any]]:
        """Settle each completed IST calendar day chronologically on clock advance.

        - Deducts predicted daily burn via fractional ``burn_carry`` accumulator.
        - Pure predicted burn day (``extra_events == 0``): keeps ``daily_burn_ewma``,
          ``daily_demand_std``, ``rop``, and ``replenishment_point`` untouched.
        - Unpredicted demand day (``extra_events > 0``): updates EWMA (alpha=0.3),
          recomputes ``daily_demand_std``, and dynamically updates ``rop`` & ``T``.
        - Evaluates replenishment triggers, ``ACCELERATED_BURN``, and ``STOCKOUT_ACTIVE``.
        """
        target_last_completed = clock.to_ist_date(new_now) - timedelta(days=1)
        prev_ist_date = clock.to_ist_date(prev_now)
        parts = list(session.scalars(select(Part).order_by(Part.id.asc())).all())
        settled_events: list[dict[str, Any]] = []

        for part in parts:
            start_date = (
                date.fromisoformat(part.last_burn_settled_date) + timedelta(days=1)
                if part.last_burn_settled_date
                else prev_ist_date
            )
            if start_date > target_last_completed:
                continue

            cur_day = start_date
            while cur_day <= target_last_completed:
                # 1. Discrete unpredicted consumption on IST date cur_day
                day_txs = session.scalars(
                    select(InventoryTransaction).where(
                        InventoryTransaction.part_id == part.id,
                        InventoryTransaction.type == InventoryTxType.CONSUMPTION.value,
                        InventoryTransaction.is_predicted_burn.is_(False),
                        InventoryTransaction.qty_delta < 0,
                    )
                ).all()
                extra_events = sum(
                    abs(int(tx.qty_delta))
                    for tx in day_txs
                    if clock.to_ist_date(tx.timestamp) == cur_day
                )

                # 2. Deduct physical simulated burn via carry accumulator
                prev_ewma = float(part.daily_burn_ewma)
                raw_consume = float(part.burn_carry) + prev_ewma
                int_consume = int(math.floor(raw_consume + 1e-9))
                new_carry = max(0.0, raw_consume - float(int_consume))
                actual_deduct = min(int(part.on_hand_stock), int_consume)

                burn_ts = datetime(
                    cur_day.year, cur_day.month, cur_day.day, 23, 59, 0, tzinfo=clock.IST
                ).astimezone(timezone.utc)

                if actual_deduct > 0:
                    part.on_hand_stock -= actual_deduct
                    session.add(
                        InventoryTransaction(
                            part_id=part.id,
                            type=InventoryTxType.CONSUMPTION.value,
                            qty_delta=-actual_deduct,
                            timestamp=burn_ts,
                            is_predicted_burn=True,
                            note="Simulated daily burn",
                        )
                    )
                part.burn_carry = new_carry
                part.last_burn_settled_date = cur_day.isoformat()
                session.flush()

                # 3. Update EWMA & ROP/T ONLY when unpredicted demand occurred
                if extra_events > 0:
                    c_d = prev_ewma + float(extra_events)
                    new_ewma = (
                        DAILY_EWMA_ALPHA * c_d + (1.0 - DAILY_EWMA_ALPHA) * prev_ewma
                    )
                    part.daily_burn_ewma = new_ewma

                    _, c_series, e_series, has_pre_oct1 = reconstruct_observed_daily_series(
                        session, part, end_date=cur_day
                    )
                    if has_pre_oct1 and len(c_series) >= 2:
                        new_std = statistics.stdev(c_series)
                        if len(e_series) >= 8:
                            part.daily_burn_prev_7d = e_series[-8]
                    else:
                        synth_series = ([prev_ewma] * 89) + [c_d]
                        new_std = statistics.stdev(synth_series)

                    part.daily_demand_std = new_std

                    # Check intermittent floor over trailing 90d window
                    tail_90 = c_series[-90:] if has_pre_oct1 else [prev_ewma] * 89 + [c_d]
                    (
                        _,
                        _,
                        _,
                        _,
                        is_intermittent,
                        zero_days,
                        floor_daily,
                        _,
                    ) = compute_ewma_from_daily_series(tail_90)
                    effective_daily = (
                        max(new_ewma, floor_daily)
                        if (is_intermittent and floor_daily is not None)
                        else new_ewma
                    )

                    primary_term = session.scalar(
                        select(PartSupplierTerm)
                        .where(PartSupplierTerm.part_id == part.id)
                        .order_by(PartSupplierTerm.supplier_rank.asc())
                        .limit(1)
                    )
                    sup_name: str | None = None
                    proxy_term: PartSupplierTerm | None = None
                    if primary_term is not None:
                        sup = session.get(Supplier, primary_term.supplier_id)
                        sup_name = sup.name if sup is not None else None
                        lt_calc = compute_effective_lead_time(
                            session, primary_term, sim_now=burn_ts
                        )
                        proxy_term = PartSupplierTerm(
                            id=primary_term.id,
                            part_id=primary_term.part_id,
                            supplier_id=primary_term.supplier_id,
                            supplier_rank=primary_term.supplier_rank,
                            agreed_unit_price=primary_term.agreed_unit_price,
                            quoted_lead_time_days=primary_term.quoted_lead_time_days,
                            effective_lead_time_days=lt_calc.effective_lead_time_days,
                            lead_time_std_dev=lt_calc.lead_time_std_dev,
                            moq=primary_term.moq,
                            last_validated_at=primary_term.last_validated_at,
                        )

                    daily_fc = DailyEWMAResult(
                        part_id=part.id,
                        sku=part.sku,
                        daily_series_90d=tail_90,
                        daily_burn_ewma=new_ewma,
                        daily_burn_prev_7d=part.daily_burn_prev_7d,
                        trend_pct=0.0,
                        daily_demand_std=new_std,
                        is_intermittent=is_intermittent,
                        zero_day_count=zero_days,
                        rolling_peak_floor_daily=floor_daily,
                        manual_demand_override=part.manual_demand_override,
                        effective_daily_demand=effective_daily,
                    )
                    rop_rec = compute_rop_recommendation(
                        part, proxy_term, sup_name, daily_fc
                    )
                    if rop_rec.recommended_rop > 0:
                        part.rop = rop_rec.recommended_rop
                        part.replenishment_point = rop_rec.recommended_replenishment_point
                else:
                    # Pure predicted burn day: leave ewma, std, rop, and T untouched.
                    # Advance daily_burn_prev_7d when pre-Oct-1 history exists.
                    _, _, e_series, has_pre_oct1 = reconstruct_observed_daily_series(
                        session, part, end_date=cur_day
                    )
                    if has_pre_oct1 and len(e_series) >= 8:
                        part.daily_burn_prev_7d = e_series[-8]

                session.flush()

                # 4. Evaluate replenishment trigger & burn/stockout alerts on day cur_day
                snap = get_part_snapshot(session, part.id)
                if snap.inventory_position < part.rop and snap.active_po_id is None:
                    primary_term = session.scalar(
                        select(PartSupplierTerm)
                        .where(
                            PartSupplierTerm.part_id == part.id,
                            PartSupplierTerm.supplier_rank == 1,
                        )
                        .limit(1)
                    )
                    if primary_term is not None:
                        order_qty = max(
                            part.replenishment_point - snap.inventory_position,
                            primary_term.moq,
                        )
                        po, _, po_alert = self._create_auto_replenishment_po(
                            session,
                            part=part,
                            supplier_id=primary_term.supplier_id,
                            order_qty=order_qty,
                            unit_price=primary_term.agreed_unit_price,
                            quoted_lead_time_days=primary_term.quoted_lead_time_days,
                            inventory_position_before=snap.inventory_position,
                            eval_sim_now=min(new_now, burn_ts),
                        )
                        settled_events.append(
                            {
                                "part_id": part.id,
                                "date": cur_day.isoformat(),
                                "po_id": po.id,
                                "alert_id": po_alert.id,
                            }
                        )

                day_eval_now = min(new_now, burn_ts)
                burn_alerts = self._sync_burn_and_stockout_alerts(
                    session, part, sim_now=day_eval_now
                )
                for ba in burn_alerts:
                    settled_events.append(
                        {
                            "part_id": part.id,
                            "date": cur_day.isoformat(),
                            "alert_id": ba.id,
                            "alert_type": ba.type,
                        }
                    )

                cur_day += timedelta(days=1)

        session.flush()
        return settled_events

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
        burn_events = (
            self.settle_daily_burn_on_clock_advance(
                session, prev_now=current, new_now=new_now
            )
            if target_utc > current
            else []
        )
        sla_events: list[SLAEvent] = (
            run_sla_watcher(session, self.lex) if target_utc >= current else []
        )
        return {
            "sim_now": new_now,
            "sla_events": sla_events,
            "burn_events": burn_events,
        }

    def advance_clock_and_watch(
        self,
        session: Session,
        *,
        hours: int = 0,
        days: int = 0,
    ) -> dict[str, Any]:
        prev_now = clock.now(session)
        new_now = clock.advance(session, hours=hours, days=days)
        burn_events = self.settle_daily_burn_on_clock_advance(
            session, prev_now=prev_now, new_now=new_now
        )
        sla_events: list[SLAEvent] = run_sla_watcher(session, self.lex)
        return {
            "sim_now": new_now,
            "sla_events": sla_events,
            "burn_events": burn_events,
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
        burn_events = self.settle_daily_burn_on_clock_advance(
            session, prev_now=current, new_now=new_now
        )
        sla_events = run_sla_watcher(session, self.lex)
        return {
            "sim_now": new_now,
            "sla_events": sla_events,
            "burn_events": burn_events,
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

        if action == "edit_po":
            po_id = int(params.get("po_id") or cta.get("po_id") or item.po_id or 0)
            if not po_id:
                raise ValueError("No po_id linked to this alert")
            outcome = self.dispatch_edited_po(
                session,
                po_id,
                supplier_id=(
                    int(params["supplier_id"])
                    if params.get("supplier_id") is not None
                    else None
                ),
                qty=int(params["qty"]) if params.get("qty") is not None else None,
                unit_price=(
                    float(params["unit_price"])
                    if params.get("unit_price") is not None
                    else None
                ),
                subject=params.get("subject"),
                body=params.get("body"),
                note=params.get("note"),
            )
            return {
                "po_id": outcome["po"].id,
                "po_status": outcome["po"].status,
                "comm_id": outcome["email"].id,
                "resolved_alerts": outcome["resolved_alerts"],
                "superseded_comm_ids": outcome["superseded_comm_ids"],
            }

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
                        {"action": "edit_po", "label": "Edit PO"},
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
