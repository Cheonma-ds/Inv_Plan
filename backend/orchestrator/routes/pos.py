from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.db import get_session
from backend.core.models import (
    GoodsReceipt,
    Invoice,
    Part,
    POCommunication,
    PurchaseOrder,
    Supplier,
)
from backend.orchestrator.deps import get_workflow_engine
from backend.orchestrator.workflows import WorkflowEngine
from backend.po import (
    InvalidPOTransitionError,
    PONotFoundError,
    update_po_terms,
)

router = APIRouter(prefix="/api/pos", tags=["purchase_orders"])


class RecordGRNRequest(BaseModel):
    received_qty: int = Field(gt=0)
    condition: str = "GOOD"


class UpdatePORequest(BaseModel):
    qty: int | None = Field(default=None, gt=0)
    unit_price: float | None = Field(default=None, gt=0)
    revised_eta: str | None = None
    note: str | None = None


class SwitchSupplierRequest(BaseModel):
    backup_supplier_id: int | None = None


def _serialize_po(
    po: PurchaseOrder,
    part: Part | None,
    supplier: Supplier | None,
) -> dict:
    return {
        "id": po.id,
        "po_number": po.po_number,
        "part_id": po.part_id,
        "part_sku": part.sku if part else None,
        "part_name": part.name if part else None,
        "part_criticality": part.criticality if part else None,
        "supplier_id": po.supplier_id,
        "supplier_name": supplier.name if supplier else None,
        "supplier_persona": supplier.persona_key if supplier else None,
        "qty": po.qty,
        "unit_price": po.unit_price,
        "total_amount": round(po.qty * po.unit_price, 2),
        "status": po.status,
        "created_at": clock.ensure_utc(po.created_at).isoformat(),
        "sent_at": clock.ensure_utc(po.sent_at).isoformat() if po.sent_at else None,
        "promised_eta": (
            clock.ensure_utc(po.promised_eta).isoformat() if po.promised_eta else None
        ),
        "revised_eta": clock.ensure_utc(po.revised_eta).isoformat() if po.revised_eta else None,
        "follow_up_count": po.follow_up_count,
        "pre_eta_checked": po.pre_eta_checked,
        "notes": po.notes,
    }


@router.get("")
def list_pos(
    status: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> list[dict]:
    stmt = select(PurchaseOrder)
    if status:
        stmt = stmt.where(PurchaseOrder.status == status)
    stmt = stmt.order_by(PurchaseOrder.id.desc())
    pos = session.scalars(stmt).all()
    parts = {p.id: p for p in session.scalars(select(Part)).all()}
    suppliers = {s.id: s for s in session.scalars(select(Supplier)).all()}
    return [_serialize_po(po, parts.get(po.part_id), suppliers.get(po.supplier_id)) for po in pos]


@router.get("/{po_id}")
def get_po_detail(po_id: int, session: Session = Depends(get_session)) -> dict:
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status_code=404, detail=f"PurchaseOrder {po_id} not found")
    part = session.get(Part, po.part_id)
    supplier = session.get(Supplier, po.supplier_id)
    grns = session.scalars(
        select(GoodsReceipt).where(GoodsReceipt.po_id == po.id).order_by(GoodsReceipt.id.desc())
    ).all()
    invoices = session.scalars(
        select(Invoice).where(Invoice.po_id == po.id).order_by(Invoice.id.desc())
    ).all()
    comms = session.scalars(
        select(POCommunication)
        .where(POCommunication.po_id == po.id)
        .order_by(POCommunication.timestamp.asc(), POCommunication.id.asc())
    ).all()

    return {
        **_serialize_po(po, part, supplier),
        "goods_receipts": [
            {
                "id": g.id,
                "received_qty": g.received_qty,
                "received_at": clock.ensure_utc(g.received_at).isoformat(),
                "condition": g.condition,
                "status": g.status,
            }
            for g in grns
        ],
        "invoices": [
            {
                "id": inv.id,
                "invoice_number": inv.invoice_number,
                "extracted_qty": inv.extracted_qty,
                "extracted_unit_price": inv.extracted_unit_price,
                "extracted_total": inv.extracted_total,
                "match_status": inv.match_status,
                "discrepancy_summary": inv.discrepancy_summary,
                "version": inv.version,
                "pdf_path": inv.pdf_path,
                "received_at": clock.ensure_utc(inv.received_at).isoformat(),
            }
            for inv in invoices
        ],
        "communications": [
            {
                "id": c.id,
                "thread_id": c.thread_id,
                "direction": c.direction,
                "status": c.status,
                "category": c.category,
                "from_persona": c.from_persona,
                "to_personas": c.to_personas,
                "cc_personas": c.cc_personas,
                "subject": c.subject,
                "body": c.body,
                "attachment_path": c.attachment_path,
                "ai_classification": c.ai_classification,
                "ai_confidence": c.ai_confidence,
                "timestamp": clock.ensure_utc(c.timestamp).isoformat(),
            }
            for c in comms
        ],
    }


@router.post("/{po_id}/grn")
def post_grn(
    po_id: int,
    payload: RecordGRNRequest,
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict:
    try:
        outcome = engine.record_grn(
            session,
            po_id=po_id,
            received_qty=payload.received_qty,
            condition=payload.condition,
        )
    except PONotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidPOTransitionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    grn = outcome["grn"]
    match_res = outcome["match_result"]
    po = session.get(PurchaseOrder, po_id)
    assert po is not None
    return {
        "grn_id": grn.id,
        "po_id": po.id,
        "po_status": po.status,
        "received_qty": grn.received_qty,
        "grn_status": grn.status,
        "match_result": match_res.model_dump(mode="json") if match_res else None,
        "alert_id": outcome["alert_id"],
        "draft_comm_id": outcome["draft_comm_id"],
    }


@router.patch("/{po_id}")
def patch_po(
    po_id: int,
    payload: UpdatePORequest,
    session: Session = Depends(get_session),
) -> dict:
    revised_eta = None
    if payload.revised_eta:
        try:
            dt = datetime.fromisoformat(payload.revised_eta.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            revised_eta = dt.astimezone(timezone.utc)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid revised_eta ISO date") from exc
    try:
        po = update_po_terms(
            session,
            po_id,
            qty=payload.qty,
            unit_price=payload.unit_price,
            revised_eta=revised_eta,
            note=payload.note,
        )
    except PONotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    part = session.get(Part, po.part_id)
    supplier = session.get(Supplier, po.supplier_id)
    return _serialize_po(po, part, supplier)


@router.post("/{po_id}/switch-supplier")
def post_switch_supplier(
    po_id: int,
    payload: SwitchSupplierRequest,
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict:
    try:
        outcome = engine.switch_to_backup_supplier(
            session,
            po_id,
            backup_supplier_id=payload.backup_supplier_id,
        )
    except PONotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidPOTransitionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session.commit()
    new_po = outcome["new_po"]
    part = session.get(Part, new_po.part_id)
    supplier = session.get(Supplier, new_po.supplier_id)
    return {
        "cancelled_po_id": outcome["cancelled_po_id"],
        "new_po": _serialize_po(new_po, part, supplier),
        "draft_comm_id": outcome["draft_comm_id"],
        "alert_id": outcome["alert_id"],
        "quarantined_cleared_qty": outcome["quarantined_cleared_qty"],
    }
