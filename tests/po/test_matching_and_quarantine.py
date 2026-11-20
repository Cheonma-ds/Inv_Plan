from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import (
    ActionItemStatus,
    AlertSeverity,
    Criticality,
    GRNStatus,
    InventoryTxType,
    InvoiceMatchStatus,
    PersonaKey,
    POStatus,
)
from backend.core.models import InventoryTransaction, Part, PartSupplierTerm, Supplier
from backend.inventory import InsufficientStockError, consume_stock, get_on_order_stock
from backend.po import (
    ThreeWayMatchInput,
    create_action_item,
    create_po,
    dismiss_action_item,
    evaluate_three_way_match,
    list_action_items,
    record_goods_receipt,
    resolve_action_item,
    submit_invoice_and_match,
    switch_po_to_backup_supplier,
    transition_po,
)


def _setup_part_with_backup(session: Session) -> tuple[Part, Supplier, Supplier]:
    sup1 = Supplier(persona_key=PersonaKey.SUP1.value, name="Apex", email="a@apex.in")
    sup2 = Supplier(persona_key=PersonaKey.SUP2.value, name="Bharat", email="b@bharat.in")
    part = Part(
        sku="MAT-100",
        name="Hex Bolt Pack",
        criticality=Criticality.STANDARD.value,
        on_hand_stock=10,
        quarantined_stock=0,
        rop=20,
        replenishment_point=120,
    )
    session.add_all([sup1, sup2, part])
    session.flush()
    session.add_all(
        [
            PartSupplierTerm(
                part_id=part.id,
                supplier_id=sup2.id,
                supplier_rank=1,
                agreed_unit_price=100.0,
                quoted_lead_time_days=15,
                effective_lead_time_days=23.0,
                moq=50,
                last_validated_at=clock.now(session),
            ),
            PartSupplierTerm(
                part_id=part.id,
                supplier_id=sup1.id,
                supplier_rank=2,
                agreed_unit_price=108.0,
                quoted_lead_time_days=10,
                effective_lead_time_days=10.0,
                moq=50,
                last_validated_at=clock.now(session),
            ),
        ]
    )
    session.flush()
    return part, sup2, sup1


def test_pure_three_way_match_rules_and_tolerance() -> None:
    # Exact match
    clean = evaluate_three_way_match(
        ThreeWayMatchInput(
            po_qty=100,
            po_unit_price=100.0,
            grn_received_qty=100,
            invoice_qty=100,
            invoice_unit_price=100.0,
            invoice_line_subtotal=10000.0,
        )
    )
    assert clean.matched is True
    assert clean.match_status == InvoiceMatchStatus.MATCHED

    # Within ₹0.50 rounding tolerance -> passes
    within_tol = evaluate_three_way_match(
        ThreeWayMatchInput(
            po_qty=100,
            po_unit_price=100.0,
            grn_received_qty=100,
            invoice_qty=100,
            invoice_unit_price=100.50,
            invoice_line_subtotal=10050.0,
        )
    )
    assert within_tol.matched is True

    # Exceeds ₹0.50 tolerance -> fails price mismatch
    over_tol = evaluate_three_way_match(
        ThreeWayMatchInput(
            po_qty=100,
            po_unit_price=100.0,
            grn_received_qty=100,
            invoice_qty=100,
            invoice_unit_price=100.51,
            invoice_line_subtotal=10051.0,
        )
    )
    assert over_tol.matched is False
    assert over_tol.match_status == InvoiceMatchStatus.MISMATCH_PRICE

    # Manager example 1: ₹150 vs ₹100 for 100 units
    price_bad = evaluate_three_way_match(
        ThreeWayMatchInput(
            po_qty=100,
            po_unit_price=100.0,
            grn_received_qty=100,
            invoice_qty=100,
            invoice_unit_price=150.0,
            invoice_line_subtotal=15000.0,
        )
    )
    assert price_bad.matched is False
    assert price_bad.match_status == InvoiceMatchStatus.MISMATCH_PRICE
    assert price_bad.price_diff_per_unit == 50.0
    assert price_bad.total_price_impact == 5000.0

    # Manager example 2: 90 units vs 100 units at ₹100
    qty_bad = evaluate_three_way_match(
        ThreeWayMatchInput(
            po_qty=100,
            po_unit_price=100.0,
            grn_received_qty=100,
            invoice_qty=90,
            invoice_unit_price=100.0,
            invoice_line_subtotal=9000.0,
        )
    )
    assert qty_bad.matched is False
    assert qty_bad.match_status == InvoiceMatchStatus.MISMATCH_QTY
    assert qty_bad.qty_diff == -10

    # Both price and qty bad
    both_bad = evaluate_three_way_match(
        ThreeWayMatchInput(
            po_qty=100,
            po_unit_price=100.0,
            grn_received_qty=100,
            invoice_qty=90,
            invoice_unit_price=150.0,
            invoice_line_subtotal=13500.0,
        )
    )
    assert both_bad.matched is False
    assert both_bad.match_status == InvoiceMatchStatus.MISMATCH_BOTH

    # Line total arithmetic error
    math_bad = evaluate_three_way_match(
        ThreeWayMatchInput(
            po_qty=100,
            po_unit_price=100.0,
            grn_received_qty=100,
            invoice_qty=100,
            invoice_unit_price=100.0,
            invoice_line_subtotal=10500.0,
        )
    )
    assert math_bad.matched is False
    assert math_bad.math_ok is False


def test_quarantine_lock_no_double_count_and_clean_release(db_session: Session) -> None:
    part, primary, _ = _setup_part_with_backup(db_session)
    po = create_po(db_session, part_id=part.id, supplier_id=primary.id, qty=100, unit_price=100.0)
    transition_po(db_session, po, POStatus.SENT_AWAITING_ACK)
    transition_po(
        db_session,
        po,
        POStatus.ACKNOWLEDGED,
        promised_eta=clock.now(db_session) + timedelta(days=15),
    )

    grn, initial_match = record_goods_receipt(db_session, po_id=po.id, received_qty=100)
    assert initial_match is None
    assert po.status == POStatus.RECEIVED_PENDING_INVOICE.value
    assert grn.status == GRNStatus.HELD_QUARANTINE.value
    assert part.on_hand_stock == 10  # Not usable yet!
    assert get_on_order_stock(db_session, part.id) == 100

    # Invoice v1 overbills at ₹150 -> quarantined!
    inv1, res1 = submit_invoice_and_match(
        db_session,
        po_id=po.id,
        invoice_number="INV-900-v1",
        extracted_qty=100,
        extracted_unit_price=150.0,
        extracted_total=15000.0,
    )
    assert res1 is not None and res1.matched is False
    assert inv1.version == 1
    assert po.status == POStatus.QUARANTINED_INVOICE_MISMATCH.value
    assert part.quarantined_stock == 100
    assert part.on_hand_stock == 10

    # Quarantined units cannot be consumed!
    with pytest.raises(InsufficientStockError):
        consume_stock(db_session, part.id, 20)

    # Invoice v2 STILL wrong (qty 90) -> must NOT double-count quarantined_stock!
    inv2, res2 = submit_invoice_and_match(
        db_session,
        po_id=po.id,
        invoice_number="INV-900-v2",
        extracted_qty=90,
        extracted_unit_price=100.0,
        extracted_total=9000.0,
    )
    assert res2 is not None and res2.matched is False
    assert inv2.version == 2
    assert part.quarantined_stock == 100
    assert po.status == POStatus.QUARANTINED_INVOICE_MISMATCH.value

    # Invoice v3 corrected -> releases quarantine to on_hand_stock and closes PO!
    inv3, res3 = submit_invoice_and_match(
        db_session,
        po_id=po.id,
        invoice_number="INV-900-v3",
        extracted_qty=100,
        extracted_unit_price=100.0,
        extracted_total=10000.0,
    )
    assert res3 is not None and res3.matched is True
    assert inv3.version == 3
    assert po.status == POStatus.CLOSED.value
    assert grn.status == GRNStatus.RELEASED.value
    assert part.quarantined_stock == 0
    assert part.on_hand_stock == 110
    assert get_on_order_stock(db_session, part.id) == 0

    txs = db_session.scalars(
        select(InventoryTransaction).where(
            InventoryTransaction.part_id == part.id,
            InventoryTransaction.type == InventoryTxType.RECEIPT_RELEASED.value,
        )
    ).all()
    assert len(txs) == 1
    assert txs[0].qty_delta == 100


def test_invoice_arriving_before_grn_reconciles_when_grn_logged(db_session: Session) -> None:
    part, primary, _ = _setup_part_with_backup(db_session)
    po = create_po(db_session, part_id=part.id, supplier_id=primary.id, qty=100, unit_price=100.0)
    transition_po(db_session, po, POStatus.SENT_AWAITING_ACK)
    transition_po(db_session, po, POStatus.ACKNOWLEDGED)

    inv, res_before = submit_invoice_and_match(
        db_session,
        po_id=po.id,
        invoice_number="INV-EARLY-1",
        extracted_qty=100,
        extracted_unit_price=100.0,
        extracted_total=10000.0,
    )
    assert res_before is None
    assert inv.match_status == InvoiceMatchStatus.PENDING_GRN.value

    grn, res_after = record_goods_receipt(db_session, po_id=po.id, received_qty=100)
    assert res_after is not None and res_after.matched is True
    assert grn.status == GRNStatus.RELEASED.value
    assert po.status == POStatus.CLOSED.value
    assert part.on_hand_stock == 110


def test_switch_po_to_backup_supplier(db_session: Session) -> None:
    part, primary, backup = _setup_part_with_backup(db_session)
    po = create_po(db_session, part_id=part.id, supplier_id=primary.id, qty=60, unit_price=100.0)
    transition_po(db_session, po, POStatus.SENT_AWAITING_ACK)
    transition_po(db_session, po, POStatus.UNRESPONSIVE_ESCALATED)

    cancelled, replacement = switch_po_to_backup_supplier(db_session, po.id)
    assert cancelled.status == POStatus.CANCELLED.value
    assert replacement.id != cancelled.id
    assert replacement.supplier_id == backup.id
    assert replacement.unit_price == 108.0
    assert replacement.qty == 60
    assert replacement.status == POStatus.PENDING_DISPATCH_APPROVAL.value


def test_action_items_lifecycle(db_session: Session) -> None:
    low = create_action_item(
        db_session,
        type="ROP_UPDATE",
        severity=AlertSeverity.INFO,
        urgency_score=20,
        title="Update ROP",
        summary="Suggested ROP increase",
    )
    high = create_action_item(
        db_session,
        type="OVERDUE_PO",
        severity=AlertSeverity.CRITICAL,
        urgency_score=95,
        title="PO Overdue",
        summary="Shipment 2 days late",
    )
    open_items = list_action_items(db_session, status=ActionItemStatus.OPEN)
    assert [i.id for i in open_items] == [high.id, low.id]

    resolve_action_item(db_session, high.id)
    dismiss_action_item(db_session, low.id)
    assert list_action_items(db_session, status=ActionItemStatus.OPEN) == []
