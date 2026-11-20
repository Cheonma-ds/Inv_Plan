from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from backend.core import clock
from backend.core.db import create_db_engine, init_db
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
        assert start == datetime(2026, 11, 20, 9, 0, 0, tzinfo=timezone.utc)
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
        assert reloaded == datetime(2026, 11, 23, 14, 0, 0, tzinfo=timezone.utc)
    engine2.dispose()


def test_reset_and_seed_populates_complete_realistic_baseline(tmp_path: Path) -> None:
    db_file = tmp_path / "seed_test.db"
    engine = create_db_engine(f"sqlite:///{db_file}")

    summary = reset_and_seed(engine)
    assert summary["suppliers"] == 3
    assert summary["parts"] == 15
    assert summary["closed_pos"] == 36
    assert summary["open_pos"] == 2

    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        assert clock.now(session) == SEED_START_TIME

        suppliers = session.scalars(select(Supplier).order_by(Supplier.persona_key)).all()
        assert [s.persona_key for s in suppliers] == ["sup1", "sup2", "sup3"]

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

        # 12 months of consumption history exist
        tx_count = session.scalar(select(func.count(InventoryTransaction.id)))
        assert tx_count is not None and tx_count > 300

        # Open & closed POs
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
        assert closed_count == 36
        assert open_count == 2

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
