from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import (
    ActionItemStatus,
    AlertSeverity,
    Criticality,
    GRNStatus,
    InvoiceMatchStatus,
    PersonaKey,
    POStatus,
)
from backend.core.models import Part, PartSupplierTerm, Supplier
from backend.po import (
    ThreeWayMatchInput,
    cancel_po,
    compute_urgency_score,
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
    update_po_terms,
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
    """Pure po/ domain test: verifies PO/GRN/Invoice state & stock-movement directives.

    Note: Per AGENTS.md §2 & P14, po/ does not directly mutate Part stock or
    InventoryTransaction rows; it emits structured stock_action directives for orchestrator/.
    """
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

    # Invoice v1 overbills at ₹150 -> transitions PO to QUARANTINED_INVOICE_MISMATCH
    # and emits HOLD_QUARANTINE directive for 100 units.
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
    assert res1.stock_action == "HOLD_QUARANTINE"
    assert res1.stock_qty == 100
    assert part.quarantined_stock == 0  # po/ never mutates Part table directly
    assert part.on_hand_stock == 10

    # Invoice v2 STILL wrong (qty 90) -> must emit stock_action="NONE" so orchestrator
    # never double-counts quarantined_stock!
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
    assert res2.stock_action == "NONE"
    assert res2.stock_qty == 0
    assert po.status == POStatus.QUARANTINED_INVOICE_MISMATCH.value

    # Invoice v3 corrected -> emits RELEASE_FROM_QUARANTINE for 100 units and closes PO!
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
    assert res3.stock_action == "RELEASE_FROM_QUARANTINE"
    assert res3.stock_qty == 100


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
    assert res_after.stock_action == "RELEASE_DIRECT"
    assert res_after.stock_qty == 100


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


def test_update_po_terms_rejects_terminal_closed_or_cancelled_pos(db_session: Session) -> None:
    part, primary, _ = _setup_part_with_backup(db_session)
    po_cancelled = create_po(
        db_session, part_id=part.id, supplier_id=primary.id, qty=50, unit_price=100.0
    )
    cancel_po(db_session, po_cancelled.id)
    assert po_cancelled.status == POStatus.CANCELLED.value

    with pytest.raises(ValueError, match="Cannot update terms on CANCELLED PO"):
        update_po_terms(db_session, po_cancelled.id, unit_price=120.0)

    po_closed = create_po(
        db_session,
        part_id=part.id,
        supplier_id=primary.id,
        qty=50,
        unit_price=100.0,
        initial_status=POStatus.CLOSED,
    )
    with pytest.raises(ValueError, match="Cannot update terms on CLOSED PO"):
        update_po_terms(db_session, po_closed.id, qty=60)


def test_compute_urgency_score_strict_severity_hierarchy(db_session: Session) -> None:
    info_alert = create_action_item(
        db_session,
        type="MINOR_DELAY_ACCEPTED",
        severity=AlertSeverity.INFO,
        part_criticality=Criticality.CRITICAL,
        days_to_stockout=0.0,
        title="Info alert",
        summary="Minor note",
    )
    med_alert = create_action_item(
        db_session,
        type="PRE_ETA_CHECKIN_DRAFT",
        severity=AlertSeverity.MEDIUM,
        part_criticality=Criticality.LOW,
        days_to_stockout=30.0,
        title="Medium alert",
        summary="Check-in draft",
    )
    high_alert = create_action_item(
        db_session,
        type="PO_EXCEPTION",
        severity=AlertSeverity.HIGH,
        part_criticality=Criticality.LOW,
        days_to_stockout=30.0,
        title="High alert",
        summary="Price exception",
    )
    crit_low = create_action_item(
        db_session,
        type="OVERDUE_PO",
        severity=AlertSeverity.CRITICAL,
        part_criticality=Criticality.STANDARD,
        days_to_stockout=15.0,
        title="Critical standard alert",
        summary="Overdue shipment",
    )
    crit_top = create_action_item(
        db_session,
        type="ACCELERATED_BURN",
        severity=AlertSeverity.CRITICAL,
        part_criticality=Criticality.CRITICAL,
        days_to_stockout=0.0,
        title="Critical immediate stockout",
        summary="0 on-hand units",
    )

    assert (
        crit_top.urgency_score
        > crit_low.urgency_score
        > high_alert.urgency_score
        > med_alert.urgency_score
        > info_alert.urgency_score
    )
    assert compute_urgency_score(AlertSeverity.CRITICAL, part_criticality=Criticality.LOW) > (
        compute_urgency_score(
            AlertSeverity.HIGH,
            part_criticality=Criticality.CRITICAL,
            days_to_stockout=0.0,
        )
    )
    ordered = list_action_items(db_session, status=ActionItemStatus.OPEN)
    assert [i.id for i in ordered] == [
        crit_top.id,
        crit_low.id,
        high_alert.id,
        med_alert.id,
        info_alert.id,
    ]
