import math

from backend.core.enums import Criticality
from backend.core.models import Part, PartSupplierTerm
from backend.planning.ewma_daily import Z_FACTORS as DAILY_Z_FACTORS
from backend.planning.schemas import DailyEWMAResult, DemandForecastResult, ROPRecommendation

Z_FACTORS: dict[str, float] = DAILY_Z_FACTORS


def compute_rop_recommendation(
    part: Part,
    primary_term: PartSupplierTerm | None,
    primary_supplier_name: str | None,
    forecast: DemandForecastResult | DailyEWMAResult,
) -> ROPRecommendation:
    """Compute Dynamic ROP and Target Replenishment Point (T) deterministically.

    Preserves full floating-point precision across all intermediate math (D_hat, sigma_D,
    L_eff, SS); rounds only on discrete ROP/T thresholds (math.ceil) and display summary fields.

    Formulas:
      SS      = Z * sqrt((L_eff * sigma_D^2) + (D_hat^2 * sigma_L^2))
      ROP_rec = ceil(D_hat * L_eff + SS)
      T_rec   = ROP_rec + ceil(D_hat * target_coverage_days)
    """
    z = Z_FACTORS.get(part.criticality, 1.65)
    d_bar = float(forecast.effective_daily_demand)
    sigma_d = float(forecast.daily_demand_std)

    quoted_lt = primary_term.quoted_lead_time_days if primary_term is not None else 14
    l_eff = (
        float(primary_term.effective_lead_time_days)
        if primary_term is not None
        else float(quoted_lt)
    )
    sigma_l = float(primary_term.lead_time_std_dev) if primary_term is not None else 0.0

    variance_term = (l_eff * (sigma_d**2)) + ((d_bar**2) * (sigma_l**2))
    safety_stock = z * math.sqrt(max(0.0, variance_term))

    if d_bar > 0.0:
        recommended_rop = max(1, math.ceil((d_bar * l_eff) + safety_stock))
        recommended_t = recommended_rop + math.ceil(d_bar * float(part.target_coverage_days))
    else:
        recommended_rop = 0
        recommended_t = 0

    needs_update = (
        recommended_rop != part.rop or recommended_t != part.replenishment_point
    )

    sup_label = primary_supplier_name or "Primary supplier"
    lt_note = (
        f"effective lead time {l_eff:.1f}d vs quoted {quoted_lt}d (σL={sigma_l:.1f}d)"
        if abs(l_eff - quoted_lt) >= 0.5
        else f"effective lead time {l_eff:.1f}d (σL={sigma_l:.1f}d)"
    )
    intermittent_note = " [intermittent floor active]" if forecast.is_intermittent else ""
    override_note = (
        f" [+{forecast.manual_demand_override:.2f}/d override]"
        if forecast.manual_demand_override
        else ""
    )

    if needs_update:
        reason = (
            f"{part.sku} ({part.name}): {sup_label} {lt_note}; demand {d_bar:.2f}/day"
            f"{intermittent_note}{override_note}, Z={z:.2f} (SS={safety_stock:.1f}). "
            f"Recommend updating ROP {part.rop} -> {recommended_rop} and "
            f"T {part.replenishment_point} -> {recommended_t}."
        )
    else:
        reason = (
            f"{part.sku} ({part.name}): current ROP ({part.rop}) and "
            f"T ({part.replenishment_point}) already match statistical target "
            f"({lt_note}, demand {d_bar:.2f}/day)."
        )

    if isinstance(forecast, DemandForecastResult):
        fc_model = forecast
        avg_daily = forecast.avg_daily_demand
    else:
        fc_model = DemandForecastResult(
            part_id=part.id,
            sku=part.sku,
            monthly_buckets=[0] * 12,
            ewma_monthly_demand=round(forecast.daily_burn_ewma * 30.0, 4),
            avg_daily_demand=forecast.effective_daily_demand,
            daily_demand_std=forecast.daily_demand_std,
            is_intermittent=forecast.is_intermittent,
            zero_month_count=0,
            rolling_peak_floor_monthly=(
                round(forecast.rolling_peak_floor_daily * 30.0, 4)
                if forecast.rolling_peak_floor_daily is not None
                else None
            ),
            manual_demand_override=forecast.manual_demand_override,
            effective_daily_demand=forecast.effective_daily_demand,
            daily_burn_ewma=forecast.daily_burn_ewma,
            daily_burn_prev_7d=forecast.daily_burn_prev_7d,
            trend_pct=forecast.trend_pct,
            zero_day_count=forecast.zero_day_count,
            rolling_peak_floor_daily=forecast.rolling_peak_floor_daily,
        )
        avg_daily = forecast.effective_daily_demand

    _ = Criticality  # keep enum import clean if needed
    return ROPRecommendation(
        part_id=part.id,
        sku=part.sku,
        part_name=part.name,
        criticality=part.criticality,
        primary_supplier_id=primary_term.supplier_id if primary_term is not None else None,
        primary_supplier_name=primary_supplier_name,
        quoted_lead_time_days=quoted_lt,
        effective_lead_time_days=round(l_eff, 2),
        lead_time_std_dev=round(sigma_l, 2),
        avg_daily_demand=round(avg_daily, 4),
        effective_daily_demand=round(d_bar, 4),
        daily_demand_std=round(sigma_d, 4),
        is_intermittent=forecast.is_intermittent,
        manual_demand_override=forecast.manual_demand_override,
        z_factor=z,
        safety_stock=round(safety_stock, 2),
        current_rop=part.rop,
        recommended_rop=recommended_rop,
        current_replenishment_point=part.replenishment_point,
        recommended_replenishment_point=recommended_t,
        target_coverage_days=part.target_coverage_days,
        needs_update=needs_update,
        reason=reason,
        forecast=fc_model,
    )
