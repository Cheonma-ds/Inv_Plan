from sqlalchemy import Engine, text

# Additive Phase 2 columns for purchase_orders so existing SQLite DBs upgrade cleanly.
_PO_ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("acknowledged_at", "DATETIME"),
    ("promised_dispatch_date", "DATETIME"),
    ("actual_dispatch_date", "DATETIME"),
    ("carrier_name", "VARCHAR(120)"),
    ("tracking_number", "VARCHAR(120)"),
    ("dispatch_details_status", "VARCHAR(32) NOT NULL DEFAULT 'NONE'"),
    ("dispatch_details_due_at", "DATETIME"),
    ("missing_dispatch_fields_json", "JSON NOT NULL DEFAULT '[]'"),
    ("escalated_to_procurement_at", "DATETIME"),
    ("procurement_escalation_note", "TEXT NOT NULL DEFAULT ''"),
    ("closed_at", "DATETIME"),
)


def ensure_schema_migrations(target_engine: Engine) -> list[str]:
    """Idempotently add missing Phase 2 columns on existing SQLite databases."""
    if target_engine.dialect.name != "sqlite":
        return []

    added: list[str] = []
    with target_engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(purchase_orders)")).fetchall()
        if not rows:
            return []
        existing_cols = {row[1] for row in rows}
        for col_name, col_def in _PO_ADDITIVE_COLUMNS:
            if col_name not in existing_cols:
                conn.execute(
                    text(f"ALTER TABLE purchase_orders ADD COLUMN {col_name} {col_def}")
                )
                added.append(f"purchase_orders.{col_name}")
    return added
