from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import GRNStatus, InvoiceMatchStatus, POStatus
from backend.core.models import (
    GoodsReceipt,
    Invoice,
    PartSupplierTerm,
    PurchaseOrder,
)
from backend.po.matching import ThreeWayMatchInput, ThreeWayMatchResult, evaluate_three_way_match
from backend.po.state_machine import append_po_note, transition_po


class PONotFoundError(ValueError):
    pass


class BackupSupplierNotFoundError(ValueError):
    pass


def _get_po_or_raise(session: Session, po_id: int) -> PurchaseOrder:
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise PONotFoundError(f"PurchaseOrder {po_id} not found")
    return po


def generate_po_number(session: Session) -> str:
    existing = session.scalars(select(PurchaseOrder.po_number)).all()
    max_num = 1000
    for code in existing:
        if code.startswith("PO-"):
            suffix = code.removeprefix("PO-")
            if suffix.isdigit():
                max_num = max(max_num, int(suffix))
    return f"PO-{max_num + 1}"


def create_po(
    session: Session,
    *,
    part_id: int,
    supplier_id: int,
    qty: int,
    unit_price: float,
    notes: str = "",
    initial_status: POStatus = POStatus.PENDING_DISPATCH_APPROVAL,
    po_number: str | None = None,
) -> PurchaseOrder:
    if qty <= 0:
        raise ValueError("PO quantity must be positive")
    if unit_price < 0:
        raise ValueError("PO unit_price cannot be negative")

    po = PurchaseOrder(
        po_number=po_number or generate_po_number(session),
        part_id=part_id,
        supplier_id=supplier_id,
        qty=qty,
        unit_price=round(unit_price, 2),
        status=initial_status.value,
        notes=notes,
        created_at=clock.now(session),
    )
    session.add(po)
    session.flush()
    return po


def get_po(session: Session, po_id: int) -> PurchaseOrder:
    return _get_po_or_raise(session, po_id)


def get_po_by_number(session: Session, po_number: str) -> PurchaseOrder | None:
    return session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.po_number == po_number)
    )


def list_pos(
    session: Session,
    *,
    status: POStatus | str | None = None,
    supplier_id: int | None = None,
    part_id: int | None = None,
) -> list[PurchaseOrder]:
    stmt = select(PurchaseOrder)
    if status is not None:
        stmt = stmt.where(PurchaseOrder.status == POStatus(status).value)
    if supplier_id is not None:
        stmt = stmt.where(PurchaseOrder.supplier_id == supplier_id)
    if part_id is not None:
        stmt = stmt.where(PurchaseOrder.part_id == part_id)
    stmt = stmt.order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc())
    return list(session.scalars(stmt).all())


def update_po_terms(
    session: Session,
    po_id: int,
    *,
    qty: int | None = None,
    unit_price: float | None = None,
    revised_eta: datetime | None = None,
    note: str | None = None,
) -> PurchaseOrder:
    po = _get_po_or_raise(session, po_id)
    if po.status in (POStatus.CLOSED.value, POStatus.CANCELLED.value):
        raise ValueError(f"Cannot update terms on {po.status} PO {po.po_number}")
    if qty is not None:
        if qty <= 0:
            raise ValueError("PO quantity must be positive")
        po.qty = qty
    if unit_price is not None:
        if unit_price < 0:
            raise ValueError("PO unit_price cannot be negative")
        po.unit_price = round(unit_price, 2)
    if revised_eta is not None:
        po.revised_eta = clock.ensure_utc(revised_eta)
    if note:
        append_po_note(session, po, note)
    session.flush()
    return po


def cancel_po(
    session: Session,
    po_id: int,
    *,
    note: str | None = None,
) -> PurchaseOrder:
    po = _get_po_or_raise(session, po_id)
    cancel_note = note or f"PO {po.po_number} cancelled by Inventory Manager."
    return transition_po(session, po, POStatus.CANCELLED, note=cancel_note)


def is_grn_damaged(condition: str | None) -> bool:
    """Treat any GRN condition other than GOOD/OK as a physical quality hold."""
    cleaned = (condition or "GOOD").strip().upper()
    return cleaned not in ("GOOD", "OK")


def _apply_match_outcome(
    session: Session,
    po: PurchaseOrder,
    grn: GoodsReceipt,
    invoice: Invoice,
    result: ThreeWayMatchResult,
    *,
    grn_already_quarantined: bool = False,
) -> None:
    invoice.match_status = result.match_status.value
    invoice.discrepancy_summary = result.discrepancy_summary
    damaged = is_grn_damaged(grn.condition)

    if result.matched and not damaged:
        was_quarantined = po.status == POStatus.QUARANTINED_INVOICE_MISMATCH.value
        grn.status = GRNStatus.RELEASED.value
        session.flush()
        cumulative_released = int(
            session.scalar(
                select(func.coalesce(func.sum(GoodsReceipt.received_qty), 0)).where(
                    GoodsReceipt.po_id == po.id,
                    GoodsReceipt.status == GRNStatus.RELEASED.value,
                )
            )
            or 0
        )
        result.stock_action = (
            "RELEASE_FROM_QUARANTINE" if was_quarantined else "RELEASE_DIRECT"
        )
        result.stock_qty = grn.received_qty
        result.stock_note = (
            f"Released {po.po_number} via matched invoice {invoice.invoice_number}"
        )
        if cumulative_released >= po.qty:
            transition_po(
                session,
                po,
                POStatus.CLOSED,
                note=(
                    f"3-way match passed (Invoice {invoice.invoice_number} v{invoice.version}); "
                    f"cumulative released {cumulative_released}/{po.qty} complete."
                ),
            )
        else:
            transition_po(
                session,
                po,
                POStatus.ACKNOWLEDGED,
                note=(
                    f"Partial 3-way match passed (Invoice {invoice.invoice_number} "
                    f"v{invoice.version}); cumulative released {cumulative_released}/{po.qty}."
                ),
            )
    elif result.matched and damaged:
        # Invoice numbers are clean, but physical goods failed quality inspection -> keep held!
        result.damaged_hold = True
        grn.status = GRNStatus.HELD_QUARANTINE.value
        invoice.discrepancy_summary = (
            f"Invoice matches PO, but GRN #{grn.id} is marked '{grn.condition}' — "
            "units remain quarantined until Manager inspection/disposition."
        )
        result.discrepancy_summary = invoice.discrepancy_summary
        if po.status == POStatus.RECEIVED_PENDING_INVOICE.value:
            result.stock_action = "NONE" if grn_already_quarantined else "HOLD_QUARANTINE"
            result.stock_qty = 0 if grn_already_quarantined else grn.received_qty
            result.stock_note = (
                f"Quality hold on {po.po_number}: GRN condition={grn.condition}"
            )
            transition_po(
                session,
                po,
                POStatus.QUARANTINED_INVOICE_MISMATCH,
                note=(
                    f"Invoice {invoice.invoice_number} v{invoice.version} matched numerically, "
                    f"but GRN #{grn.id} is '{grn.condition}'; held in quality quarantine."
                ),
            )
        else:
            result.stock_action = "NONE"
            result.stock_qty = 0
            append_po_note(
                session,
                po,
                f"Invoice {invoice.invoice_number} v{invoice.version} matched numerically, "
                f"but PO remains quarantined due to GRN condition '{grn.condition}'.",
            )
    else:
        if damaged:
            result.damaged_hold = True
        grn.status = GRNStatus.HELD_QUARANTINE.value
        if po.status == POStatus.RECEIVED_PENDING_INVOICE.value:
            # Emit HOLD_QUARANTINE once on the first mismatch unless already quarantined by GRN.
            result.stock_action = "NONE" if grn_already_quarantined else "HOLD_QUARANTINE"
            result.stock_qty = 0 if grn_already_quarantined else grn.received_qty
            result.stock_note = (
                f"Quarantined {po.po_number} due to invoice {invoice.invoice_number} mismatch"
            )
            transition_po(
                session,
                po,
                POStatus.QUARANTINED_INVOICE_MISMATCH,
                note=(
                    f"Invoice {invoice.invoice_number} v{invoice.version} failed 3-way match: "
                    f"{result.discrepancy_summary}"
                ),
            )
        else:
            result.stock_action = "NONE"
            result.stock_qty = 0
            append_po_note(
                session,
                po,
                f"Invoice {invoice.invoice_number} v{invoice.version} still mismatched: "
                f"{result.discrepancy_summary}",
            )
    session.flush()


def record_goods_receipt(
    session: Session,
    *,
    po_id: int,
    received_qty: int,
    condition: str = "GOOD",
) -> tuple[GoodsReceipt, ThreeWayMatchResult | None]:
    if received_qty <= 0:
        raise ValueError("GRN received_qty must be positive")

    po = _get_po_or_raise(session, po_id)
    transition_po(
        session,
        po,
        POStatus.RECEIVED_PENDING_INVOICE,
        note=f"GRN logged for {received_qty} units ({condition}).",
    )

    grn = GoodsReceipt(
        po_id=po.id,
        received_qty=received_qty,
        received_at=clock.now(session),
        condition=condition,
        status=GRNStatus.HELD_QUARANTINE.value,
    )
    session.add(grn)
    session.flush()

    # If an invoice arrived before physical receipt, reconcile immediately.
    pending_invoice = session.scalar(
        select(Invoice)
        .where(
            Invoice.po_id == po.id,
            Invoice.match_status == InvoiceMatchStatus.PENDING_GRN.value,
        )
        .order_by(Invoice.version.desc())
        .limit(1)
    )
    if pending_invoice is not None:
        result = evaluate_three_way_match(
            ThreeWayMatchInput(
                po_qty=po.qty,
                po_unit_price=po.unit_price,
                grn_received_qty=grn.received_qty,
                invoice_qty=pending_invoice.extracted_qty,
                invoice_unit_price=pending_invoice.extracted_unit_price,
                invoice_line_subtotal=pending_invoice.extracted_total,
            )
        )
        _apply_match_outcome(session, po, grn, pending_invoice, result)
        return grn, result

    return grn, None


def submit_invoice_and_match(
    session: Session,
    *,
    po_id: int,
    invoice_number: str,
    extracted_qty: int,
    extracted_unit_price: float,
    extracted_total: float | None = None,
    pdf_path: str = "",
    comm_id: int | None = None,
) -> tuple[Invoice, ThreeWayMatchResult | None]:
    po = _get_po_or_raise(session, po_id)
    if po.status in (POStatus.CLOSED.value, POStatus.CANCELLED.value):
        raise ValueError(f"Cannot submit invoice against {po.status} PO {po.po_number}")

    max_version = session.scalar(
        select(func.coalesce(func.max(Invoice.version), 0)).where(Invoice.po_id == po.id)
    )
    next_version = int(max_version or 0) + 1
    line_total = (
        round(extracted_total, 2)
        if extracted_total is not None
        else round(extracted_qty * extracted_unit_price, 2)
    )

    latest_grn = session.scalar(
        select(GoodsReceipt)
        .where(GoodsReceipt.po_id == po.id)
        .order_by(GoodsReceipt.id.desc())
        .limit(1)
    )

    invoice = Invoice(
        po_id=po.id,
        comm_id=comm_id,
        invoice_number=invoice_number,
        pdf_path=pdf_path,
        extracted_qty=extracted_qty,
        extracted_unit_price=round(extracted_unit_price, 2),
        extracted_total=line_total,
        match_status=InvoiceMatchStatus.PENDING_GRN.value,
        discrepancy_summary="Awaiting physical Goods Receipt (GRN) for 3-way match.",
        version=next_version,
        received_at=clock.now(session),
    )
    session.add(invoice)
    session.flush()

    if latest_grn is None:
        append_po_note(
            session,
            po,
            f"Invoice {invoice_number} v{next_version} received prior to GRN; held for matching.",
        )
        session.flush()
        return invoice, None

    result = evaluate_three_way_match(
        ThreeWayMatchInput(
            po_qty=po.qty,
            po_unit_price=po.unit_price,
            grn_received_qty=latest_grn.received_qty,
            invoice_qty=extracted_qty,
            invoice_unit_price=extracted_unit_price,
            invoice_line_subtotal=extracted_total,
        )
    )
    _apply_match_outcome(
        session,
        po,
        latest_grn,
        invoice,
        result,
        grn_already_quarantined=is_grn_damaged(latest_grn.condition),
    )
    return invoice, result


def apply_damaged_goods_disposition(
    session: Session,
    *,
    po_id: int,
    good_qty: int,
    note: str = "",
) -> dict[str, int | str]:
    """Update GRN/PO state after Manager inspects a DAMAGED GRN in quarantine."""
    po = _get_po_or_raise(session, po_id)
    grn = session.scalar(
        select(GoodsReceipt)
        .where(
            GoodsReceipt.po_id == po.id,
            GoodsReceipt.status == GRNStatus.HELD_QUARANTINE.value,
        )
        .order_by(GoodsReceipt.id.desc())
        .limit(1)
    )
    if grn is None:
        raise ValueError(f"No held GRN found for PO {po.po_number}")
    if good_qty < 0 or good_qty > grn.received_qty:
        raise ValueError(
            f"good_qty ({good_qty}) must be between 0 and held GRN qty ({grn.received_qty})"
        )

    held_qty = grn.received_qty
    damaged_qty = held_qty - good_qty
    if good_qty > 0:
        grn.status = GRNStatus.RELEASED.value
        grn.received_qty = good_qty
        grn.condition = (
            "INSPECTED_GOOD"
            if damaged_qty == 0
            else f"PARTIAL_GOOD ({good_qty}/{held_qty})"
        )
    else:
        grn.condition = f"SCRAPPED_DAMAGED ({held_qty})"

    latest_inv = session.scalar(
        select(Invoice)
        .where(Invoice.po_id == po.id)
        .order_by(Invoice.version.desc(), Invoice.id.desc())
        .limit(1)
    )
    if (
        good_qty == held_qty
        and latest_inv is not None
        and latest_inv.match_status == InvoiceMatchStatus.MATCHED.value
        and po.status in (
            POStatus.RECEIVED_PENDING_INVOICE.value,
            POStatus.QUARANTINED_INVOICE_MISMATCH.value,
        )
    ):
        session.flush()
        cumulative_released = int(
            session.scalar(
                select(func.coalesce(func.sum(GoodsReceipt.received_qty), 0)).where(
                    GoodsReceipt.po_id == po.id,
                    GoodsReceipt.status == GRNStatus.RELEASED.value,
                )
            )
            or 0
        )
        target_status = (
            POStatus.CLOSED if cumulative_released >= po.qty else POStatus.ACKNOWLEDGED
        )
        transition_po(
            session,
            po,
            target_status,
            note=note
            or (
                f"Manager cleared {good_qty}/{held_qty} units from damaged quarantine "
                f"({cumulative_released}/{po.qty} released)."
            ),
        )
    else:
        append_po_note(
            session,
            po,
            note
            or (
                f"Damaged GRN disposition: released {good_qty} usable units, "
                f"rejected/cleared {damaged_qty} damaged units."
            ),
        )
    session.flush()
    return {
        "po_id": po.id,
        "po_number": po.po_number,
        "po_status": po.status,
        "held_qty": held_qty,
        "good_qty": good_qty,
        "damaged_qty": damaged_qty,
    }


def record_net_settlement_invoice(
    session: Session,
    *,
    po_id: int,
    invoice_number: str,
    credit_note_number: str,
    invoice_subtotal: float,
    credit_note_amount: float,
    net_amount: float,
    extracted_qty: int,
    pdf_path: str = "",
    comm_id: int | None = None,
) -> tuple[Invoice, ThreeWayMatchResult | None]:
    """Record a Manager-approved Invoice + Credit Note net settlement and evaluate 3-way match."""
    effective_unit_price = (
        round(net_amount / extracted_qty, 2) if extracted_qty > 0 else round(net_amount, 2)
    )
    invoice, match_result = submit_invoice_and_match(
        session,
        po_id=po_id,
        invoice_number=f"{invoice_number} + {credit_note_number}",
        extracted_qty=extracted_qty,
        extracted_unit_price=effective_unit_price,
        extracted_total=round(net_amount, 2),
        pdf_path=pdf_path,
        comm_id=comm_id,
    )
    if match_result is not None and match_result.matched and not match_result.damaged_hold:
        invoice.discrepancy_summary = (
            f"Net settlement approved: Invoice {invoice_number} (₹{invoice_subtotal:,.2f}) "
            f"+ Credit Note {credit_note_number} (-₹{abs(credit_note_amount):,.2f}) "
            f"= ₹{net_amount:,.2f}."
        )
        session.flush()
    return invoice, match_result


def switch_po_to_backup_supplier(
    session: Session,
    po_id: int,
    *,
    backup_supplier_id: int | None = None,
    note: str = "",
) -> tuple[PurchaseOrder, PurchaseOrder]:
    original = _get_po_or_raise(session, po_id)

    if backup_supplier_id is not None:
        term = session.scalar(
            select(PartSupplierTerm).where(
                PartSupplierTerm.part_id == original.part_id,
                PartSupplierTerm.supplier_id == backup_supplier_id,
            )
        )
    else:
        term = session.scalar(
            select(PartSupplierTerm)
            .where(
                PartSupplierTerm.part_id == original.part_id,
                PartSupplierTerm.supplier_id != original.supplier_id,
            )
            .order_by(PartSupplierTerm.supplier_rank.asc())
            .limit(1)
        )

    if term is None:
        raise BackupSupplierNotFoundError(
            f"No backup supplier configured for part_id={original.part_id}"
        )

    cancel_note = note or f"Cancelled and reassigned to backup supplier_id={term.supplier_id}."
    transition_po(session, original, POStatus.CANCELLED, note=cancel_note)

    replacement_qty = max(original.qty, term.moq)
    replacement = create_po(
        session,
        part_id=original.part_id,
        supplier_id=term.supplier_id,
        qty=replacement_qty,
        unit_price=term.agreed_unit_price,
        notes=f"Replacement PO for cancelled {original.po_number}.",
        initial_status=POStatus.PENDING_DISPATCH_APPROVAL,
    )
    return original, replacement


def escalate_po_to_procurement(
    session: Session,
    po_id: int,
    *,
    note: str = "",
) -> PurchaseOrder:
    """Record a Procurement escalation timestamp and audit note on an active PO."""
    po = _get_po_or_raise(session, po_id)
    if po.status in (POStatus.CLOSED.value, POStatus.CANCELLED.value):
        raise ValueError(f"Cannot escalate {po.status} PO {po.po_number} to Procurement")

    sim_now = clock.now(session)
    cleaned_note = note.strip() or f"Escalated {po.po_number} to Procurement for follow-up."
    po.escalated_to_procurement_at = sim_now
    po.procurement_escalation_note = cleaned_note
    append_po_note(session, po, f"Escalated to Procurement: {cleaned_note}")
    session.flush()
    return po
