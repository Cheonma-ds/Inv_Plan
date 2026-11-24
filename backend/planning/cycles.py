import math
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import GRNStatus, InventoryTxType, POStatus
from backend.core.models import (
    GoodsReceipt,
    InventoryTransaction,
    Part,
    PurchaseOrder,
    Supplier,
)
from backend.planning.ewma_daily import (
    HISTORY_WINDOW_DAYS,
    NON_ON_ORDER_PO_STATUSES,
    compute_part_planning_metrics,
    compute_po_suggestion,
)
from backend.planning.schemas import (
    CycleAnalyticsResult,
    CycleEventMarker,
    DailyStockPoint,
    ProjectedStockPoint,
)


def build_cycle_analytics(
    session: Session,
    part: Part,
    *,
    sim_now: datetime | None = None,
) -> CycleAnalyticsResult:
    """Reconstruct 90-day daily closing on-hand trajectory, cycle events, and future projection."""
    now = clock.ensure_utc(sim_now) if sim_now is not None else clock.now(session)
    d_today = clock.to_ist_date(now)
    d_0 = d_today - timedelta(days=HISTORY_WINDOW_DAYS)

    txs = list(
        session.scalars(
            select(InventoryTransaction)
            .where(
                InventoryTransaction.part_id == part.id,
                InventoryTransaction.timestamp <= now,
            )
            .order_by(InventoryTransaction.timestamp.asc(), InventoryTransaction.id.asc())
        ).all()
    )

    consumption_by_day: dict[date, int] = {}
    receipt_by_day: dict[date, int] = {}
    net_delta_by_day: dict[date, int] = {}

    for tx in txs:
        tx_date = clock.to_ist_date(tx.timestamp)
        if tx_date < d_0 or tx_date > d_today:
            continue
        delta = int(tx.qty_delta)
        net_delta_by_day[tx_date] = net_delta_by_day.get(tx_date, 0) + delta
        if tx.type == InventoryTxType.CONSUMPTION.value and delta < 0:
            consumption_by_day[tx_date] = consumption_by_day.get(tx_date, 0) + abs(delta)
        elif tx.type == InventoryTxType.RECEIPT_RELEASED.value and delta > 0:
            receipt_by_day[tx_date] = receipt_by_day.get(tx_date, 0) + delta
        elif tx.type == InventoryTxType.ADJUSTMENT.value:
            if delta > 0:
                receipt_by_day[tx_date] = receipt_by_day.get(tx_date, 0) + delta
            elif delta < 0:
                consumption_by_day[tx_date] = consumption_by_day.get(tx_date, 0) + abs(delta)

    # Reconstruct closing on_hand backward from live part.on_hand_stock so final point == live
    num_days = (d_today - d_0).days + 1  # 91 points: d_0 .. d_today
    all_dates = [d_0 + timedelta(days=i) for i in range(num_days)]
    closing_by_day: dict[date, int] = {}

    running_stock = int(part.on_hand_stock)
    for d in reversed(all_dates):
        closing_by_day[d] = running_stock
        running_stock = max(0, running_stock - net_delta_by_day.get(d, 0))

    history = [
        DailyStockPoint(
            date=d.isoformat(),
            consumption=consumption_by_day.get(d, 0),
            receipt=receipt_by_day.get(d, 0),
            closing_on_hand=closing_by_day[d],
            is_today=(d == d_today),
        )
        for d in all_dates
    ]

    # Attach lifecycle markers (STOCKOUT / LOW_STOCK, PO_PLACED, REPLENISHMENT)
    events: list[CycleEventMarker] = []
    for idx, pt in enumerate(history):
        prev_on_hand = history[idx - 1].closing_on_hand if idx > 0 else pt.closing_on_hand
        if pt.closing_on_hand == 0 and (idx == 0 or prev_on_hand > 0):
            events.append(
                CycleEventMarker(
                    date=pt.date,
                    event_type="STOCKOUT",
                    label="Stockout (0 on hand)",
                    on_hand=0,
                )
            )
        elif (
            0 < pt.closing_on_hand < part.rop
            and (idx == 0 or prev_on_hand >= part.rop)
        ):
            events.append(
                CycleEventMarker(
                    date=pt.date,
                    event_type="LOW_STOCK",
                    label=f"Crossed below ROP ({pt.closing_on_hand} < {part.rop})",
                    on_hand=pt.closing_on_hand,
                )
            )

    pos = list(
        session.scalars(
            select(PurchaseOrder)
            .where(
                PurchaseOrder.part_id == part.id,
                PurchaseOrder.status != POStatus.CANCELLED.value,
            )
            .order_by(PurchaseOrder.created_at.asc(), PurchaseOrder.id.asc())
        ).all()
    )
    suppliers_by_id = {
        s.id: s for s in session.scalars(select(Supplier)).all()
    }
    po_ids = [po.id for po in pos]
    grns_by_po: dict[int, list[GoodsReceipt]] = {pid: [] for pid in po_ids}
    if po_ids:
        for grn in session.scalars(
            select(GoodsReceipt)
            .where(GoodsReceipt.po_id.in_(po_ids))
            .order_by(GoodsReceipt.received_at.asc(), GoodsReceipt.id.asc())
        ).all():
            grns_by_po[grn.po_id].append(grn)

    for po in pos:
        sup = suppliers_by_id.get(po.supplier_id)
        sup_name = sup.name if sup else "Unknown Supplier"
        placed_anchor = po.sent_at or po.created_at
        placed_date = clock.to_ist_date(placed_anchor)
        if d_0 <= placed_date <= d_today:
            events.append(
                CycleEventMarker(
                    date=placed_date.isoformat(),
                    event_type="PO_PLACED",
                    label=f"PO Placed: {po.po_number} ({po.qty}u, {sup_name})",
                    po_number=po.po_number,
                    supplier_name=sup_name,
                    qty=po.qty,
                )
            )
        for grn in grns_by_po.get(po.id, []):
            if grn.status != GRNStatus.RELEASED.value:
                continue
            rec_date = clock.to_ist_date(grn.received_at)
            if d_0 <= rec_date <= d_today:
                actual_lt = round(
                    max(
                        0.0,
                        (
                            clock.ensure_utc(grn.received_at)
                            - clock.ensure_utc(placed_anchor)
                        ).total_seconds()
                        / 86400.0,
                    ),
                    2,
                )
                events.append(
                    CycleEventMarker(
                        date=rec_date.isoformat(),
                        event_type="REPLENISHMENT",
                        label=(
                            f"Replenishment: {po.po_number} (+{grn.received_qty}u "
                            f"in {actual_lt:.1f}d)"
                        ),
                        po_number=po.po_number,
                        supplier_name=sup_name,
                        qty=grn.received_qty,
                        actual_lt_days=actual_lt,
                    )
                )

    events.sort(key=lambda e: (e.date, e.event_type))

    # On-order stock & open PO arrival steps for projection
    dispatched_pos = [
        po for po in pos if po.status not in NON_ON_ORDER_PO_STATUSES
    ]
    received_totals: dict[int, int] = {}
    if dispatched_pos:
        disp_ids = [po.id for po in dispatched_pos]
        grn_sums = session.execute(
            select(
                GoodsReceipt.po_id,
                func.coalesce(func.sum(GoodsReceipt.received_qty), 0),
            )
            .where(
                GoodsReceipt.po_id.in_(disp_ids),
                GoodsReceipt.status.in_(
                    (GRNStatus.RELEASED.value, GRNStatus.HELD_QUARANTINE.value)
                ),
            )
            .group_by(GoodsReceipt.po_id)
        ).all()
        received_totals = {po_id: int(q or 0) for po_id, q in grn_sums}

    on_order_stock = sum(
        max(0, po.qty - received_totals.get(po.id, 0)) for po in dispatched_pos
    )
    inventory_position = (
        part.on_hand_stock + on_order_stock + part.quarantined_stock
    )

    metrics = compute_part_planning_metrics(session, part, sim_now=now)
    po_suggestion: CycleAnalyticsResult | None = None
    try:
        suggestion = compute_po_suggestion(
            session,
            part,
            supplier_id=metrics.primary_supplier_id,
            inventory_position=inventory_position,
            sim_now=now,
        )
    except ValueError:
        suggestion = None

    # Projected linear depletion + open PO arrival step
    arrivals_by_date: dict[date, list[tuple[str, int]]] = {}
    max_arrival_offset = 0
    for po in dispatched_pos:
        rem_qty = max(0, po.qty - received_totals.get(po.id, 0))
        if rem_qty <= 0:
            continue
        raw_eta = po.revised_eta or po.promised_eta
        if raw_eta is not None:
            eta_date = clock.to_ist_date(raw_eta)
        else:
            anchor = po.sent_at or po.created_at
            eta_date = clock.to_ist_date(
                clock.ensure_utc(anchor) + timedelta(days=metrics.primary_effective_lt)
            )
        arrivals_by_date.setdefault(eta_date, []).append((po.po_number, rem_qty))
        max_arrival_offset = max(max_arrival_offset, (eta_date - d_today).days)

    burn_rate = (
        metrics.daily_burn_ewma
        if metrics.daily_burn_ewma > 0.0
        else metrics.daily_demand_std
    )
    projected: list[ProjectedStockPoint] = [
        ProjectedStockPoint(
            date=d_today.isoformat(),
            projected_on_hand=round(float(part.on_hand_stock), 2),
            event_note=None,
        )
    ]

    if metrics.daily_burn_ewma > 0.0:
        burn_rate = metrics.daily_burn_ewma
        base_days = (
            math.ceil(metrics.days_to_stockout)
            if metrics.days_to_stockout is not None
            else 30
        )
        horizon_days = max(1, min(60, max(base_days + 2, max_arrival_offset + 10)))
        proj_stock = float(part.on_hand_stock)
        for step in range(1, horizon_days + 1):
            cur_date = d_today + timedelta(days=step)
            notes: list[str] = []
            if cur_date in arrivals_by_date:
                for po_num, arr_qty in arrivals_by_date[cur_date]:
                    proj_stock += float(arr_qty)
                    notes.append(f"{po_num} arrival (+{arr_qty})")
            proj_stock = max(0.0, proj_stock - burn_rate)
            projected.append(
                ProjectedStockPoint(
                    date=cur_date.isoformat(),
                    projected_on_hand=round(proj_stock, 2),
                    event_note="; ".join(notes) if notes else None,
                )
            )
            if proj_stock <= 0.0 and cur_date >= d_today + timedelta(days=max_arrival_offset):
                break

    del po_suggestion  # unused placeholder
    return CycleAnalyticsResult(
        part_id=part.id,
        sku=part.sku,
        part_name=part.name,
        criticality=part.criticality,
        on_hand_stock=part.on_hand_stock,
        quarantined_stock=part.quarantined_stock,
        on_order_stock=on_order_stock,
        inventory_position=inventory_position,
        rop=part.rop,
        replenishment_point=part.replenishment_point,
        target_coverage_days=part.target_coverage_days,
        ewma_alpha=0.3,
        metrics=metrics,
        po_suggestion=suggestion,
        history=history,
        events=events,
        projected=projected,
    )
