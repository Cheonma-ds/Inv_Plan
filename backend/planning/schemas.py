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
