from datetime import datetime

from pydantic import BaseModel, Field


class UnresolvedDismissedAlert(BaseModel):
    id: int
    type: str
    severity: str
    title: str
    summary: str
    po_id: int | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class PartSnapshot(BaseModel):
    part_id: int
    sku: str
    name: str
    criticality: str
    on_hand_stock: int
    quarantined_stock: int
    on_order_stock: int
    inventory_position: int
    rop: int
    replenishment_point: int
    target_coverage_days: int
    manual_demand_override: float | None = None
    active_po_id: int | None = None
    active_po_number: str | None = None
    active_po_status: str | None = None
    pending_dispatch_qty: int = 0
    unresolved_dismissed_alerts: list[UnresolvedDismissedAlert] = Field(default_factory=list)


class ReorderTriggered(BaseModel):
    part_id: int
    sku: str
    part_name: str
    order_qty: int
    primary_supplier_id: int
    agreed_unit_price: float
    quoted_lead_time_days: int
    moq: int
    inventory_position_before: int
    rop: int
    replenishment_point: int


class AcceleratedBurnAlert(BaseModel):
    part_id: int
    sku: str
    part_name: str
    open_po_ids: list[int]
    earliest_eta: datetime
    days_until_eta: float
    days_to_stockout: float | None = None
    predicted_stockout_date: datetime | None = None


class ConsumeResult(BaseModel):
    snapshot: PartSnapshot
    transaction_id: int
    reorder_triggered: ReorderTriggered | None = None
    accelerated_burn: AcceleratedBurnAlert | None = None
    stockout_active: bool = False
