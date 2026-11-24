from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.db import get_session
from backend.core.models import Part, PartSupplierTerm, Supplier, SupplierMemoryNote
from backend.inventory import (
    InsufficientStockError,
    PartNotFoundError,
    get_part_snapshot,
    list_part_snapshots,
    set_manual_demand_override,
    update_rop_and_replenishment,
)
from backend.orchestrator.deps import get_workflow_engine
from backend.orchestrator.workflows import WorkflowEngine
from backend.planning import (
    build_cycle_analytics,
    compute_part_planning_metrics,
    compute_po_suggestion,
)

router = APIRouter(prefix="/api/parts", tags=["parts"])


class ConsumeStockRequest(BaseModel):
    qty: int = Field(gt=0)
    note: str = ""


class UpdateROPRequest(BaseModel):
    rop: int = Field(ge=0)
    replenishment_point: int = Field(gt=0)


class UpdateDemandOverrideRequest(BaseModel):
    manual_demand_override: float | None = Field(default=None, ge=0.0)


def _serialize_part_metrics(part_metrics) -> dict:
    return {
        "daily_burn_ewma": part_metrics.daily_burn_ewma,
        "daily_burn_prev_7d": part_metrics.daily_burn_prev_7d,
        "trend_pct": round(part_metrics.trend_pct, 2),
        "days_to_stockout": part_metrics.days_to_stockout,
        "predicted_stockout_date": (
            part_metrics.predicted_stockout_date.isoformat()
            if part_metrics.predicted_stockout_date is not None
            else None
        ),
        "stockout_urgency": part_metrics.stockout_urgency,
        "comm_buffer_days": round(part_metrics.comm_buffer_days, 2),
        "primary_effective_lt": part_metrics.primary_effective_lt,
    }


@router.get("")
def list_parts(session: Session = Depends(get_session)) -> list[dict]:
    snapshots = list_part_snapshots(session)
    parts_by_id = {p.id: p for p in session.scalars(select(Part)).all()}
    terms = session.scalars(
        select(PartSupplierTerm).order_by(
            PartSupplierTerm.part_id.asc(), PartSupplierTerm.supplier_rank.asc()
        )
    ).all()
    suppliers = {s.id: s for s in session.scalars(select(Supplier)).all()}
    sim_now = clock.now(session)
    latest_note_by_sup: dict[int, str] = {}
    for mem in session.scalars(
        select(SupplierMemoryNote).order_by(SupplierMemoryNote.id.desc())
    ).all():
        latest_note_by_sup.setdefault(mem.supplier_id, mem.note)

    terms_by_part: dict[int, list[dict]] = {}
    for t in terms:
        sup = suppliers.get(t.supplier_id)
        eta_days = max(1, int(round(t.effective_lead_time_days)))
        estimated_arrival = (sim_now + timedelta(days=eta_days)).date().isoformat()
        terms_by_part.setdefault(t.part_id, []).append(
            {
                "supplier_id": t.supplier_id,
                "supplier_name": sup.name if sup else None,
                "supplier_persona": sup.persona_key if sup else None,
                "supplier_rank": t.supplier_rank,
                "agreed_unit_price": t.agreed_unit_price,
                "quoted_lead_time_days": t.quoted_lead_time_days,
                "effective_lead_time_days": t.effective_lead_time_days,
                "moq": t.moq,
                "reliability_score": sup.reliability_score if sup else None,
                "otd_rate": sup.otd_rate if sup else None,
                "response_rate": sup.response_rate if sup else None,
                "avg_response_days": sup.avg_response_days if sup else None,
                "invoice_accuracy_rate": sup.invoice_accuracy_rate if sup else None,
                "estimated_arrival_date": estimated_arrival,
                "latest_memory_note": latest_note_by_sup.get(t.supplier_id),
            }
        )

    output: list[dict] = []
    for snap in snapshots:
        part_obj = parts_by_id.get(snap.part_id)
        metrics_payload = (
            _serialize_part_metrics(
                compute_part_planning_metrics(session, part_obj, sim_now=sim_now)
            )
            if part_obj is not None
            else {}
        )
        output.append(
            {
                **snap.model_dump(),
                **metrics_payload,
                "suppliers": terms_by_part.get(snap.part_id, []),
            }
        )
    return output


@router.get("/{part_id}")
def get_part(part_id: int, session: Session = Depends(get_session)) -> dict:
    try:
        snap = get_part_snapshot(session, part_id)
    except PartNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    part_obj = session.get(Part, part_id)
    metrics_payload = (
        _serialize_part_metrics(compute_part_planning_metrics(session, part_obj))
        if part_obj is not None
        else {}
    )
    return {**snap.model_dump(), **metrics_payload}


@router.get("/{part_id}/cycle-analytics")
def get_part_cycle_analytics(
    part_id: int,
    session: Session = Depends(get_session),
) -> dict:
    part = session.get(Part, part_id)
    if part is None:
        raise HTTPException(status_code=404, detail=f"Part {part_id} not found")
    analytics = build_cycle_analytics(session, part)
    return analytics.model_dump(mode="json")


@router.get("/{part_id}/po-suggestion")
def get_part_po_suggestion(
    part_id: int,
    supplier_id: int | None = Query(default=None),
    session: Session = Depends(get_session),
) -> dict:
    part = session.get(Part, part_id)
    if part is None:
        raise HTTPException(status_code=404, detail=f"Part {part_id} not found")
    snap = get_part_snapshot(session, part_id)
    try:
        suggestion = compute_po_suggestion(
            session,
            part,
            supplier_id=supplier_id,
            inventory_position=snap.inventory_position,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return suggestion.model_dump(mode="json")


@router.post("/{part_id}/consume")
def consume_part_stock(
    part_id: int,
    payload: ConsumeStockRequest,
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict:
    try:
        result = engine.withdraw_stock(
            session, part_id=part_id, qty=payload.qty, note=payload.note
        )
    except PartNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InsufficientStockError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session.commit()
    created_po = result["created_po"]
    draft = result["draft_email"]
    return {
        "consume": result["consume"].model_dump(mode="json"),
        "created_po": (
            {
                "id": created_po.id,
                "po_number": created_po.po_number,
                "qty": created_po.qty,
                "unit_price": created_po.unit_price,
                "status": created_po.status,
            }
            if created_po
            else None
        ),
        "draft_comm_id": draft.id if draft else None,
        "alert_ids": [a.id for a in result["alerts"]],
    }


@router.patch("/{part_id}/rop")
def patch_part_rop(
    part_id: int,
    payload: UpdateROPRequest,
    session: Session = Depends(get_session),
) -> dict:
    try:
        part = update_rop_and_replenishment(
            session,
            part_id,
            rop=payload.rop,
            replenishment_point=payload.replenishment_point,
        )
    except PartNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return {
        "part_id": part.part_id,
        "sku": part.sku,
        "rop": part.rop,
        "replenishment_point": part.replenishment_point,
    }


@router.patch("/{part_id}/demand-override")
def patch_part_demand_override(
    part_id: int,
    payload: UpdateDemandOverrideRequest,
    session: Session = Depends(get_session),
) -> dict:
    try:
        snap = set_manual_demand_override(
            session,
            part_id,
            payload.manual_demand_override,
        )
    except PartNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return snap.model_dump()
