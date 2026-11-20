from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import Criticality, PersonaKey, POStatus
from backend.core.models import Part, PartSupplierTerm, Supplier
from backend.inventory import (
    InsufficientStockError,
    consume_stock,
    get_inventory_position,
    get_on_order_stock,
    get_part_snapshot,
    hold_in_quarantine,
    release_quarantined_stock,
    update_rop_and_replenishment,
)
from backend.po import create_po, transition_po


def _setup_single_part(
    session: Session,
    *,
    on_hand: int = 4,
    rop: int = 3,
    replenishment_point: int = 6,
    moq: int = 1,
) -> tuple[Part, Supplier]:
    sup = Supplier(
        persona_key=PersonaKey.SUP1.value,
        name="Apex Precision",
        email="orders@apex.in",
        phone="+91-80-1111",
    )
    part = Part(
        sku="TEST-001",
        name="Test Bearing",
        criticality=Criticality.CRITICAL.value,
        on_hand_stock=on_hand,
        quarantined_stock=0,
        rop=rop,
        replenishment_point=replenishment_point,
    )
    session.add_all([sup, part])
    session.flush()
    session.add(
        PartSupplierTerm(
            part_id=part.id,
            supplier_id=sup.id,
            supplier_rank=1,
            agreed_unit_price=100.0,
            quoted_lead_time_days=10,
            effective_lead_time_days=10.0,
            lead_time_std_dev=1.0,
            moq=moq,
            last_validated_at=clock.now(session),
        )
    )
    session.flush()
    return part, sup


def test_manager_example_reorder_and_no_duplicate_po_in_pending_approval(
    db_session: Session,
) -> None:
    part, sup = _setup_single_part(db_session, on_hand=4, rop=3, replenishment_point=6, moq=1)

    # First withdrawal: 4 -> 2 (< ROP 3) => triggers PO for 6 - 2 = 4 units.
    first = consume_stock(db_session, part.id, 2, note="Engineer pull #1")
    assert first.snapshot.on_hand_stock == 2
    assert first.snapshot.inventory_position == 2
    assert first.reorder_triggered is not None
    assert first.reorder_triggered.order_qty == 4
    assert first.reorder_triggered.primary_supplier_id == sup.id

    # Orchestrator creates PO in PENDING_DISPATCH_APPROVAL immediately.
    po = create_po(
        db_session,
        part_id=part.id,
        supplier_id=sup.id,
        qty=first.reorder_triggered.order_qty,
        unit_price=first.reorder_triggered.agreed_unit_price,
        initial_status=POStatus.PENDING_DISPATCH_APPROVAL,
    )
    assert po.status == POStatus.PENDING_DISPATCH_APPROVAL.value
    assert get_on_order_stock(db_session, part.id) == 4
    assert get_inventory_position(db_session, part.id) == 6

    # Second withdrawal BEFORE manager even approves the PO email: 2 -> 0.
    # Inventory Position = 0 + 4 = 4 >= ROP (3) => MUST NOT trigger duplicate PO!
    second = consume_stock(db_session, part.id, 2, note="Engineer pull #2")
    assert second.snapshot.on_hand_stock == 0
    assert second.snapshot.on_order_stock == 4
    assert second.snapshot.inventory_position == 4
    assert second.reorder_triggered is None


def test_reorder_rounds_up_to_moq(db_session: Session) -> None:
    part, _ = _setup_single_part(db_session, on_hand=5, rop=4, replenishment_point=6, moq=10)
    # 5 -> 3 (< ROP 4). Raw order = 6 - 3 = 3, rounded up to MOQ 10.
    result = consume_stock(db_session, part.id, 2)
    assert result.reorder_triggered is not None
    assert result.reorder_triggered.order_qty == 10


def test_accelerated_burn_alert_when_stock_hits_zero_with_far_eta(db_session: Session) -> None:
    part, sup = _setup_single_part(db_session, on_hand=2, rop=3, replenishment_point=6)
    po = create_po(db_session, part_id=part.id, supplier_id=sup.id, qty=4, unit_price=100.0)
    transition_po(db_session, po, POStatus.SENT_AWAITING_ACK)
    transition_po(
        db_session,
        po,
        POStatus.ACKNOWLEDGED,
        promised_eta=clock.now(db_session) + timedelta(days=8),
    )

    result = consume_stock(db_session, part.id, 2, note="Emergency usage")
    assert result.snapshot.on_hand_stock == 0
    assert result.reorder_triggered is None  # inv_pos = 0 + 4 >= 3
    assert result.accelerated_burn is not None
    assert result.accelerated_burn.open_po_ids == [po.id]
    assert result.accelerated_burn.days_until_eta == pytest.approx(8.0, abs=0.1)


def test_no_accelerated_burn_alert_when_eta_within_three_days(db_session: Session) -> None:
    part, sup = _setup_single_part(db_session, on_hand=2, rop=3, replenishment_point=6)
    po = create_po(db_session, part_id=part.id, supplier_id=sup.id, qty=4, unit_price=100.0)
    transition_po(db_session, po, POStatus.SENT_AWAITING_ACK)
    transition_po(
        db_session,
        po,
        POStatus.ACKNOWLEDGED,
        promised_eta=clock.now(db_session) + timedelta(days=2),
    )

    result = consume_stock(db_session, part.id, 2)
    assert result.accelerated_burn is None


def test_quarantined_stock_cannot_be_consumed(db_session: Session) -> None:
    part, _ = _setup_single_part(db_session, on_hand=1, rop=3, replenishment_point=6)
    hold_in_quarantine(db_session, part.id, 10)
    snap = get_part_snapshot(db_session, part.id)
    assert snap.on_hand_stock == 1
    assert snap.quarantined_stock == 10

    with pytest.raises(InsufficientStockError):
        consume_stock(db_session, part.id, 2)

    released = release_quarantined_stock(db_session, part.id, 10, note="Invoice fixed")
    assert released.quarantined_stock == 0
    assert released.on_hand_stock == 11


def test_update_rop_and_replenishment_validation(db_session: Session) -> None:
    part, _ = _setup_single_part(db_session)
    updated = update_rop_and_replenishment(db_session, part.id, rop=10, replenishment_point=25)
    assert updated.rop == 10
    assert updated.replenishment_point == 25

    with pytest.raises(ValueError):
        update_rop_and_replenishment(db_session, part.id, rop=20, replenishment_point=20)


def test_seeded_hero_part_reproduces_manager_scenario(seeded_session: Session) -> None:
    sku1 = seeded_session.scalar(select(Part).where(Part.sku == "SKU-001"))
    assert sku1 is not None
    res = consume_stock(seeded_session, sku1.id, 2, note="CNC spindle maintenance")
    assert res.snapshot.on_hand_stock == 2
    assert res.reorder_triggered is not None
    assert res.reorder_triggered.order_qty == 4
