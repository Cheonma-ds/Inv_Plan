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


_COMM_ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("attachment_paths_json", "JSON NOT NULL DEFAULT '[]'"),
)

_PART_ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("daily_burn_ewma", "FLOAT NOT NULL DEFAULT 0.0"),
    ("daily_burn_prev_7d", "FLOAT NOT NULL DEFAULT 0.0"),
    ("daily_demand_std", "FLOAT NOT NULL DEFAULT 0.0"),
    ("burn_carry", "FLOAT NOT NULL DEFAULT 0.0"),
    ("last_burn_settled_date", "VARCHAR(16)"),
)

_SUPPLIER_ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("avg_response_days", "FLOAT NOT NULL DEFAULT 0.5"),
    ("cycles_90d", "INTEGER NOT NULL DEFAULT 0"),
)

_INV_TX_ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("is_predicted_burn", "BOOLEAN NOT NULL DEFAULT 0"),
)

_ACTION_ITEM_ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("dismiss_resolved_at", "DATETIME"),
)


def _add_missing_columns(
    conn, table_name: str, columns: tuple[tuple[str, str], ...]
) -> list[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    if not rows:
        return []
    existing_cols = {row[1] for row in rows}
    added: list[str] = []
    for col_name, col_def in columns:
        if col_name not in existing_cols:
            conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}"))
            added.append(f"{table_name}.{col_name}")
    return added


def ensure_schema_migrations(target_engine: Engine) -> list[str]:
    """Idempotently add missing Phase 2 / Fix-pack columns on existing SQLite databases."""
    if target_engine.dialect.name != "sqlite":
        return []

    added: list[str] = []
    with target_engine.begin() as conn:
        added.extend(_add_missing_columns(conn, "purchase_orders", _PO_ADDITIVE_COLUMNS))
        added.extend(_add_missing_columns(conn, "po_communications", _COMM_ADDITIVE_COLUMNS))
        added.extend(_add_missing_columns(conn, "parts", _PART_ADDITIVE_COLUMNS))
        added.extend(_add_missing_columns(conn, "suppliers", _SUPPLIER_ADDITIVE_COLUMNS))
        added.extend(_add_missing_columns(conn, "inventory_transactions", _INV_TX_ADDITIVE_COLUMNS))
        added.extend(_add_missing_columns(conn, "action_items", _ACTION_ITEM_ADDITIVE_COLUMNS))
    return added
