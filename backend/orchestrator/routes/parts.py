from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.db import get_session
from backend.core.models import PartSupplierTerm, Supplier
from backend.inventory import (
    InsufficientStockError,
    PartNotFoundError,
    get_part_snapshot,
    list_part_snapshots,
    update_rop_and_replenishment,
)
from backend.orchestrator.deps import get_workflow_engine
from backend.orchestrator.workflows import WorkflowEngine

router = APIRouter(prefix="/api/parts", tags=["parts"])


class ConsumeStockRequest(BaseModel):
    qty: int = Field(gt=0)
    note: str = ""


class UpdateROPRequest(BaseModel):
    rop: int = Field(ge=0)
    replenishment_point: int = Field(gt=0)


@router.get("")
def list_parts(session: Session = Depends(get_session)) -> list[dict]:
    snapshots = list_part_snapshots(session)
    terms = session.scalars(
        select(PartSupplierTerm).order_by(
            PartSupplierTerm.part_id.asc(), PartSupplierTerm.supplier_rank.asc()
        )
    ).all()
    suppliers = {s.id: s for s in session.scalars(select(Supplier)).all()}

    terms_by_part: dict[int, list[dict]] = {}
    for t in terms:
        sup = suppliers.get(t.supplier_id)
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
            }
        )

    return [
        {
            **snap.model_dump(),
            "suppliers": terms_by_part.get(snap.part_id, []),
        }
        for snap in snapshots
    ]


@router.get("/{part_id}")
def get_part(part_id: int, session: Session = Depends(get_session)) -> dict:
    try:
        snap = get_part_snapshot(session, part_id)
    except PartNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return snap.model_dump()


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
        "part_id": part.id,
        "sku": part.sku,
        "rop": part.rop,
        "replenishment_point": part.replenishment_point,
    }
