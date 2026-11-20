from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.core.db import get_session
from backend.seed.data import seed_database

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/reset-seed")
def reset_seed(session: Session = Depends(get_session)) -> dict:
    summary = seed_database(session, reset=True)
    return {"status": "reset", "summary": summary}
