import math

from backend.core.enums import Criticality
from backend.core.models import Part, PartSupplierTerm
from backend.planning.schemas import DemandForecastResult, ROPRecommendation

Z_FACTORS: dict[str, float] = {
    Criticality.CRITICAL.value: 2.05,  # 98% service level
    Criticality.STANDARD.value: 1.65,  # 95% service level
    Criticality.LOW.value: 1.28,  # 90% service level
}


def compute_rop_recommendation(
    part: Part,
    primary_term: PartSupplierTerm | None,
    primary_supplier_name: str | None,
    forecast: DemandForecastResult,
) -> ROPRecommendation:
    """Compute Dynamic ROP and Replenishment Point (S) deterministically (DESIGN.md §7).

    Formulas:
      SS      = Z * sqrt((L_eff * sigma_D^2) + (D_bar^2 * sigma_L^2))
      ROP_rec = ceil(D_bar * L_eff + SS)
      S_rec   = ROP_rec + ceil(D_bar * target_coverage_days)
    """
    z = Z_FACTORS.get(part.criticality, 1.65)
    d_bar = forecast.effective_daily_demand
    sigma_d = forecast.daily_demand_std

    quoted_lt = primary_term.quoted_lead_time_days if primary_term is not None else 14
    l_eff = primary_term.effective_lead_time_days if primary_term is not None else float(quoted_lt)
    sigma_l = primary_term.lead_time_std_dev if primary_term is not None else 0.0

    variance_term = (l_eff * (sigma_d**2)) + ((d_bar**2) * (sigma_l**2))
    safety_stock = z * math.sqrt(max(0.0, variance_term))

    if d_bar > 0.0:
        recommended_rop = max(1, math.ceil((d_bar * l_eff) + safety_stock))
        recommended_s = recommended_rop + math.ceil(d_bar * float(part.target_coverage_days))
    else:
        recommended_rop = 0
        recommended_s = 0

    needs_update = (
        recommended_rop != part.rop or recommended_s != part.replenishment_point
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
            f"S {part.replenishment_point} -> {recommended_s}."
        )
    else:
        reason = (
            f"{part.sku} ({part.name}): current ROP ({part.rop}) and "
            f"S ({part.replenishment_point}) already match statistical target "
            f"({lt_note}, demand {d_bar:.2f}/day)."
        )

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
        avg_daily_demand=forecast.avg_daily_demand,
        effective_daily_demand=forecast.effective_daily_demand,
        daily_demand_std=forecast.daily_demand_std,
        is_intermittent=forecast.is_intermittent,
        manual_demand_override=forecast.manual_demand_override,
        z_factor=z,
        safety_stock=round(safety_stock, 2),
        current_rop=part.rop,
        recommended_rop=recommended_rop,
        current_replenishment_point=part.replenishment_point,
        recommended_replenishment_point=recommended_s,
        target_coverage_days=part.target_coverage_days,
        needs_update=needs_update,
        reason=reason,
        forecast=forecast,
    )
