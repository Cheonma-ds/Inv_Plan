from datetime import datetime

from pydantic import BaseModel


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


class ConsumeResult(BaseModel):
    snapshot: PartSnapshot
    transaction_id: int
    reorder_triggered: ReorderTriggered | None = None
    accelerated_burn: AcceleratedBurnAlert | None = None
