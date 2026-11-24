from datetime import datetime

from pydantic import BaseModel, Field


class DemandForecastResult(BaseModel):
    part_id: int
    sku: str
    monthly_buckets: list[int] = Field(
        description="12 trailing 30-day consumption buckets ordered oldest -> most recent."
    )
    ewma_monthly_demand: float
    avg_daily_demand: float
    daily_demand_std: float
    is_intermittent: bool
    zero_month_count: int
    rolling_peak_floor_monthly: float | None = None
    manual_demand_override: float | None = None
    effective_daily_demand: float
    daily_burn_ewma: float = 0.0
    daily_burn_prev_7d: float = 0.0
    trend_pct: float = 0.0
    zero_day_count: int = 0
    rolling_peak_floor_daily: float | None = None


class DailyEWMAResult(BaseModel):
    part_id: int
    sku: str
    daily_series_90d: list[float]
    daily_burn_ewma: float
    daily_burn_prev_7d: float
    trend_pct: float
    daily_demand_std: float
    is_intermittent: bool
    zero_day_count: int
    rolling_peak_floor_daily: float | None = None
    manual_demand_override: float | None = None
    effective_daily_demand: float


class PartPlanningMetrics(BaseModel):
    part_id: int
    sku: str
    daily_burn_ewma: float
    daily_burn_prev_7d: float
    trend_pct: float
    daily_demand_std: float
    is_intermittent: bool
    days_to_stockout: float | None = None
    predicted_stockout_date: datetime | None = None
    stockout_urgency: str
    comm_buffer_days: float
    primary_supplier_id: int | None = None
    primary_supplier_name: str | None = None
    primary_quoted_lt: int
    primary_effective_lt: float
    primary_lead_time_std: float
    primary_avg_response_days: float
    primary_response_rate: float
    safety_stock: float
    safety_days: float
    urgent_threshold_days: float
    warning_threshold_days: float
    earliest_po_eta: datetime | None = None


class POSuggestionResult(BaseModel):
    part_id: int
    sku: str
    supplier_id: int
    supplier_name: str
    supplier_rank: int
    moq: int
    unit_price: float
    currency: str
    quoted_lead_time_days: int
    effective_lead_time_days: float
    lead_time_std_dev: float
    avg_response_days: float
    comm_buffer_days: float
    daily_burn_ewma: float
    effective_daily_demand: float
    inventory_position: int
    safety_stock: float
    rop_for_supplier: int
    target_stock_t: int
    suggested_qty: int
    reasoning: str


class DailyStockPoint(BaseModel):
    date: str
    consumption: int
    receipt: int
    closing_on_hand: int
    is_today: bool = False


class CycleEventMarker(BaseModel):
    date: str
    event_type: str
    label: str
    po_number: str | None = None
    supplier_name: str | None = None
    qty: int | None = None
    actual_lt_days: float | None = None
    on_hand: int | None = None


class ProjectedStockPoint(BaseModel):
    date: str
    projected_on_hand: float
    event_note: str | None = None


class CycleAnalyticsResult(BaseModel):
    part_id: int
    sku: str
    part_name: str
    criticality: str
    on_hand_stock: int
    quarantined_stock: int
    on_order_stock: int
    inventory_position: int
    rop: int
    replenishment_point: int
    target_coverage_days: int
    ewma_alpha: float = 0.3
    metrics: PartPlanningMetrics
    po_suggestion: POSuggestionResult | None = None
    history: list[DailyStockPoint]
    events: list[CycleEventMarker]
    projected: list[ProjectedStockPoint]


class SupplierScoreResult(BaseModel):
    supplier_id: int
    persona_key: str
    name: str
    closed_po_count: int
    otd_rate: float = Field(ge=0.0, le=1.0)
    avg_days_late: float = Field(ge=0.0)
    otd_component: float = Field(ge=0.0, le=40.0)
    lead_time_std_dev: float = Field(ge=0.0)
    lead_time_consistency_component: float = Field(ge=0.0, le=25.0)
    invoice_accuracy_rate: float = Field(ge=0.0, le=1.0)
    invoice_accuracy_component: float = Field(ge=0.0, le=20.0)
    response_rate: float = Field(ge=0.0, le=1.0)
    responsiveness_component: float = Field(ge=0.0, le=15.0)
    avg_response_days: float = Field(default=0.5, ge=0.0)
    cycles_90d: int = Field(default=0, ge=0)
    reliability_score: float = Field(ge=0.0, le=100.0)


class EffectiveLeadTimeResult(BaseModel):
    term_id: int
    part_id: int
    supplier_id: int
    supplier_rank: int
    quoted_lead_time_days: int
    closed_delivery_count: int
    actual_mean_lead_time_days: float | None = None
    effective_lead_time_days: float
    lead_time_std_dev: float


class ROPRecommendation(BaseModel):
    part_id: int
    sku: str
    part_name: str
    criticality: str
    primary_supplier_id: int | None = None
    primary_supplier_name: str | None = None
    quoted_lead_time_days: int
    effective_lead_time_days: float
    lead_time_std_dev: float
    avg_daily_demand: float
    effective_daily_demand: float
    daily_demand_std: float
    is_intermittent: bool
    manual_demand_override: float | None = None
    z_factor: float
    safety_stock: float
    current_rop: int
    recommended_rop: int
    current_replenishment_point: int
    recommended_replenishment_point: int
    target_coverage_days: int
    needs_update: bool
    reason: str
    forecast: DemandForecastResult


class PlanningRecalculationSummary(BaseModel):
    calculated_at: datetime
    suppliers_updated: int
    terms_updated: int
    parts_evaluated: int
    recommendations_needing_update: int
    supplier_scores: list[SupplierScoreResult]
    lead_time_updates: list[EffectiveLeadTimeResult]
    recommendations: list[ROPRecommendation]
