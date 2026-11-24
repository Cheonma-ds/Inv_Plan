from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import InventoryTxType
from backend.core.models import InventoryTransaction, Part
from backend.planning.ewma_daily import DAILY_EWMA_ALPHA, compute_daily_ewma
from backend.planning.schemas import DemandForecastResult

EWMA_ALPHA = DAILY_EWMA_ALPHA
MONTH_BUCKET_DAYS = 30
NUM_MONTH_BUCKETS = 12


def compute_demand_forecast(
    session: Session,
    part: Part,
    *,
    sim_now: datetime | None = None,
    alpha: float = EWMA_ALPHA,
) -> DemandForecastResult:
    """Compute 90-day daily EWMA (alpha=0.3) demand forecast anchored at simulated clock.

    Preserves 12 trailing 30-day ``monthly_buckets`` for reporting/UI compatibility while
    driving ``avg_daily_demand``, ``daily_demand_std``, ``is_intermittent``, and
    ``effective_daily_demand`` from the full-precision 90-day IST completed-day EWMA.
    """
    now = clock.ensure_utc(sim_now) if sim_now is not None else clock.now(session)
    daily = compute_daily_ewma(session, part, sim_now=now, alpha=alpha)

    window_days = NUM_MONTH_BUCKETS * MONTH_BUCKET_DAYS
    window_start = now - timedelta(days=window_days)
    txs = list(
        session.scalars(
            select(InventoryTransaction)
            .where(
                InventoryTransaction.part_id == part.id,
                InventoryTransaction.type == InventoryTxType.CONSUMPTION.value,
                InventoryTransaction.timestamp >= window_start,
                InventoryTransaction.timestamp <= now,
            )
            .order_by(InventoryTransaction.timestamp.asc())
        ).all()
    )

    buckets = [0] * NUM_MONTH_BUCKETS
    for tx in txs:
        qty = abs(int(tx.qty_delta)) if tx.qty_delta < 0 else 0
        if qty == 0:
            continue
        tx_ts = clock.ensure_utc(tx.timestamp)
        age_seconds = max(0.0, (now - tx_ts).total_seconds())
        age_days = age_seconds / 86400.0
        months_back = min(NUM_MONTH_BUCKETS - 1, int(age_days // MONTH_BUCKET_DAYS))
        bucket_idx = (NUM_MONTH_BUCKETS - 1) - months_back
        buckets[bucket_idx] += qty

    zero_month_count = sum(1 for val in buckets if val == 0)

    # Compute unrounded base avg_daily_demand (excluding override) for clean ROP math
    base_daily = daily.effective_daily_demand
    if daily.manual_demand_override is not None:
        base_daily = max(0.0, daily.effective_daily_demand - daily.manual_demand_override)

    floor_monthly = (
        round(daily.rolling_peak_floor_daily * float(MONTH_BUCKET_DAYS), 4)
        if daily.rolling_peak_floor_daily is not None
        else None
    )

    return DemandForecastResult(
        part_id=part.id,
        sku=part.sku,
        monthly_buckets=buckets,
        ewma_monthly_demand=round(daily.daily_burn_ewma * float(MONTH_BUCKET_DAYS), 4),
        avg_daily_demand=base_daily,
        daily_demand_std=daily.daily_demand_std,
        is_intermittent=daily.is_intermittent,
        zero_month_count=zero_month_count,
        rolling_peak_floor_monthly=floor_monthly,
        manual_demand_override=daily.manual_demand_override,
        effective_daily_demand=daily.effective_daily_demand,
        daily_burn_ewma=daily.daily_burn_ewma,
        daily_burn_prev_7d=daily.daily_burn_prev_7d,
        trend_pct=daily.trend_pct,
        zero_day_count=daily.zero_day_count,
        rolling_peak_floor_daily=daily.rolling_peak_floor_daily,
    )
