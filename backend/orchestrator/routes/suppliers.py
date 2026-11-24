from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.db import get_session
from backend.core.enums import SupplierMemorySource
from backend.core.models import Supplier, SupplierMemoryNote

router = APIRouter(prefix="/api/suppliers", tags=["suppliers"])


class MemoryNoteRequest(BaseModel):
    note: str = Field(min_length=1, max_length=1000)


@router.get("")
def list_suppliers(session: Session = Depends(get_session)) -> list[dict]:
    suppliers = session.scalars(select(Supplier).order_by(Supplier.id.asc())).all()
    notes = session.scalars(
        select(SupplierMemoryNote).order_by(SupplierMemoryNote.id.asc())
    ).all()
    notes_by_sup: dict[int, list[dict]] = {}
    for n in notes:
        notes_by_sup.setdefault(n.supplier_id, []).append(
            {
                "id": n.id,
                "note": n.note,
                "source": n.source,
                "created_at": clock.ensure_utc(n.created_at).isoformat(),
                "updated_at": clock.ensure_utc(n.updated_at).isoformat(),
            }
        )
    return [
        {
            "id": s.id,
            "name": s.name,
            "persona_key": s.persona_key,
            "email": s.email,
            "phone": s.phone,
            "reliability_score": s.reliability_score,
            "otd_rate": s.otd_rate,
            "invoice_accuracy_rate": s.invoice_accuracy_rate,
            "response_rate": s.response_rate,
            "avg_response_days": s.avg_response_days,
            "cycles_90d": s.cycles_90d,
            "memory_notes": notes_by_sup.get(s.id, []),
        }
        for s in suppliers
    ]


@router.post("/{supplier_id}/memory")
def add_memory_note(
    supplier_id: int,
    payload: MemoryNoteRequest,
    session: Session = Depends(get_session),
) -> dict:
    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(status_code=404, detail=f"Supplier {supplier_id} not found")
    sim_now = clock.now(session)
    note = SupplierMemoryNote(
        supplier_id=supplier_id,
        note=payload.note.strip(),
        source=SupplierMemorySource.MANAGER_ADDED.value,
        created_at=sim_now,
        updated_at=sim_now,
    )
    session.add(note)
    session.commit()
    return {
        "id": note.id,
        "supplier_id": note.supplier_id,
        "note": note.note,
        "source": note.source,
        "created_at": clock.ensure_utc(note.created_at).isoformat(),
        "updated_at": clock.ensure_utc(note.updated_at).isoformat(),
    }


@router.patch("/{supplier_id}/memory/{note_id}")
def update_memory_note(
    supplier_id: int,
    note_id: int,
    payload: MemoryNoteRequest,
    session: Session = Depends(get_session),
) -> dict:
    note = session.get(SupplierMemoryNote, note_id)
    if note is None or note.supplier_id != supplier_id:
        raise HTTPException(status_code=404, detail="Supplier memory note not found")
    note.note = payload.note.strip()
    note.updated_at = clock.now(session)
    session.commit()
    return {
        "id": note.id,
        "supplier_id": note.supplier_id,
        "note": note.note,
        "source": note.source,
    }


@router.delete("/{supplier_id}/memory/{note_id}")
def delete_memory_note(
    supplier_id: int,
    note_id: int,
    session: Session = Depends(get_session),
) -> dict:
    note = session.get(SupplierMemoryNote, note_id)
    if note is None or note.supplier_id != supplier_id:
        raise HTTPException(status_code=404, detail="Supplier memory note not found")
    session.delete(note)
    session.commit()
    return {"deleted": True, "id": note_id}
