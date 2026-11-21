from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.agents.lex import (
    CONFIDENCE_THRESHOLD,
    EmailDraftInput,
    EmailIntent,
    EmailTriageResult,
    InvoiceExtractionResult,
    PDFDocumentType,
)
from backend.core import clock
from backend.core.enums import (
    AlertSeverity,
    Criticality,
    EmailCategory,
    PersonaKey,
)
from backend.core.models import (
    ActionItem,
    GoodsReceipt,
    Invoice,
    Part,
    PartSupplierTerm,
    PurchaseOrder,
    Supplier,
)
from backend.inventory import (
    clear_quarantined_stock,
    hold_in_quarantine,
    receive_stock_direct,
    release_quarantined_stock,
)
from backend.mailroom import (
    OutboundMessage,
    StoredEmail,
    annotate_triage,
    inspect_pdf_readability,
    save_draft,
)
from backend.orchestrator.sla_watcher import auto_resolve_po_alerts
from backend.planning import refresh_supplier_metrics
from backend.po import (
    LINE_TOTAL_TOLERANCE_INR,
    ThreeWayMatchResult,
    append_po_note,
    apply_damaged_goods_disposition,
    create_action_item,
    record_net_settlement_invoice,
    resolve_action_item,
    submit_invoice_and_match,
)

if TYPE_CHECKING:
    from backend.orchestrator.workflows import WorkflowEngine

DAMAGED_GOODS_CTA_ACTIONS = {
    "release_damaged_goods",
    "draft_damage_claim",
}
NET_SETTLEMENT_CTA_ACTIONS = {
    "approve_net_settlement",
}


def _term_currency(session: Session, part_id: int, supplier_id: int) -> str:
    term = session.scalar(
        select(PartSupplierTerm).where(
            PartSupplierTerm.part_id == part_id,
            PartSupplierTerm.supplier_id == supplier_id,
        )
    )
    return term.currency if term is not None and term.currency else "INR"


def _draft_supplier_general_email(
    session: Session,
    engine: "WorkflowEngine",
    *,
    supplier: Supplier,
    po: PurchaseOrder | None,
    part: Part | None,
    thread_id: str | None,
    thread_subject: str | None,
    context_facts: dict[str, Any],
    required_cc_personas: list[str] | None = None,
) -> StoredEmail:
    sim_now = clock.now(session)
    currency = (
        _term_currency(session, part.id, supplier.id)
        if part is not None
        else "INR"
    )
    draft_res = engine.lex.draft_email(
        session,
        EmailDraftInput(
            category=EmailCategory.GENERAL,
            po_number=po.po_number if po is not None else "",
            part_sku=part.sku if part is not None else "GENERAL",
            part_name=part.name if part is not None else "Purchase Order Documents",
            qty=po.qty if po is not None else 0,
            unit_price=po.unit_price if po is not None else 0.0,
            currency=currency,
            recipient_persona=supplier.persona_key,
            recipient_name=supplier.name,
            current_date=sim_now.date().isoformat(),
            thread_subject=thread_subject,
            required_cc_personas=required_cc_personas or [PersonaKey.INVENTORY_MANAGER.value],
            context_facts={"currency": currency, **context_facts},
        ),
        linked_po_id=po.id if po is not None else None,
    )
    return save_draft(
        session,
        OutboundMessage(
            from_persona=PersonaKey.LEX,
            to_personas=[PersonaKey(p) for p in draft_res.to_personas],
            cc_personas=[PersonaKey(p) for p in draft_res.cc_personas],
            subject=draft_res.subject,
            body=draft_res.body,
            po_id=po.id if po is not None else None,
            thread_id=thread_id,
            category=EmailCategory.GENERAL,
        ),
    )


def _escalate_unreadable_pdf(
    session: Session,
    engine: "WorkflowEngine",
    *,
    email: StoredEmail,
    supplier: Supplier,
    matched_po: PurchaseOrder | None,
    triage_result: EmailTriageResult,
    filename: str,
    reason: str,
    attachment_path: str,
) -> dict[str, Any]:
    part = session.get(Part, matched_po.part_id) if matched_po is not None else None
    draft: StoredEmail | None = None
    try:
        draft = _draft_supplier_general_email(
            session,
            engine,
            supplier=supplier,
            po=matched_po,
            part=part,
            thread_id=email.thread_id,
            thread_subject=email.subject,
            context_facts={
                "purpose": "unlocked_pdf_request",
                "filename": filename,
                "error_reason": reason,
                "requested_action": (
                    f"Please resend '{filename}' as an unlocked, readable standard PDF "
                    "so we can process 3-way matching."
                ),
            },
        )
    except AssertionError:
        raise
    except Exception:
        draft = None

    suggested = []
    if draft is not None:
        suggested.extend(
            [
                {"action": "approve_draft", "label": "Approve & Request Unlocked PDF"},
                {"action": "reject_draft", "label": "Reject Draft"},
            ]
        )
    suggested.append({"action": "add_note", "label": "Log Review Note"})

    alert = create_action_item(
        session,
        type="INVOICE_EXTRACTION_REVIEW",
        severity=AlertSeverity.HIGH,
        title=f"Unreadable or locked PDF from {supplier.name}: {filename}",
        summary=(
            f"Attached PDF ('{filename}') is password-protected or unreadable and could not be "
            f"parsed automatically ({reason}). Zero PO or stock state changes were made."
        ),
        po_id=matched_po.id if matched_po else None,
        part_id=matched_po.part_id if matched_po else None,
        draft_comm_id=draft.id if draft else None,
        suggested_actions=suggested,
        cta_payload={
            "comm_id": email.id,
            "attachment_path": attachment_path,
            "filename": filename,
            "reason": reason,
            "draft_comm_id": draft.id if draft else None,
        },
    )
    session.flush()
    return {
        "triage": triage_result,
        "matched_po_id": matched_po.id if matched_po else None,
        "alert_id": alert.id,
        "draft_comm_id": draft.id if draft else None,
        "invoice_extraction": None,
        "invoice_id": None,
        "match_result": None,
    }


def _extract_pdf_safe(
    session: Session,
    engine: "WorkflowEngine",
    *,
    pdf_file: Path,
    supplier_name: str,
    comm_id: int,
    po_id: int | None,
) -> tuple[InvoiceExtractionResult | None, str | None]:
    if not pdf_file.is_file():
        return None, "Attachment file could not be read from disk"
    raw_bytes = pdf_file.read_bytes()
    readability_err = inspect_pdf_readability(raw_bytes)
    if readability_err is not None:
        return None, readability_err
    try:
        extraction = engine.lex.extract_invoice(
            session,
            raw_bytes,
            filename=pdf_file.name,
            supplier_name=supplier_name,
            linked_comm_id=comm_id,
            linked_po_id=po_id,
        )
        return extraction, None
    except AssertionError:
        raise
    except Exception as exc:
        return None, f"PDF parsing failed: {exc}"


def _build_damaged_goods_alert(
    session: Session,
    *,
    po: PurchaseOrder,
    part: Part,
    grn: GoodsReceipt,
    summary: str,
    invoice: Invoice | None = None,
) -> ActionItem:
    auto_resolve_po_alerts(session, po.id, ("DAMAGED_GOODS_HOLD",))
    severity = (
        AlertSeverity.CRITICAL
        if (part.criticality == Criticality.CRITICAL.value or part.on_hand_stock <= part.rop)
        else AlertSeverity.HIGH
    )
    return create_action_item(
        session,
        type="DAMAGED_GOODS_HOLD",
        severity=severity,
        part_criticality=part.criticality,
        title=f"Damaged goods held in quarantine: {po.po_number} ({part.sku})",
        summary=summary,
        po_id=po.id,
        part_id=part.id,
        suggested_actions=[
            {"action": "release_damaged_goods", "label": "Release Usable Qty"},
            {"action": "draft_damage_claim", "label": "Draft Damage Claim / Replacement"},
            {"action": "escalate_procurement", "label": "Escalate to Procurement"},
            {"action": "add_note", "label": "Add Note"},
        ],
        cta_payload={
            "po_id": po.id,
            "part_id": part.id,
            "grn_id": grn.id,
            "held_qty": grn.received_qty,
            "grn_condition": grn.condition,
            "invoice_id": invoice.id if invoice else None,
            "invoice_number": invoice.invoice_number if invoice else None,
            "invoice_match_status": invoice.match_status if invoice else None,
        },
    )


def handle_damaged_grn_created(
    session: Session,
    *,
    po: PurchaseOrder,
    part: Part,
    grn: GoodsReceipt,
) -> ActionItem:
    """Place damaged GRN units directly into quarantine and raise Manager hold alert."""
    hold_in_quarantine(session, part.id, grn.received_qty)
    append_po_note(
        session,
        po,
        (
            f"GRN #{grn.id} marked '{grn.condition}': "
            f"{grn.received_qty} units held in quality quarantine."
        ),
    )
    return _build_damaged_goods_alert(
        session,
        po=po,
        part=part,
        grn=grn,
        summary=(
            f"GRN #{grn.id} for {po.po_number} ({part.sku}) logged {grn.received_qty} units "
            f"with condition '{grn.condition}'. Units are held in quarantine (excluded from "
            "usable on-hand stock) pending Manager inspection/disposition."
        ),
    )


def handle_invoice_match_outcome(
    session: Session,
    engine: "WorkflowEngine",
    *,
    po: PurchaseOrder,
    part: Part,
    supplier: Supplier,
    invoice: Invoice,
    match_result: ThreeWayMatchResult | None,
    thread_id: str | None = None,
    thread_subject: str | None = None,
    packing_list_note: str | None = None,
    packing_list_qty: int | None = None,
) -> dict[str, Any]:
    """Apply inventory stock movement directives, refresh supplier score, and create alerts."""
    if match_result is None:
        alert = create_action_item(
            session,
            type="INVOICE_PENDING_GRN",
            severity=AlertSeverity.INFO,
            part_criticality=part.criticality,
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

    if match_result.stock_action == "RELEASE_FROM_QUARANTINE":
        release_quarantined_stock(
            session,
            part.id,
            match_result.stock_qty,
            note=match_result.stock_note,
        )
    elif match_result.stock_action == "RELEASE_DIRECT":
        receive_stock_direct(
            session,
            part.id,
            match_result.stock_qty,
            note=match_result.stock_note,
        )
    elif match_result.stock_action == "HOLD_QUARANTINE":
        hold_in_quarantine(
            session,
            part.id,
            match_result.stock_qty,
        )

    refresh_supplier_metrics(session, supplier.id, part_id=part.id)

    grn = session.scalar(
        select(GoodsReceipt)
        .where(GoodsReceipt.po_id == po.id)
        .order_by(GoodsReceipt.id.desc())
        .limit(1)
    )

    if match_result.damaged_hold and grn is not None:
        summary = (
            f"Invoice {invoice.invoice_number} (v{invoice.version}) numbers match {po.po_number}, "
            f"but GRN #{grn.id} is marked {grn.condition} — {grn.received_qty} units remain "
            "quarantined until Manager inspection/disposition."
            if match_result.matched
            else (
                f"Invoice {invoice.invoice_number} failed 3-way match "
                f"({match_result.discrepancy_summary}) AND GRN #{grn.id} is marked "
                f"{grn.condition} — {grn.received_qty} units remain "
                "quarantined until Manager inspection/disposition."
            )
        )
        alert = _build_damaged_goods_alert(
            session,
            po=po,
            part=part,
            grn=grn,
            summary=summary,
            invoice=invoice,
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
        released_qty = grn.received_qty if grn is not None else invoice.extracted_qty
        pl_clause = f" Note: {packing_list_note}" if packing_list_note else ""
        alert = create_action_item(
            session,
            type="INVOICE_MATCHED_CLOSED",
            severity=AlertSeverity.MEDIUM if packing_list_note else AlertSeverity.INFO,
            part_criticality=part.criticality,
            title=f"3-way match passed & stock released: {po.po_number} ({part.sku})",
            summary=(
                f"Invoice {invoice.invoice_number} (v{invoice.version}) matched "
                f"PO {po.po_number} and GRN ({released_qty} units @ ₹{po.unit_price:,.2f}). "
                f"Released {released_qty} units to usable on-hand stock "
                f"(now {part.on_hand_stock} on hand, {part.quarantined_stock} quarantined) "
                f"and closed PO.{pl_clause}"
            ),
            po_id=po.id,
            part_id=part.id,
            suggested_actions=[],
            cta_payload={
                "po_id": po.id,
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "released_qty": released_qty,
                "packing_list_qty": packing_list_qty,
                "packing_list_conflict": bool(packing_list_note),
            },
        )
        return {"alert_id": alert.id, "draft_comm_id": None}

    grn_qty = grn.received_qty if grn is not None else po.qty
    price_diff = match_result.price_diff_per_unit
    total_price_impact = match_result.total_price_impact
    qty_diff = match_result.qty_diff

    discrepancy_text = match_result.discrepancy_summary
    if packing_list_note:
        discrepancy_text = f"{discrepancy_text}; {packing_list_note}"

    draft = engine._create_lex_draft(
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
            "discrepancy_summary": discrepancy_text,
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
            "packing_list_qty": packing_list_qty,
            "packing_list_conflict": bool(packing_list_note),
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
    pl_summary = f" {packing_list_note}" if packing_list_note else ""
    alert = create_action_item(
        session,
        type="INVOICE_MISMATCH_QUARANTINE",
        severity=severity,
        part_criticality=part.criticality,
        title=f"Stock Quarantined — Invoice Mismatch on {po.po_number} ({part.sku})",
        summary=(
            f"Invoice {invoice.invoice_number} (v{invoice.version}) from {supplier.name} "
            f"failed 3-way match ({match_result.match_status.value}): "
            f"{match_result.discrepancy_summary}.{pl_summary} {grn_qty} units are held in "
            "quarantine (excluded from on-hand stock). "
            "Lex drafted a correction request for approval."
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
            "discrepancy_summary": discrepancy_text,
            "price_diff_per_unit": price_diff,
            "total_price_impact": total_price_impact,
            "qty_diff": qty_diff,
            "packing_list_qty": packing_list_qty,
            "packing_list_conflict": bool(packing_list_note),
        },
    )
    return {"alert_id": alert.id, "draft_comm_id": draft.id}


def _resolve_target_po(
    matched_po: PurchaseOrder | None,
    extraction: InvoiceExtractionResult,
    po_by_number: dict[str, PurchaseOrder],
) -> tuple[PurchaseOrder | None, str | None]:
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
    return target_po, po_conflict_reason


def _compute_extracted_qty_and_price(extraction: InvoiceExtractionResult) -> tuple[int, float]:
    if len(extraction.line_items) == 1:
        return extraction.line_items[0].qty, extraction.line_items[0].unit_price
    extracted_qty = sum(li.qty for li in extraction.line_items)
    unique_prices = {round(li.unit_price, 4) for li in extraction.line_items}
    if len(unique_prices) == 1:
        return extracted_qty, extraction.line_items[0].unit_price
    if extracted_qty > 0:
        computed_lines_sum = sum(li.qty * li.unit_price for li in extraction.line_items)
        return extracted_qty, round(computed_lines_sum / extracted_qty, 2)
    return extracted_qty, 0.0


def process_pdf_invoice_email(
    session: Session,
    engine: "WorkflowEngine",
    *,
    email: StoredEmail,
    supplier: Supplier,
    matched_po: PurchaseOrder | None,
    po_by_number: dict[str, PurchaseOrder],
    triage_result: EmailTriageResult,
) -> dict[str, Any]:
    """Extract one or more PDF attachments, classify docs, and execute safe 3-way match."""
    all_paths = list(email.attachment_paths or [])
    if email.attachment_path and email.attachment_path not in all_paths:
        all_paths.insert(0, email.attachment_path)
    pdf_paths = [p for p in all_paths if p.lower().endswith(".pdf")]

    if not pdf_paths:
        non_pdf_note = (
            f" (Received non-PDF attachment(s): {', '.join(Path(p).name for p in all_paths)}.)"
            if all_paths
            else ""
        )
        alert = create_action_item(
            session,
            type="INVOICE_MISSING_PDF",
            severity=AlertSeverity.HIGH if all_paths else AlertSeverity.MEDIUM,
            title=(
                "Invoice email missing PDF attachment"
                + (f" ({matched_po.po_number})" if matched_po else "")
            ),
            summary=(
                f"{supplier.name} sent an invoice email without a PDF attachment.{non_pdf_note} "
                "No PO state was modified; request a PDF invoice before matching."
            ),
            po_id=matched_po.id if matched_po else None,
            part_id=matched_po.part_id if matched_po else None,
            suggested_actions=[{"action": "add_note", "label": "Log Review Note"}],
            cta_payload={
                "comm_id": email.id,
                "po_id": matched_po.id if matched_po else None,
                "attachments": all_paths,
            },
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

    extractions: list[tuple[Path, InvoiceExtractionResult]] = []
    for raw_path in pdf_paths:
        pdf_file = Path(raw_path)
        ext, err = _extract_pdf_safe(
            session,
            engine,
            pdf_file=pdf_file,
            supplier_name=supplier.name,
            comm_id=email.id,
            po_id=matched_po.id if matched_po else None,
        )
        if ext is None:
            return _escalate_unreadable_pdf(
                session,
                engine,
                email=email,
                supplier=supplier,
                matched_po=matched_po,
                triage_result=triage_result,
                filename=pdf_file.name,
                reason=err or "Unreadable PDF",
                attachment_path=raw_path,
            )
        extractions.append((pdf_file, ext))

    invoices = [(p, e) for p, e in extractions if e.document_type == PDFDocumentType.INVOICE]
    credit_notes = [
        (p, e) for p, e in extractions if e.document_type == PDFDocumentType.CREDIT_NOTE
    ]
    packing_lists = [
        (p, e) for p, e in extractions if e.document_type == PDFDocumentType.PACKING_LIST
    ]

    # Check multi-PO consolidated invoice (F-2.3 / SCN-28)
    for pdf_file, ext in extractions:
        if len(ext.referenced_po_numbers) > 1:
            po_list_str = ", ".join(ext.referenced_po_numbers)
            alert = create_action_item(
                session,
                type="INVOICE_EXTRACTION_REVIEW",
                severity=AlertSeverity.HIGH,
                title=f"Consolidated multi-PO invoice review ({supplier.name})",
                summary=(
                    f"Consolidated invoice '{pdf_file.name}' references multiple POs "
                    f"({po_list_str}). Single-PO 3-way match requires separate per-PO invoices; "
                    "zero PO state or stock changes were made."
                ),
                po_id=matched_po.id if matched_po else None,
                part_id=matched_po.part_id if matched_po else None,
                suggested_actions=[{"action": "add_note", "label": "Log Review Note"}],
                cta_payload={
                    "comm_id": email.id,
                    "attachment_path": str(pdf_file),
                    "invoice_number": ext.invoice_number,
                    "referenced_po_numbers": ext.referenced_po_numbers,
                    "confidence": ext.confidence,
                },
            )
            annotate_triage(
                session,
                email.id,
                classification=EmailIntent.AMBIGUOUS.value,
                extracted_json={
                    "triage": triage_result.model_dump(mode="json"),
                    "invoice_extraction": ext.model_dump(mode="json"),
                },
                confidence=ext.confidence,
                po_id=matched_po.id if matched_po else email.po_id,
            )
            session.flush()
            return {
                "triage": triage_result,
                "matched_po_id": matched_po.id if matched_po else None,
                "alert_id": alert.id,
                "draft_comm_id": None,
                "invoice_extraction": ext,
                "invoice_id": None,
                "match_result": None,
            }

    # Standalone Credit Note (no Invoice attached) -> never run 3-way price match as billed invoice
    if not invoices and credit_notes:
        cn_path, cn_ext = credit_notes[0]
        target_po, _ = _resolve_target_po(matched_po, cn_ext, po_by_number)
        cn_amt = -abs(cn_ext.total_amount if cn_ext.total_amount != 0 else cn_ext.subtotal)
        alert = create_action_item(
            session,
            type="INVOICE_EXTRACTION_REVIEW",
            severity=AlertSeverity.HIGH,
            title=f"Credit Note {cn_ext.invoice_number} received without invoice",
            summary=(
                f"{supplier.name} sent standalone Credit Note {cn_ext.invoice_number} "
                f"(-₹{abs(cn_amt):,.2f}) without a paired Tax Invoice. "
                "Review and apply manually or request the paired Tax Invoice."
            ),
            po_id=target_po.id if target_po else None,
            part_id=target_po.part_id if target_po else None,
            suggested_actions=[{"action": "add_note", "label": "Log Review Note"}],
            cta_payload={
                "comm_id": email.id,
                "attachment_path": str(cn_path),
                "credit_note_number": cn_ext.invoice_number,
                "credit_note_amount": cn_amt,
                "po_id": target_po.id if target_po else None,
            },
        )
        session.flush()
        return {
            "triage": triage_result,
            "matched_po_id": target_po.id if target_po else None,
            "alert_id": alert.id,
            "draft_comm_id": None,
            "invoice_extraction": cn_ext,
            "invoice_id": None,
            "match_result": None,
        }

    primary_path, extraction = invoices[0] if invoices else extractions[0]
    target_po, po_conflict_reason = _resolve_target_po(matched_po, extraction, po_by_number)

    annotate_triage(
        session,
        email.id,
        classification=EmailIntent.INVOICE_SUBMISSION.value
        if extraction.confidence >= CONFIDENCE_THRESHOLD and target_po is not None
        else EmailIntent.AMBIGUOUS.value,
        extracted_json={
            "triage": triage_result.model_dump(mode="json"),
            "invoice_extraction": extraction.model_dump(mode="json"),
            "all_extractions": [e.model_dump(mode="json") for _, e in extractions],
        },
        confidence=min(triage_result.confidence, extraction.confidence)
        if matched_po is not None
        else extraction.confidence,
        po_id=target_po.id if target_po else email.po_id,
    )

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
                f"Lex could not safely auto-match PDF '{primary_path.name}': "
                f"{'; '.join(escalation_reasons)}. Zero PO state changes were made."
            ),
            po_id=target_po.id if target_po else None,
            part_id=target_po.part_id if target_po else None,
            suggested_actions=[{"action": "add_note", "label": "Log Review Note"}],
            cta_payload={
                "comm_id": email.id,
                "attachment_path": str(primary_path),
                "invoice_number": extraction.invoice_number,
                "pdf_po_number": extraction.po_number,
                "referenced_po_numbers": extraction.referenced_po_numbers,
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
    extracted_qty, extracted_unit_price = _compute_extracted_qty_and_price(extraction)

    # Invoice + Credit Note paired settlement review (F-3.1 / SCN-30)
    if invoices and credit_notes:
        _, cn_ext = credit_notes[0]
        inv_amount = round(extraction.subtotal, 2)
        cn_raw = cn_ext.total_amount if cn_ext.total_amount != 0 else cn_ext.subtotal
        cn_amount = -round(abs(cn_raw), 2)
        net_amount = round(inv_amount + cn_amount, 2)
        po_total = round(target_po.qty * target_po.unit_price, 2)
        net_matches_po = (
            abs(net_amount - po_total) <= LINE_TOTAL_TOLERANCE_INR
            and extracted_qty == target_po.qty
        )

        suggested = []
        if net_matches_po:
            suggested.append(
                {
                    "action": "approve_net_settlement",
                    "label": "Approve Net Settlement & Release Stock",
                }
            )
        suggested.append({"action": "add_note", "label": "Log Review Note"})

        alert = create_action_item(
            session,
            type="INVOICE_WITH_CREDIT_NOTE_REVIEW",
            severity=AlertSeverity.MEDIUM if net_matches_po else AlertSeverity.HIGH,
            part_criticality=part.criticality,
            title=(
                f"Invoice + Credit Note net settlement review: "
                f"{target_po.po_number} ({part.sku})"
            ),
            summary=(
                f"Invoice {extraction.invoice_number}: ₹{inv_amount:,.2f} + "
                f"Credit Note {cn_ext.invoice_number}: -₹{abs(cn_amount):,.2f} = "
                f"Net ₹{net_amount:,.2f} vs PO {target_po.po_number} ₹{po_total:,.2f}. "
                + (
                    "Net settlement matches PO total; approve to release stock and close PO."
                    if net_matches_po
                    else "Net amount still differs from PO total; manual review required."
                )
            ),
            po_id=target_po.id,
            part_id=part.id,
            suggested_actions=suggested,
            cta_payload={
                "po_id": target_po.id,
                "comm_id": email.id,
                "pdf_path": str(primary_path),
                "invoice_number": extraction.invoice_number,
                "credit_note_number": cn_ext.invoice_number,
                "invoice_amount": inv_amount,
                "credit_note_amount": cn_amount,
                "net_amount": net_amount,
                "po_total": po_total,
                "extracted_qty": extracted_qty,
                "net_matches_po": net_matches_po,
            },
        )
        session.flush()
        return {
            "triage": triage_result,
            "matched_po_id": target_po.id,
            "alert_id": alert.id,
            "draft_comm_id": None,
            "invoice_extraction": extraction,
            "invoice_id": None,
            "match_result": None,
        }

    # Packing List cross-check (F-3.1 / SCN-44)
    packing_list_note: str | None = None
    packing_list_qty: int | None = None
    if packing_lists:
        _, pl_ext = packing_lists[0]
        packing_list_qty = sum(li.qty for li in pl_ext.line_items)
        if packing_list_qty != extracted_qty:
            packing_list_note = (
                f"Packing List ({pl_ext.invoice_number or 'PL'}) states {packing_list_qty} shipped "
                f"pcs vs Invoice ({extraction.invoice_number}) billing {extracted_qty} pcs."
            )

    invoice, match_result = submit_invoice_and_match(
        session,
        po_id=target_po.id,
        invoice_number=extraction.invoice_number,
        extracted_qty=extracted_qty,
        extracted_unit_price=extracted_unit_price,
        extracted_total=extraction.subtotal,
        pdf_path=str(primary_path),
        comm_id=email.id,
    )
    outcome = handle_invoice_match_outcome(
        session,
        engine,
        po=target_po,
        part=part,
        supplier=supplier,
        invoice=invoice,
        match_result=match_result,
        thread_id=email.thread_id,
        thread_subject=email.subject,
        packing_list_note=packing_list_note,
        packing_list_qty=packing_list_qty,
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


def execute_damaged_goods_cta(
    session: Session,
    engine: "WorkflowEngine",
    item: ActionItem,
    *,
    action: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    cta = item.cta_payload_json or {}
    po_id = int(params.get("po_id") or cta.get("po_id") or item.po_id or 0)
    if not po_id:
        raise ValueError("No po_id linked to damaged goods alert")
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise ValueError(f"PurchaseOrder {po_id} not found")
    part = session.get(Part, po.part_id)
    supplier = session.get(Supplier, po.supplier_id)
    assert part is not None and supplier is not None

    if action == "release_damaged_goods":
        held_qty = int(cta.get("held_qty") or po.qty)
        raw_good = params.get("good_qty")
        good_qty = held_qty if raw_good is None else int(raw_good)
        disposition = apply_damaged_goods_disposition(
            session,
            po_id=po.id,
            good_qty=good_qty,
            note=str(params.get("note") or ""),
        )
        damaged_qty = int(disposition["damaged_qty"])
        if good_qty > 0:
            release_quarantined_stock(
                session,
                part.id,
                good_qty,
                note=f"Released {good_qty} inspected usable units from {po.po_number}",
            )
        if damaged_qty > 0:
            clear_quarantined_stock(session, part.id, damaged_qty)

        resolve_action_item(session, item.id)
        session.flush()
        return {
            **disposition,
            "on_hand_stock": part.on_hand_stock,
            "quarantined_stock": part.quarantined_stock,
        }

    if action == "draft_damage_claim":
        held_qty = int(cta.get("held_qty") or po.qty)
        draft = _draft_supplier_general_email(
            session,
            engine,
            supplier=supplier,
            po=po,
            part=part,
            thread_id=None,
            thread_subject=f"[{po.po_number}] Damaged Shipment Claim & Replacement Request",
            required_cc_personas=[
                PersonaKey.INVENTORY_MANAGER.value,
                PersonaKey.PROCUREMENT.value,
            ],
            context_facts={
                "purpose": "damage_claim",
                "held_qty": held_qty,
                "grn_condition": cta.get("grn_condition", "DAMAGED"),
                "claim_note": str(params.get("note") or ""),
                "requested_action": (
                    f"Goods received for {po.po_number} ({held_qty} units of {part.sku}) "
                    "arrived damaged and are held in quarantine. Please arrange replacement "
                    "units or issue a credit adjustment."
                ),
            },
        )
        claim_alert = create_action_item(
            session,
            type="DAMAGE_CLAIM_DRAFT",
            severity=AlertSeverity.HIGH,
            part_criticality=part.criticality,
            title=f"Approve damage claim email for {po.po_number} ({supplier.name})",
            summary=(
                f"Lex drafted a damage claim / replacement request to {supplier.name} "
                f"(CC: Inventory Manager & Procurement) for {po.po_number}."
            ),
            po_id=po.id,
            part_id=part.id,
            draft_comm_id=draft.id,
            suggested_actions=[
                {"action": "approve_draft", "label": "Approve & Send Damage Claim"},
                {"action": "reject_draft", "label": "Reject Draft"},
            ],
            cta_payload={"draft_comm_id": draft.id, "po_id": po.id},
        )
        session.flush()
        return {
            "po_id": po.id,
            "draft_comm_id": draft.id,
            "alert_id": claim_alert.id,
        }

    raise ValueError(f"Unsupported damaged goods CTA action: '{action}'")


def execute_net_settlement_cta(
    session: Session,
    engine: "WorkflowEngine",
    item: ActionItem,
    *,
    action: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    if action != "approve_net_settlement":
        raise ValueError(f"Unsupported net settlement CTA action: '{action}'")
    cta = item.cta_payload_json or {}
    po_id = int(params.get("po_id") or cta.get("po_id") or item.po_id or 0)
    if not po_id:
        raise ValueError("No po_id linked to net settlement alert")
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise ValueError(f"PurchaseOrder {po_id} not found")
    part = session.get(Part, po.part_id)
    supplier = session.get(Supplier, po.supplier_id)
    assert part is not None and supplier is not None

    inv, match_res = record_net_settlement_invoice(
        session,
        po_id=po.id,
        invoice_number=str(cta.get("invoice_number") or "INV"),
        credit_note_number=str(cta.get("credit_note_number") or "CN"),
        invoice_subtotal=float(cta.get("invoice_amount") or 0.0),
        credit_note_amount=float(cta.get("credit_note_amount") or 0.0),
        net_amount=float(cta.get("net_amount") or 0.0),
        extracted_qty=int(cta.get("extracted_qty") or po.qty),
        pdf_path=str(cta.get("pdf_path") or ""),
        comm_id=int(cta["comm_id"]) if cta.get("comm_id") else None,
    )
    outcome = handle_invoice_match_outcome(
        session,
        engine,
        po=po,
        part=part,
        supplier=supplier,
        invoice=inv,
        match_result=match_res,
    )
    resolve_action_item(session, item.id)
    session.flush()
    return {
        "po_id": po.id,
        "po_status": po.status,
        "invoice_id": inv.id,
        "match_status": inv.match_status,
        "alert_id": outcome["alert_id"],
    }
