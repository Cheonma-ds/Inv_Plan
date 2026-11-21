from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from backend.core.db import create_db_engine, init_db
from backend.core.migrations import ensure_schema_migrations
from backend.core.models import PurchaseOrder


def test_init_db_migrates_legacy_sqlite_purchase_orders_idempotently(tmp_path: Path) -> None:
    db_file = tmp_path / "legacy_phase1.db"
    engine = create_db_engine(f"sqlite:///{db_file}")

    # Simulate a Phase 1 purchase_orders table missing all Phase 2 columns
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE purchase_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    po_number VARCHAR(32) NOT NULL UNIQUE,
                    part_id INTEGER NOT NULL,
                    supplier_id INTEGER NOT NULL,
                    qty INTEGER NOT NULL,
                    unit_price FLOAT NOT NULL,
                    status VARCHAR(48) NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at DATETIME NOT NULL,
                    sent_at DATETIME,
                    promised_eta DATETIME,
                    revised_eta DATETIME,
                    follow_up_count INTEGER NOT NULL DEFAULT 0,
                    pre_eta_checked BOOLEAN NOT NULL DEFAULT 0,
                    next_sla_check_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO purchase_orders (
                    po_number, part_id, supplier_id, qty, unit_price, status, notes, created_at
                ) VALUES (
                    'PO-0999', 1, 1, 10, 100.0, 'SENT_AWAITING_ACK', '', '2026-11-10 09:00:00'
                )
                """
            )
        )

    init_db(engine)

    inspector = inspect(engine)
    cols = {col["name"] for col in inspector.get_columns("purchase_orders")}
    expected_new = {
        "acknowledged_at",
        "promised_dispatch_date",
        "actual_dispatch_date",
        "carrier_name",
        "tracking_number",
        "dispatch_details_status",
        "dispatch_details_due_at",
        "missing_dispatch_fields_json",
        "escalated_to_procurement_at",
        "procurement_escalation_note",
        "closed_at",
    }
    assert expected_new.issubset(cols)
    assert "stock_requests" in set(inspector.get_table_names())

    # Second pass must be a clean no-op
    assert ensure_schema_migrations(engine) == []

    # Existing row survives migration and loads cleanly via ORM
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with SessionLocal() as session:
        po = session.query(PurchaseOrder).filter_by(po_number="PO-0999").one()
        assert po.dispatch_details_status == "NONE"
        assert po.missing_dispatch_fields_json == []
        assert po.procurement_escalation_note == ""

    engine.dispose()
