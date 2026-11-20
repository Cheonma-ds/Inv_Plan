from fastapi import APIRouter

from backend.orchestrator.routes import (
    admin,
    alerts,
    clock,
    mailroom,
    observability,
    parts,
    pos,
    suppliers,
)

api_router = APIRouter()
api_router.include_router(clock.router)
api_router.include_router(parts.router)
api_router.include_router(pos.router)
api_router.include_router(alerts.router)
api_router.include_router(mailroom.router)
api_router.include_router(suppliers.router)
api_router.include_router(observability.router)
api_router.include_router(admin.router)

__all__ = ["api_router"]
