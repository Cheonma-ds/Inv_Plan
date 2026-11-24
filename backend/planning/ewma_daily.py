import math
import statistics
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import Criticality, GRNStatus, InventoryTxType, POStatus
from backend.core.models import (
    GoodsReceipt,
    InventoryTransaction,
    Part,
    PartSupplierTerm,
    PurchaseOrder,
    Supplier,
)
from backend.planning.schemas import DailyEWMAResult, PartPlanningMetrics, POSuggestionResult
from backend.planning.scoring import compute_effective_lead_time

DAILY_EWMA_ALPHA = 0.3
HISTORY_WINDOW_DAYS = 90
SEED_HISTORY_START_DATE = date(2026, 7, 3)

Z_FACTORS: dict[str, float] = {
    Criticality.CRITICAL.value: 2.05,
    Criticality.STANDARD.value: 1.65,
    Criticality.LOW.value: 1.28,
}

TERMINAL_PO_STATUSES = (POStatus.CLOSED.value, POStatus.CANCELLED.value)
NON_ON_ORDER_PO_STATUSES = (
    POStatus.PENDING_DISPATCH_APPROVAL.value,
    POStatus.CLOSED.value,
    POStatus.CANCELLED.value,
)


def compute_ewma_from_daily_series(
    series: list[float],
    *,
    alpha: float = DAILY_EWMA_ALPHA,
) -> tuple[float, float, float, float, bool, int, float | None, float]:
    """Compute unrounded daily EWMA, 7-day trend, std dev, and intermittent floor."""
    if not series:
        return 0.0, 0.0, 0.0, 0.0, False, 0, None, 0.0

    e_list: list[float] = [float(series[0])]
    for val in series[1:]:
        e_list.append(alpha * float(val) + (1.0 - alpha) * e_list[-1])

    daily_burn_ewma = e_list[-1]
    prev_idx = max(0, len(e_list) - 8)
    daily_burn_prev_7d = e_list[prev_idx]
    trend_pct = (
        ((daily_burn_ewma - daily_burn_prev_7d) / daily_burn_prev_7d) * 100.0
        if daily_burn_prev_7d > 0.0
        else 0.0
    )
    daily_demand_std = statistics.stdev(series) if len(series) >= 2 else 0.0

    zero_day_count = sum(1 for v in series if v <= 0.0)
    has_any_positive = any(v > 0.0 for v in series)
    zero_ratio = zero_day_count / float(len(series))

    rolling_30d_sparse_ratio = 0.0
    if len(series) >= 30:
        windows_30 = len(series) - 29
        sparse_windows = sum(
            1
            for i in range(windows_30)
            if sum(1 for v in series[i : i + 30] if v > 0.0) <= 2
        )
        rolling_30d_sparse_ratio = sparse_windows / float(windows_30)

    is_intermittent = has_any_positive and (
        zero_ratio > 0.50 or rolling_30d_sparse_ratio > 0.50
    )

    rolling_peak_floor_daily: float | None = None
    effective_daily = daily_burn_ewma
    if is_intermittent:
        if len(series) >= 7:
            max_7d_peak_avg = max(
                sum(series[i : i + 7]) / 7.0 for i in range(len(series) - 6)
            )
        else:
            max_7d_peak_avg = sum(series) / float(len(series))

        if len(series) >= 30:
            max_30d_rolling_avg = max(
                sum(series[i : i + 30]) / 30.0 for i in range(len(series) - 29)
            )
        else:
            max_30d_rolling_avg = sum(series) / float(len(series))

        rolling_peak_floor_daily = max(max_7d_peak_avg * 0.5, max_30d_rolling_avg)
        effective_daily = max(daily_burn_ewma, rolling_peak_floor_daily)

    return (
        daily_burn_ewma,
        daily_burn_prev_7d,
        trend_pct,
        daily_demand_std,
        is_intermittent,
        zero_day_count,
        rolling_peak_floor_daily,
        effective_daily,
    )


def _load_daily_consumption_maps(
    session: Session,
    part_id: int,
    *,
    max_ist_date: date,
) -> tuple[dict[date, int], dict[date, int], date | None]:
    """Return (actual_extra_by_date, predicted_burn_by_date, earliest_tx_date)."""
    txs = session.scalars(
        select(InventoryTransaction)
        .where(
            InventoryTransaction.part_id == part_id,
            InventoryTransaction.type == InventoryTxType.CONSUMPTION.value,
        )
        .order_by(InventoryTransaction.timestamp.asc(), InventoryTransaction.id.asc())
    ).all()

    actual_by_date: dict[date, int] = {}
    predicted_by_date: dict[date, int] = {}
    earliest: date | None = None

    for tx in txs:
        qty = abs(int(tx.qty_delta)) if tx.qty_delta < 0 else 0
        if qty == 0:
            continue
        tx_date = clock.to_ist_date(tx.timestamp)
        if tx_date > max_ist_date:
            continue
        if earliest is None or tx_date < earliest:
            earliest = tx_date
        if tx.is_predicted_burn:
            predicted_by_date[tx_date] = predicted_by_date.get(tx_date, 0) + qty
        else:
            actual_by_date[tx_date] = actual_by_date.get(tx_date, 0) + qty

    return actual_by_date, predicted_by_date, earliest


def reconstruct_observed_daily_series(
    session: Session,
    part: Part,
    *,
    end_date: date,
    start_date: date | None = None,
    alpha: float = DAILY_EWMA_ALPHA,
) -> tuple[list[date], list[float], list[float], bool]:
    """Reconstruct daily demand C_d and EWMA E_d from start_date through end_date (inclusive).

    Days prior to Oct 1, 2026 (or without simulated burn) use exact logged consumption.
    Settled simulated days (>= Oct 1, 2026 or marked with is_predicted_burn) use uncensored
    predicted pace + unpredicted discrete draws (C_d = E_{d-1} + extra_d), keeping E_d exact
    on zero-extra-event days without integer truncation drift.
    """
    actual_by_date, predicted_by_date, earliest_tx = _load_daily_consumption_maps(
        session, part.id, max_ist_date=end_date
    )
    resolved_start = start_date or SEED_HISTORY_START_DATE
    if earliest_tx is not None and earliest_tx < resolved_start:
        resolved_start = earliest_tx

    has_pre_oct1 = any(d < date(2026, 10, 1) for d in actual_by_date)
    settled_limit = (
        date.fromisoformat(part.last_burn_settled_date)
        if part.last_burn_settled_date
        else None
    )

    dates: list[date] = []
    c_series: list[float] = []
    e_series: list[float] = []

    cur = resolved_start
    while cur <= end_date:
        extra = float(actual_by_date.get(cur, 0))
        is_sim_day = (
            cur in predicted_by_date
            or (
                settled_limit is not None
                and cur >= date(2026, 10, 1)
                and cur <= settled_limit
            )
        )
        if not e_series:
            c_val = extra
            e_val = c_val
        elif is_sim_day:
            prev_e = e_series[-1]
            if extra == 0.0:
                c_val = prev_e
                e_val = prev_e
            else:
                c_val = prev_e + extra
                e_val = alpha * c_val + (1.0 - alpha) * prev_e
        else:
            c_val = extra
            e_val = alpha * c_val + (1.0 - alpha) * e_series[-1]

        dates.append(cur)
        c_series.append(c_val)
        e_series.append(e_val)
        cur += timedelta(days=1)

    return dates, c_series, e_series, has_pre_oct1


def compute_daily_ewma(
    session: Session,
    part: Part,
    *,
    sim_now: datetime | None = None,
    alpha: float = DAILY_EWMA_ALPHA,
) -> DailyEWMAResult:
    """Compute 90-day IST completed-day EWMA (alpha=0.3) anchored at simulated clock.

    Same-day events on ``to_ist_date(sim_now)`` are strictly excluded.
    """
    now = clock.ensure_utc(sim_now) if sim_now is not None else clock.now(session)
    d_today = clock.to_ist_date(now)
    d_0 = d_today - timedelta(days=HISTORY_WINDOW_DAYS)
    d_89 = d_today - timedelta(days=1)

    settled_date = (
        date.fromisoformat(part.last_burn_settled_date)
        if part.last_burn_settled_date
        else None
    )

    dates, c_full, e_full, has_pre_oct1 = reconstruct_observed_daily_series(
        session,
        part,
        end_date=d_89,
        start_date=min(d_0, SEED_HISTORY_START_DATE),
        alpha=alpha,
    )
    # Extract trailing 90 completed days [d_0 .. d_89]
    window_pairs = [(d, c, e) for d, c, e in zip(dates, c_full, e_full) if d >= d_0]
    series_90 = [c for _, c, _ in window_pairs]
    if len(series_90) < HISTORY_WINDOW_DAYS:
        series_90 = ([0.0] * (HISTORY_WINDOW_DAYS - len(series_90))) + series_90

    (
        calc_ewma,
        calc_prev_7d,
        calc_trend,
        calc_std,
        is_intermittent,
        zero_day_count,
        rolling_peak_floor,
        calc_effective,
    ) = compute_ewma_from_daily_series(series_90, alpha=alpha)

    if settled_date is not None and settled_date >= d_89:
        # Preserve settled Part state (exact zero-extra-event freeze invariant)
        ewma = float(part.daily_burn_ewma)
        prev_7d = (
            float(part.daily_burn_prev_7d)
            if not has_pre_oct1 and part.daily_burn_prev_7d > 0.0
            else (
                window_pairs[-8][2]
                if len(window_pairs) >= 8 and has_pre_oct1
                else float(part.daily_burn_prev_7d)
            )
        )
        trend_pct = ((ewma - prev_7d) / prev_7d) * 100.0 if prev_7d > 0.0 else 0.0
        demand_std = float(part.daily_demand_std)
        effective = (
            max(ewma, rolling_peak_floor)
            if (is_intermittent and rolling_peak_floor is not None)
            else ewma
        )
    else:
        ewma = calc_ewma
        prev_7d = calc_prev_7d
        trend_pct = calc_trend
        demand_std = calc_std
        effective = calc_effective

    override = part.manual_demand_override
    if override is not None:
        effective = max(0.0, effective + float(override))

    return DailyEWMAResult(
        part_id=part.id,
        sku=part.sku,
        daily_series_90d=series_90,
        daily_burn_ewma=ewma,
        daily_burn_prev_7d=prev_7d,
        trend_pct=trend_pct,
        daily_demand_std=demand_std,
        is_intermittent=is_intermittent,
        zero_day_count=zero_day_count,
        rolling_peak_floor_daily=rolling_peak_floor,
        manual_demand_override=override,
        effective_daily_demand=effective,
    )


def _get_on_order_and_earliest_po_eta(
    session: Session,
    part_id: int,
    *,
    default_lt_days: float,
) -> tuple[int, datetime | None]:
    """Compute dispatched on-order qty and earliest active PO effective ETA."""
    dispatched_pos = list(
        session.scalars(
            select(PurchaseOrder)
            .where(
                PurchaseOrder.part_id == part_id,
                PurchaseOrder.status.not_in(NON_ON_ORDER_PO_STATUSES),
            )
            .order_by(PurchaseOrder.id.asc())
        ).all()
    )
    if not dispatched_pos:
        return 0, None

    po_ids = [po.id for po in dispatched_pos]
    grn_rows = session.execute(
        select(
            GoodsReceipt.po_id,
            func.coalesce(func.sum(GoodsReceipt.received_qty), 0),
        )
        .where(
            GoodsReceipt.po_id.in_(po_ids),
            GoodsReceipt.status.in_(
                (GRNStatus.RELEASED.value, GRNStatus.HELD_QUARANTINE.value)
            ),
        )
        .group_by(GoodsReceipt.po_id)
    ).all()
    received_by_po = {po_id: int(qty or 0) for po_id, qty in grn_rows}
    on_order = sum(max(0, po.qty - received_by_po.get(po.id, 0)) for po in dispatched_pos)

    etas: list[datetime] = []
    for po in dispatched_pos:
        raw_eta = po.revised_eta or po.promised_eta
        if raw_eta is not None:
            etas.append(clock.ensure_utc(raw_eta))
        else:
            anchor = po.sent_at or po.created_at
            term = session.scalar(
                select(PartSupplierTerm).where(
                    PartSupplierTerm.part_id == part_id,
                    PartSupplierTerm.supplier_id == po.supplier_id,
                )
            )
            lt = float(term.effective_lead_time_days) if term is not None else default_lt_days
            etas.append(clock.ensure_utc(anchor) + timedelta(days=lt))

    earliest_eta = min(etas) if etas else None
    return on_order, earliest_eta


def compute_part_planning_metrics(
    session: Session,
    part: Part,
    *,
    sim_now: datetime | None = None,
    live_compute_lt: bool = True,
) -> PartPlanningMetrics:
    """Compute live daily burn, stockout runway, comm buffer, safety stock, and urgency tier."""
    now = clock.ensure_utc(sim_now) if sim_now is not None else clock.now(session)
    daily = compute_daily_ewma(session, part, sim_now=now)

    primary_term = session.scalar(
        select(PartSupplierTerm)
        .where(PartSupplierTerm.part_id == part.id)
        .order_by(PartSupplierTerm.supplier_rank.asc())
        .limit(1)
    )
    primary_sup: Supplier | None = (
        session.get(Supplier, primary_term.supplier_id)
        if primary_term is not None
        else None
    )

    quoted_lt = primary_term.quoted_lead_time_days if primary_term is not None else 14
    if primary_term is not None and live_compute_lt:
        lt_calc = compute_effective_lead_time(session, primary_term, sim_now=now)
        l_eff = lt_calc.effective_lead_time_days
        sigma_l = lt_calc.lead_time_std_dev
    elif primary_term is not None:
        l_eff = float(primary_term.effective_lead_time_days)
        sigma_l = float(primary_term.lead_time_std_dev)
    else:
        l_eff = float(quoted_lt)
        sigma_l = 0.0

    avg_resp_days = float(primary_sup.avg_response_days) if primary_sup is not None else 0.5
    resp_rate = float(primary_sup.response_rate) if primary_sup is not None else 1.0
    comm_buffer_days = avg_resp_days + (1.0 if resp_rate < 0.80 else 0.0)

    z = Z_FACTORS.get(part.criticality, 1.65)
    d_eff = daily.effective_daily_demand
    sigma_d = daily.daily_demand_std
    variance_term = (l_eff * (sigma_d**2)) + ((d_eff**2) * (sigma_l**2))
    safety_stock = z * math.sqrt(max(0.0, variance_term))

    burn_rate = (
        daily.daily_burn_ewma
        if daily.daily_burn_ewma > 0.0
        else daily.effective_daily_demand
    )
    if part.on_hand_stock <= 0:
        days_to_stockout: float | None = 0.0
        predicted_stockout_date: datetime | None = now
    elif burn_rate > 0.0:
        days_to_stockout = float(part.on_hand_stock) / burn_rate
        predicted_stockout_date = now + timedelta(days=days_to_stockout)
    else:
        days_to_stockout = None
        predicted_stockout_date = None

    safety_days = (safety_stock / burn_rate) if burn_rate > 0.0 else 0.0
    urgent_threshold_days = l_eff + comm_buffer_days
    warning_threshold_days = urgent_threshold_days + safety_days

    _, earliest_po_eta = _get_on_order_and_earliest_po_eta(
        session, part.id, default_lt_days=l_eff
    )

    if part.on_hand_stock <= 0:
        urgency = "CRITICAL"
    elif (
        earliest_po_eta is not None
        and predicted_stockout_date is not None
        and predicted_stockout_date < earliest_po_eta
    ):
        urgency = "CRITICAL"
    elif (
        earliest_po_eta is None
        and days_to_stockout is not None
        and days_to_stockout <= urgent_threshold_days
    ):
        urgency = "CRITICAL"
    elif (
        earliest_po_eta is None
        and days_to_stockout is not None
        and days_to_stockout <= warning_threshold_days
    ):
        urgency = "WARNING"
    else:
        urgency = "HEALTHY"

    return PartPlanningMetrics(
        part_id=part.id,
        sku=part.sku,
        daily_burn_ewma=daily.daily_burn_ewma,
        daily_burn_prev_7d=daily.daily_burn_prev_7d,
        trend_pct=daily.trend_pct,
        daily_demand_std=sigma_d,
        is_intermittent=daily.is_intermittent,
        days_to_stockout=days_to_stockout,
        predicted_stockout_date=predicted_stockout_date,
        stockout_urgency=urgency,
        comm_buffer_days=comm_buffer_days,
        primary_supplier_id=primary_sup.id if primary_sup else None,
        primary_supplier_name=primary_sup.name if primary_sup else None,
        primary_quoted_lt=quoted_lt,
        primary_effective_lt=l_eff,
        primary_lead_time_std=sigma_l,
        primary_avg_response_days=avg_resp_days,
        primary_response_rate=resp_rate,
        safety_stock=safety_stock,
        safety_days=safety_days,
        urgent_threshold_days=urgent_threshold_days,
        warning_threshold_days=warning_threshold_days,
        earliest_po_eta=earliest_po_eta,
    )


def compute_po_suggestion(
    session: Session,
    part: Part,
    *,
    supplier_id: int | None = None,
    inventory_position: int | None = None,
    sim_now: datetime | None = None,
) -> POSuggestionResult:
    """Compute deterministic supplier-specific PO quantity suggestion + plain-English reasoning."""
    now = clock.ensure_utc(sim_now) if sim_now is not None else clock.now(session)
    daily = compute_daily_ewma(session, part, sim_now=now)

    if supplier_id is not None:
        term = session.scalar(
            select(PartSupplierTerm).where(
                PartSupplierTerm.part_id == part.id,
                PartSupplierTerm.supplier_id == supplier_id,
            )
        )
        if term is None:
            raise ValueError(
                f"Supplier {supplier_id} is not configured for part {part.sku}"
            )
    else:
        term = session.scalar(
            select(PartSupplierTerm)
            .where(PartSupplierTerm.part_id == part.id)
            .order_by(PartSupplierTerm.supplier_rank.asc())
            .limit(1)
        )
        if term is None:
            raise ValueError(f"Part {part.sku} has no configured suppliers")

    sup = session.get(Supplier, term.supplier_id)
    if sup is None:
        raise ValueError(f"Supplier {term.supplier_id} not found")

    lt_res = compute_effective_lead_time(session, term, sim_now=now)
    l_eff_sup = lt_res.effective_lead_time_days
    sigma_l_sup = lt_res.lead_time_std_dev
    comm_buffer_sup = float(sup.avg_response_days) + (
        1.0 if float(sup.response_rate) < 0.80 else 0.0
    )

    if inventory_position is None:
        on_order, _ = _get_on_order_and_earliest_po_eta(
            session, part.id, default_lt_days=l_eff_sup
        )
        inv_pos = part.on_hand_stock + on_order + part.quarantined_stock
    else:
        inv_pos = int(inventory_position)

    z = Z_FACTORS.get(part.criticality, 1.65)
    d_eff = daily.effective_daily_demand
    sigma_d = daily.daily_demand_std
    variance_term = (l_eff_sup * (sigma_d**2)) + ((d_eff**2) * (sigma_l_sup**2))
    ss_sup = z * math.sqrt(max(0.0, variance_term))

    if d_eff > 0.0:
        rop_sup = max(1, math.ceil((d_eff * l_eff_sup) + ss_sup))
        t_sup = rop_sup + math.ceil(d_eff * float(part.target_coverage_days))
        cycle_cov_qty = math.ceil(d_eff * float(part.target_coverage_days))
    else:
        rop_sup = part.rop
        t_sup = part.replenishment_point
        cycle_cov_qty = term.moq

    if inv_pos < t_sup:
        raw_qty = t_sup - inv_pos
        formula_clause = (
            f"Inventory Position ({inv_pos}) is below {sup.name}'s Target Stock T={t_sup} "
            f"(ROP={rop_sup} + {part.target_coverage_days}d coverage), "
            f"giving T - IP = {raw_qty} units"
        )
    else:
        raw_qty = cycle_cov_qty
        formula_clause = (
            f"Inventory Position ({inv_pos}) meets Target Stock T={t_sup}; "
            f"suggesting one {part.target_coverage_days}-day cycle = {raw_qty} units"
        )

    suggested_qty = max(term.moq, raw_qty)
    moq_clause = (
        f" (rounded up to MOQ {term.moq})."
        if suggested_qty > raw_qty
        else f" (meets MOQ {term.moq})."
    )

    reasoning = (
        f"{sup.name} ({'Primary' if term.supplier_rank == 1 else 'Backup'}): "
        f"Daily Burn = {daily.daily_burn_ewma:.2f}/d (EWMA α=0.30), "
        f"Effective LT = {l_eff_sup:.1f}d (quoted {term.quoted_lead_time_days}d, "
        f"comm buffer {comm_buffer_sup:.1f}d), Safety Stock = {ss_sup:.1f}u (Z={z:.2f}). "
        f"{formula_clause}{moq_clause}"
    )

    return POSuggestionResult(
        part_id=part.id,
        sku=part.sku,
        supplier_id=sup.id,
        supplier_name=sup.name,
        supplier_rank=term.supplier_rank,
        moq=term.moq,
        unit_price=term.agreed_unit_price,
        currency=term.currency or "INR",
        quoted_lead_time_days=term.quoted_lead_time_days,
        effective_lead_time_days=l_eff_sup,
        lead_time_std_dev=sigma_l_sup,
        avg_response_days=float(sup.avg_response_days),
        comm_buffer_days=round(comm_buffer_sup, 2),
        daily_burn_ewma=daily.daily_burn_ewma,
        effective_daily_demand=d_eff,
        inventory_position=inv_pos,
        safety_stock=round(ss_sup, 2),
        rop_for_supplier=rop_sup,
        target_stock_t=t_sup,
        suggested_qty=suggested_qty,
        reasoning=reasoning,
    )
