import math
import statistics
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import InventoryTxType
from backend.core.models import InventoryTransaction, Part
from backend.planning.schemas import DemandForecastResult

EWMA_ALPHA = 0.25
MONTH_BUCKET_DAYS = 30
NUM_MONTH_BUCKETS = 12


def compute_demand_forecast(
    session: Session,
    part: Part,
    *,
    sim_now: datetime | None = None,
    alpha: float = EWMA_ALPHA,
) -> DemandForecastResult:
    """Compute 12-month EWMA demand forecast anchored at simulated clock.

    - Aggregates trailing 12 contiguous 30-day consumption buckets ending at ``sim_now``.
    - Applies EWMA (alpha=0.25) across the 12 monthly buckets to estimate monthly & daily demand.
    - If > 50% of months have zero consumption (> 6 of 12), flags ``is_intermittent = True``
      and applies a conservative rolling-peak floor so slow-moving critical parts are not
      under-stocked.
    - Adds ``part.manual_demand_override`` (daily additive override) if set by the manager.
    """
    now = clock.ensure_utc(sim_now) if sim_now is not None else clock.now(session)
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
        # bucket 11 is most recent [0, 30d), bucket 0 is oldest [330d, 360d]
        months_back = min(NUM_MONTH_BUCKETS - 1, int(age_days // MONTH_BUCKET_DAYS))
        bucket_idx = (NUM_MONTH_BUCKETS - 1) - months_back
        buckets[bucket_idx] += qty

    # Exponentially Weighted Moving Average across the 12 ordered monthly buckets
    ewma_monthly = float(buckets[0])
    for monthly_val in buckets[1:]:
        ewma_monthly = alpha * float(monthly_val) + (1.0 - alpha) * ewma_monthly

    zero_month_count = sum(1 for val in buckets if val == 0)
    is_intermittent = (zero_month_count / NUM_MONTH_BUCKETS) > 0.50

    rolling_peak_floor_monthly: float | None = None
    effective_monthly = ewma_monthly
    if is_intermittent and any(val > 0 for val in buckets):
        # Conservative rolling-peak floor: max of best 3-month rolling average and 50% of peak month
        rolling_3m_sums = [sum(buckets[i : i + 3]) for i in range(NUM_MONTH_BUCKETS - 2)]
        rolling_3m_avg_peak = max(rolling_3m_sums) / 3.0
        single_month_peak_half = max(buckets) * 0.5
        rolling_peak_floor_monthly = round(max(rolling_3m_avg_peak, single_month_peak_half), 4)
        effective_monthly = max(ewma_monthly, rolling_peak_floor_monthly)

    avg_daily_demand = effective_monthly / float(MONTH_BUCKET_DAYS)

    # Monthly sample std dev -> daily std dev via Var(sum of 30 i.i.d. days) = 30 * sigma_D^2
    monthly_std = statistics.stdev(buckets) if len(buckets) > 1 else 0.0
    daily_demand_std = monthly_std / math.sqrt(float(MONTH_BUCKET_DAYS))

    override = part.manual_demand_override
    effective_daily = avg_daily_demand + (override if override is not None else 0.0)
    effective_daily = max(0.0, effective_daily)

    return DemandForecastResult(
        part_id=part.id,
        sku=part.sku,
        monthly_buckets=buckets,
        ewma_monthly_demand=round(ewma_monthly, 4),
        avg_daily_demand=round(avg_daily_demand, 4),
        daily_demand_std=round(daily_demand_std, 4),
        is_intermittent=is_intermittent,
        zero_month_count=zero_month_count,
        rolling_peak_floor_monthly=rolling_peak_floor_monthly,
        manual_demand_override=override,
        effective_daily_demand=round(effective_daily, 4),
    )
