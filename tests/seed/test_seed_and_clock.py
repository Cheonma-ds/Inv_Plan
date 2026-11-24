from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

import backend.main as main_mod
from backend.core import clock
from backend.core.db import create_db_engine, get_session, init_db
from backend.core.enums import AutonomyMode, Criticality, EmailCategory, POStatus
from backend.core.models import (
    AutonomyPolicy,
    InventoryTransaction,
    Part,
    PartSupplierTerm,
    PurchaseOrder,
    Supplier,
    SupplierMemoryNote,
)
from backend.main import create_app
from backend.planning import compute_supplier_score, evaluate_part_recommendation
from backend.seed.data import SEED_START_TIME
from backend.seed.reset import reset_and_seed


def test_simulated_clock_persists_across_restarts(tmp_path: Path) -> None:
    db_file = tmp_path / "clock_test.db"
    url = f"sqlite:///{db_file}"

    engine1 = create_db_engine(url)
    init_db(engine1)
    s1_factory = sessionmaker(bind=engine1, autoflush=False, autocommit=False)
    with s1_factory() as s1:
        start = clock.now(s1)
        assert start == datetime(2026, 10, 1, 7, 30, 0, tzinfo=timezone.utc)
        advanced = clock.advance(s1, days=3, hours=5)
        assert advanced == start + timedelta(days=3, hours=5)
        s1.commit()

        with pytest.raises(ValueError):
            clock.advance(s1, days=-1)
    engine1.dispose()

    # Open a brand-new engine pointing at the same SQLite file -> clock must persist.
    engine2 = create_db_engine(url)
    s2_factory = sessionmaker(bind=engine2, autoflush=False, autocommit=False)
    with s2_factory() as s2:
        reloaded = clock.now(s2)
        assert reloaded == datetime(2026, 10, 4, 12, 30, 0, tzinfo=timezone.utc)
    engine2.dispose()


def test_reset_and_seed_populates_complete_realistic_baseline(tmp_path: Path) -> None:
    db_file = tmp_path / "seed_test.db"
    engine = create_db_engine(f"sqlite:///{db_file}")

    summary = reset_and_seed(engine)
    assert summary["suppliers"] == 3
    assert summary["parts"] == 15
    assert summary["closed_pos"] > 30
    assert summary["open_pos"] == 0

    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        assert clock.now(session) == SEED_START_TIME

        suppliers = session.scalars(select(Supplier).order_by(Supplier.persona_key)).all()
        assert [s.persona_key for s in suppliers] == ["sup1", "sup2", "sup3"]

        # Zero-mismatch guarantee: supplier metrics match planning/scoring.py directly
        for sup in suppliers:
            score = compute_supplier_score(session, sup)
            assert sup.reliability_score == score.reliability_score
            assert sup.otd_rate == score.otd_rate
            assert sup.invoice_accuracy_rate == score.invoice_accuracy_rate
            assert sup.response_rate == score.response_rate
            assert sup.avg_response_days == score.avg_response_days
            assert sup.cycles_90d == score.cycles_90d
            assert sup.cycles_90d > 0

        # Every supplier has a seeded memory note
        note_count = session.scalar(select(func.count(SupplierMemoryNote.id)))
        assert note_count == 3

        # 15 parts across CRITICAL(5), STANDARD(6), LOW(4), each with Primary + Backup supplier
        parts = session.scalars(select(Part)).all()
        assert len(parts) == 15
        crit_counts = {
            c.value: sum(1 for p in parts if p.criticality == c.value) for c in Criticality
        }
        assert crit_counts == {"CRITICAL": 5, "STANDARD": 6, "LOW": 4}

        term_count = session.scalar(select(func.count(PartSupplierTerm.id)))
        assert term_count == 30
        for part in parts:
            ranks = sorted(
                session.scalars(
                    select(PartSupplierTerm.supplier_rank).where(
                        PartSupplierTerm.part_id == part.id
                    )
                ).all()
            )
            assert ranks == [1, 2]
            assert part.last_burn_settled_date == "2026-09-30"
            assert part.daily_burn_ewma > 0
            rec = evaluate_part_recommendation(session, part.id)
            assert part.rop == rec.recommended_rop
            assert part.replenishment_point == rec.recommended_replenishment_point

        # 90 days (Jul 3 .. Sep 30) of consumption & receipt transactions exist
        txs = session.scalars(select(InventoryTransaction)).all()
        assert len(txs) > 900
        ist_dates = [clock.to_ist_date(tx.timestamp) for tx in txs]
        assert min(ist_dates) == datetime(2026, 7, 3).date()
        assert max(ist_dates) <= datetime(2026, 9, 30).date()

        # Zero open POs; all historical POs closed on or before Sep 29
        closed_count = session.scalar(
            select(func.count(PurchaseOrder.id)).where(
                PurchaseOrder.status == POStatus.CLOSED.value
            )
        )
        open_count = session.scalar(
            select(func.count(PurchaseOrder.id)).where(
                PurchaseOrder.status != POStatus.CLOSED.value
            )
        )
        assert closed_count == summary["closed_pos"]
        assert open_count == 0

        all_pos = session.scalars(select(PurchaseOrder)).all()
        assert all(po.sent_at is not None and po.created_at < po.sent_at for po in all_pos)
        assert all(
            po.acknowledged_at is not None
            and po.promised_dispatch_date is not None
            and po.actual_dispatch_date is not None
            and po.carrier_name
            and po.tracking_number
            and po.closed_at is not None
            and clock.to_ist_date(po.closed_at) <= datetime(2026, 9, 29).date()
            for po in all_pos
        )

        # All Lex email categories locked to APPROVAL_REQUIRED in MVP
        policies = session.scalars(
            select(AutonomyPolicy).where(AutonomyPolicy.agent_name == "lex")
        ).all()
        assert len(policies) == len(EmailCategory)
        assert all(p.mode == AutonomyMode.APPROVAL_REQUIRED.value for p in policies)

    # Calling reset_and_seed a second time is idempotent and clean
    summary2 = reset_and_seed(engine)
    assert summary2 == summary
    engine.dispose()


def test_fastapi_lifespan_empty_db_and_admin_reset_seed_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify P1: FastAPI lifespan seeds an empty DB cleanly and POST /api/admin/reset-seed
    resets an active mutated database without TypeError or SQLite table-lock errors.
    """
    db_file = tmp_path / "lifespan_reset.db"
    test_engine = create_db_engine(f"sqlite:///{db_file}")
    test_session_local = sessionmaker(
        bind=test_engine, autoflush=False, autocommit=False, expire_on_commit=False
    )

    monkeypatch.setattr(main_mod, "init_db", lambda: init_db(test_engine))
    monkeypatch.setattr(main_mod, "SessionLocal", test_session_local)

    app = create_app()

    def _override_get_session():
        with test_session_local() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session

    with TestClient(app) as client:
        # 1. Lifespan automatically seeded the empty DB on startup
        parts_resp = client.get("/api/parts")
        assert parts_resp.status_code == 200
        assert len(parts_resp.json()) == 15
        brg_initial_rop = next(p for p in parts_resp.json() if p["sku"] == "BRG-402")["rop"]

        # 2. Mutate state: advance clock (+5h, within SLA window) and modify a part's ROP
        adv_resp = client.post("/api/clock/advance", json={"hours": 5})
        assert adv_resp.status_code == 200
        part0_id = parts_resp.json()[0]["part_id"]
        rop_resp = client.patch(
            f"/api/parts/{part0_id}/rop",
            json={"rop": 42, "replenishment_point": 90},
        )
        assert rop_resp.status_code == 200

        # 3. Call POST /api/admin/reset-seed and verify clean reset to baseline
        reset_resp = client.post("/api/admin/reset-seed")
        assert reset_resp.status_code == 200
        body = reset_resp.json()
        assert body["status"] == "reset"
        assert body["summary"]["suppliers"] == 3
        assert body["summary"]["parts"] == 15
        assert body["summary"]["closed_pos"] > 30
        assert body["summary"]["open_pos"] == 0

        clock_resp = client.get("/api/clock")
        assert clock_resp.status_code == 200
        assert clock_resp.json()["sim_now"] == SEED_START_TIME.isoformat()

        parts_after = client.get("/api/parts").json()
        sku1 = next(p for p in parts_after if p["sku"] == "BRG-402")
        assert sku1["rop"] == brg_initial_rop

    test_engine.dispose()
