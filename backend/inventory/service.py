from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import GRNStatus, InventoryTxType, POStatus
from backend.core.models import (
    GoodsReceipt,
    InventoryTransaction,
    Part,
    PartSupplierTerm,
    PurchaseOrder,
)
from backend.inventory.schemas import (
    AcceleratedBurnAlert,
    ConsumeResult,
    PartSnapshot,
    ReorderTriggered,
)

TERMINAL_PO_STATUSES = (POStatus.CLOSED.value, POStatus.CANCELLED.value)


class PartNotFoundError(ValueError):
    pass


class InsufficientStockError(ValueError):
    pass


class MissingPrimarySupplierError(ValueError):
    pass


def _get_part_or_raise(session: Session, part_id: int) -> Part:
    part = session.get(Part, part_id)
    if part is None:
        raise PartNotFoundError(f"Part {part_id} not found")
    return part


def get_on_order_stock(session: Session, part_id: int) -> int:
    open_po_ids_stmt = select(PurchaseOrder.id).where(
        PurchaseOrder.part_id == part_id,
        PurchaseOrder.status.not_in(TERMINAL_PO_STATUSES),
    )
    ordered_qty = session.scalar(
        select(func.coalesce(func.sum(PurchaseOrder.qty), 0)).where(
            PurchaseOrder.part_id == part_id,
            PurchaseOrder.status.not_in(TERMINAL_PO_STATUSES),
        )
    )
    released_qty = session.scalar(
        select(func.coalesce(func.sum(GoodsReceipt.received_qty), 0)).where(
            GoodsReceipt.po_id.in_(open_po_ids_stmt),
            GoodsReceipt.status == GRNStatus.RELEASED.value,
        )
    )
    return max(0, int(ordered_qty or 0) - int(released_qty or 0))


def get_inventory_position(session: Session, part_id: int) -> int:
    part = _get_part_or_raise(session, part_id)
    return part.on_hand_stock + get_on_order_stock(session, part_id)


def get_part_snapshot(session: Session, part_id: int) -> PartSnapshot:
    part = _get_part_or_raise(session, part_id)
    on_order = get_on_order_stock(session, part.id)
    return PartSnapshot(
        part_id=part.id,
        sku=part.sku,
        name=part.name,
        criticality=part.criticality,
        on_hand_stock=part.on_hand_stock,
        quarantined_stock=part.quarantined_stock,
        on_order_stock=on_order,
        inventory_position=part.on_hand_stock + on_order,
        rop=part.rop,
        replenishment_point=part.replenishment_point,
        target_coverage_days=part.target_coverage_days,
        manual_demand_override=part.manual_demand_override,
    )


def list_part_snapshots(session: Session) -> list[PartSnapshot]:
    parts = session.scalars(select(Part).order_by(Part.sku)).all()
    return [get_part_snapshot(session, part.id) for part in parts]


def list_transactions(session: Session, part_id: int) -> list[InventoryTransaction]:
    _get_part_or_raise(session, part_id)
    return list(
        session.scalars(
            select(InventoryTransaction)
            .where(InventoryTransaction.part_id == part_id)
            .order_by(InventoryTransaction.timestamp.desc(), InventoryTransaction.id.desc())
        ).all()
    )


def consume_stock(
    session: Session,
    part_id: int,
    qty: int,
    *,
    note: str = "",
) -> ConsumeResult:
    if qty <= 0:
        raise ValueError("Consumption quantity must be positive")

    part = _get_part_or_raise(session, part_id)
    if qty > part.on_hand_stock:
        raise InsufficientStockError(
            f"Cannot withdraw {qty} units of {part.sku}; only {part.on_hand_stock} on hand "
            f"({part.quarantined_stock} quarantined)"
        )

    sim_now = clock.now(session)
    part.on_hand_stock -= qty

    tx = InventoryTransaction(
        part_id=part.id,
        type=InventoryTxType.CONSUMPTION.value,
        qty_delta=-qty,
        timestamp=sim_now,
        note=note,
    )
    session.add(tx)
    session.flush()

    on_order = get_on_order_stock(session, part.id)
    inv_pos = part.on_hand_stock + on_order

    reorder_event: ReorderTriggered | None = None
    if inv_pos < part.rop:
        primary_term = session.scalar(
            select(PartSupplierTerm)
            .where(
                PartSupplierTerm.part_id == part.id,
                PartSupplierTerm.supplier_rank == 1,
            )
            .limit(1)
        )
        if primary_term is None:
            raise MissingPrimarySupplierError(
                f"Part {part.sku} has no primary supplier (supplier_rank=1) configured"
            )
        raw_qty = part.replenishment_point - inv_pos
        order_qty = max(raw_qty, primary_term.moq)
        reorder_event = ReorderTriggered(
            part_id=part.id,
            sku=part.sku,
            part_name=part.name,
            order_qty=order_qty,
            primary_supplier_id=primary_term.supplier_id,
            agreed_unit_price=primary_term.agreed_unit_price,
            quoted_lead_time_days=primary_term.quoted_lead_time_days,
            moq=primary_term.moq,
            inventory_position_before=inv_pos,
            rop=part.rop,
            replenishment_point=part.replenishment_point,
        )

    burn_alert: AcceleratedBurnAlert | None = None
    if part.on_hand_stock == 0:
        open_pos = list(
            session.scalars(
                select(PurchaseOrder).where(
                    PurchaseOrder.part_id == part.id,
                    PurchaseOrder.status.not_in(TERMINAL_PO_STATUSES),
                )
            ).all()
        )
        far_pos: list[tuple[PurchaseOrder, float]] = []
        threshold = sim_now + timedelta(days=3)
        for po in open_pos:
            eta_raw = po.revised_eta or po.promised_eta
            if eta_raw is None:
                continue
            eta = clock.ensure_utc(eta_raw)
            if eta > threshold:
                days_away = (eta - sim_now).total_seconds() / 86400.0
                far_pos.append((po, days_away))
        if far_pos:
            far_pos.sort(key=lambda item: item[1])
            earliest_po, earliest_days = far_pos[0]
            earliest_eta = clock.ensure_utc(earliest_po.revised_eta or earliest_po.promised_eta)
            burn_alert = AcceleratedBurnAlert(
                part_id=part.id,
                sku=part.sku,
                part_name=part.name,
                open_po_ids=[item[0].id for item in far_pos],
                earliest_eta=earliest_eta,
                days_until_eta=round(earliest_days, 2),
            )

    snapshot = PartSnapshot(
        part_id=part.id,
        sku=part.sku,
        name=part.name,
        criticality=part.criticality,
        on_hand_stock=part.on_hand_stock,
        quarantined_stock=part.quarantined_stock,
        on_order_stock=on_order,
        inventory_position=inv_pos,
        rop=part.rop,
        replenishment_point=part.replenishment_point,
        target_coverage_days=part.target_coverage_days,
        manual_demand_override=part.manual_demand_override,
    )
    return ConsumeResult(
        snapshot=snapshot,
        transaction_id=tx.id,
        reorder_triggered=reorder_event,
        accelerated_burn=burn_alert,
    )


def update_rop_and_replenishment(
    session: Session,
    part_id: int,
    *,
    rop: int,
    replenishment_point: int,
) -> PartSnapshot:
    if rop < 0 or replenishment_point <= rop:
        raise ValueError("Require 0 <= rop < replenishment_point")
    part = _get_part_or_raise(session, part_id)
    part.rop = rop
    part.replenishment_point = replenishment_point
    session.flush()
    return get_part_snapshot(session, part.id)


def hold_in_quarantine(session: Session, part_id: int, qty: int) -> PartSnapshot:
    if qty <= 0:
        raise ValueError("Quarantine quantity must be positive")
    part = _get_part_or_raise(session, part_id)
    part.quarantined_stock += qty
    session.flush()
    return get_part_snapshot(session, part.id)


def release_quarantined_stock(
    session: Session,
    part_id: int,
    qty: int,
    *,
    note: str = "",
) -> PartSnapshot:
    if qty <= 0:
        raise ValueError("Release quantity must be positive")
    part = _get_part_or_raise(session, part_id)
    if qty > part.quarantined_stock:
        raise InsufficientStockError(
            f"Cannot release {qty} units from quarantine for {part.sku}; "
            f"only {part.quarantined_stock} quarantined"
        )
    part.quarantined_stock -= qty
    part.on_hand_stock += qty
    tx = InventoryTransaction(
        part_id=part.id,
        type=InventoryTxType.RECEIPT_RELEASED.value,
        qty_delta=qty,
        timestamp=clock.now(session),
        note=note,
    )
    session.add(tx)
    session.flush()
    return get_part_snapshot(session, part.id)
