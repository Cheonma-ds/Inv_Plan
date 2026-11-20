from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select

from backend.core.db import SessionLocal, init_db
from backend.core.models import Supplier
from backend.orchestrator.routes import api_router
from backend.seed.data import seed_database


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    with SessionLocal() as session:
        count = session.scalar(select(func.count(Supplier.id))) or 0
        if count == 0:
            seed_database(session, reset=False)
    yield


def create_app() -> FastAPI:
    application = FastAPI(
        title="InvPlan — Demand-to-Delivery & Inventory Planning",
        version="0.2.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    application.include_router(api_router)
    return application


app = create_app()
