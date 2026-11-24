from datetime import datetime

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
from backend.orchestrator.logistics_workflow import parse_iso_date_utc
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


class EscalateProcurementRequest(BaseModel):
    reason: str = ""


class ManualPODraftRequest(BaseModel):
    part_id: int
    supplier_id: int
    qty: int = Field(gt=0)


class ManualPOCreateRequest(BaseModel):
    part_id: int
    supplier_id: int
    qty: int = Field(gt=0)
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)


class DispatchEditedPORequest(BaseModel):
    supplier_id: int | None = None
    qty: int | None = Field(default=None, gt=0)
    unit_price: float | None = Field(default=None, gt=0)
    subject: str | None = None
    body: str | None = None
    note: str | None = None


def _iso_or_none(dt: datetime | None) -> str | None:
    return clock.ensure_utc(dt).isoformat() if dt else None


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
        "sent_at": _iso_or_none(po.sent_at),
        "acknowledged_at": _iso_or_none(po.acknowledged_at),
        "promised_dispatch_date": _iso_or_none(po.promised_dispatch_date),
        "actual_dispatch_date": _iso_or_none(po.actual_dispatch_date),
        "carrier_name": po.carrier_name,
        "tracking_number": po.tracking_number,
        "dispatch_details_status": po.dispatch_details_status,
        "dispatch_details_due_at": _iso_or_none(po.dispatch_details_due_at),
        "missing_dispatch_fields": list(po.missing_dispatch_fields_json or []),
        "escalated_to_procurement_at": _iso_or_none(po.escalated_to_procurement_at),
        "procurement_escalation_note": po.procurement_escalation_note,
        "closed_at": _iso_or_none(po.closed_at),
        "promised_eta": _iso_or_none(po.promised_eta),
        "revised_eta": _iso_or_none(po.revised_eta),
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


@router.post("/draft-manual")
def post_manual_po_draft(
    payload: ManualPODraftRequest,
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict:
    try:
        draft = engine.generate_manual_po_draft(
            session,
            part_id=payload.part_id,
            supplier_id=payload.supplier_id,
            qty=payload.qty,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return draft


@router.post("/manual")
def post_create_manual_po(
    payload: ManualPOCreateRequest,
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict:
    try:
        outcome = engine.create_manual_po(
            session,
            part_id=payload.part_id,
            supplier_id=payload.supplier_id,
            qty=payload.qty,
            subject=payload.subject,
            body=payload.body,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    po = outcome["po"]
    part = session.get(Part, po.part_id)
    supplier = session.get(Supplier, po.supplier_id)
    return {
        "po": _serialize_po(po, part, supplier),
        "comm_id": outcome["email"].id,
    }


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
                "attachment_paths": list(
                    c.attachment_paths_json or ([c.attachment_path] if c.attachment_path else [])
                ),
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
        revised_eta = parse_iso_date_utc(payload.revised_eta)
        if revised_eta is None:
            raise HTTPException(status_code=400, detail="Invalid revised_eta ISO date")
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


@router.post("/{po_id}/escalate-procurement")
def post_escalate_procurement(
    po_id: int,
    payload: EscalateProcurementRequest | None = None,
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict:
    try:
        outcome = engine.escalate_to_procurement(
            session,
            po_id,
            reason=payload.reason if payload else "",
        )
    except PONotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session.commit()
    po = outcome["po"]
    part = session.get(Part, po.part_id)
    supplier = session.get(Supplier, po.supplier_id)
    return {
        "po": _serialize_po(po, part, supplier),
        "comm_id": outcome["email"].id,
        "alert_id": outcome["alert"].id,
    }


@router.post("/{po_id}/dispatch-edited")
def post_dispatch_edited_po(
    po_id: int,
    payload: DispatchEditedPORequest,
    session: Session = Depends(get_session),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> dict:
    try:
        outcome = engine.dispatch_edited_po(
            session,
            po_id,
            supplier_id=payload.supplier_id,
            qty=payload.qty,
            unit_price=payload.unit_price,
            subject=payload.subject,
            body=payload.body,
            note=payload.note,
        )
    except PONotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidPOTransitionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    po = outcome["po"]
    part = session.get(Part, po.part_id)
    supplier = session.get(Supplier, po.supplier_id)
    return {
        "po": _serialize_po(po, part, supplier),
        "comm_id": outcome["email"].id,
        "resolved_alerts": outcome["resolved_alerts"],
        "superseded_comm_ids": outcome["superseded_comm_ids"],
    }
