from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

from backend.agents.lex import EmailDraftResult, LexAgent
from backend.agents.llm_client import LLMResult
from backend.core import clock
from backend.core.config import settings
from backend.core.db import init_db
from backend.core.enums import (
    Criticality,
    GRNStatus,
    InvoiceMatchStatus,
    LexReminderStatus,
    PersonaKey,
    POStatus,
)
from backend.core.migrations import ensure_schema_migrations
from backend.core.models import (
    GoodsReceipt,
    LexReminder,
    Part,
    PartSupplierTerm,
    Supplier,
)
from backend.inventory import consume_stock, get_on_order_stock, get_part_snapshot
from backend.orchestrator.logistics_workflow import parse_iso_date_utc
from backend.orchestrator.sla_watcher import run_sla_watcher
from backend.po import (
    ThreeWayMatchInput,
    can_transition,
    create_po,
    evaluate_three_way_match,
    record_goods_receipt,
    submit_invoice_and_match,
    transition_po,
)
from backend.seed.data import SEED_START_TIME


class _DummySLAQuizLLM:
    def generate_structured(
        self,
        *,
        prompt: str,
        response_schema,
        system_instruction: str | None = None,
        temperature: float = 0.0,
        pdf_bytes: bytes | None = None,
    ) -> LLMResult:
        del prompt, response_schema, system_instruction, temperature, pdf_bytes
        parsed = EmailDraftResult(
            to_personas=[PersonaKey.SUP1.value],
            cc_personas=[PersonaKey.INVENTORY_MANAGER.value],
            subject="Overdue follow-up",
            body="Please share an updated delivery date.\n\nBest regards,\nLex",
        )
        return LLMResult(
            parsed=parsed,
            raw_text=parsed.model_dump_json(),
            input_tokens=10,
            output_tokens=10,
            latency_ms=1,
            cost_inr=0.0,
            model_name="dummy",
        )


def test_clock_and_ist_date_alignment() -> None:
    expected = datetime(2026, 10, 1, 7, 30, 0, tzinfo=timezone.utc)
    assert SEED_START_TIME == expected
    assert datetime.fromisoformat(settings.default_sim_start) == expected

    # Oct 1 19:00 UTC is Oct 2 00:30 IST
    late_utc = datetime(2026, 10, 1, 19, 0, 0, tzinfo=timezone.utc)
    assert clock.to_ist_date(late_utc) == date(2026, 10, 2)
    assert clock.to_ist_date(expected) == date(2026, 10, 1)

    eta_dt = parse_iso_date_utc("2026-10-05")
    assert eta_dt == datetime(2026, 10, 5, 7, 30, 0, tzinfo=timezone.utc)


def test_phase3_migrations_and_lex_reminder_table(tmp_path: Path) -> None:
    db_file = tmp_path / "legacy_phase2.db"
    engine = create_engine(f"sqlite:///{db_file}", future=True)

    # Simulate a pre-Phase-3 SQLite schema missing the additive Phase 3 columns & lex_reminders
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE parts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sku VARCHAR(64) NOT NULL,
                    name VARCHAR(200) NOT NULL,
                    criticality VARCHAR(32) NOT NULL,
                    on_hand_stock INTEGER NOT NULL DEFAULT 0,
                    quarantined_stock INTEGER NOT NULL DEFAULT 0,
                    rop INTEGER NOT NULL,
                    replenishment_point INTEGER NOT NULL,
                    target_coverage_days INTEGER NOT NULL DEFAULT 30,
                    manual_demand_override FLOAT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE suppliers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    persona_key VARCHAR(32) NOT NULL,
                    name VARCHAR(200) NOT NULL,
                    email VARCHAR(200) NOT NULL,
                    phone VARCHAR(64) NOT NULL DEFAULT '',
                    reliability_score FLOAT NOT NULL DEFAULT 80.0,
                    otd_rate FLOAT NOT NULL DEFAULT 0.85,
                    invoice_accuracy_rate FLOAT NOT NULL DEFAULT 0.90,
                    response_rate FLOAT NOT NULL DEFAULT 0.85
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE inventory_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    part_id INTEGER NOT NULL,
                    type VARCHAR(32) NOT NULL,
                    qty_delta INTEGER NOT NULL,
                    timestamp DATETIME NOT NULL,
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE action_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type VARCHAR(64) NOT NULL,
                    severity VARCHAR(16) NOT NULL,
                    urgency_score INTEGER NOT NULL DEFAULT 50,
                    po_id INTEGER,
                    part_id INTEGER,
                    draft_comm_id INTEGER,
                    title VARCHAR(250) NOT NULL,
                    summary TEXT NOT NULL,
                    suggested_actions_json JSON NOT NULL DEFAULT '[]',
                    cta_payload_json JSON NOT NULL DEFAULT '{}',
                    status VARCHAR(32) NOT NULL DEFAULT 'OPEN',
                    assigned_team VARCHAR(64) NOT NULL DEFAULT 'Inventory',
                    created_at DATETIME NOT NULL,
                    resolved_at DATETIME
                )
                """
            )
        )

    added_first = ensure_schema_migrations(engine)
    assert "parts.daily_burn_ewma" in added_first
    assert "parts.daily_burn_prev_7d" in added_first
    assert "parts.daily_demand_std" in added_first
    assert "parts.burn_carry" in added_first
    assert "parts.last_burn_settled_date" in added_first
    assert "suppliers.avg_response_days" in added_first
    assert "suppliers.cycles_90d" in added_first
    assert "inventory_transactions.is_predicted_burn" in added_first
    assert "action_items.dismiss_resolved_at" in added_first

    # Idempotent second run adds nothing
    assert ensure_schema_migrations(engine) == []

    # init_db creates lex_reminders cleanly
    init_db(engine)
    with engine.begin() as conn:
        cols = conn.execute(text("PRAGMA table_info(lex_reminders)")).fetchall()
    col_names = {c[1] for c in cols}
    assert {"id", "po_id", "part_id", "supplier_id", "remind_at", "reason", "status"} <= col_names
    assert LexReminderStatus.PENDING.value == "PENDING"
    assert LexReminder.__tablename__ == "lex_reminders"
    engine.dispose()


def test_on_order_excludes_draft_pos_and_avoids_cross_po_contamination(db_session) -> None:
    sup = Supplier(persona_key=PersonaKey.SUP1.value, name="Sup1", email="s1@example.com")
    part = Part(
        sku="P3-TEST-1",
        name="Phase 3 Bearing",
        criticality=Criticality.CRITICAL.value,
        on_hand_stock=5,
        rop=4,
        replenishment_point=12,
    )
    db_session.add_all([sup, part])
    db_session.flush()
    db_session.add(
        PartSupplierTerm(
            part_id=part.id,
            supplier_id=sup.id,
            supplier_rank=1,
            agreed_unit_price=100.0,
            quoted_lead_time_days=10,
            effective_lead_time_days=10.0,
            moq=1,
            last_validated_at=clock.now(db_session),
        )
    )
    db_session.flush()

    # Withdrawal drops on_hand 5 -> 3 (< ROP 4), triggers reorder for 12 - 3 = 9
    c1 = consume_stock(db_session, part.id, 2)
    assert c1.reorder_triggered is not None
    assert c1.reorder_triggered.order_qty == 9

    po_draft = create_po(
        db_session,
        part_id=part.id,
        supplier_id=sup.id,
        qty=9,
        unit_price=100.0,
        initial_status=POStatus.PENDING_DISPATCH_APPROVAL,
    )
    snap = get_part_snapshot(db_session, part.id)
    assert snap.on_order_stock == 0
    assert snap.pending_dispatch_qty == 9
    assert snap.active_po_id == po_draft.id
    assert snap.active_po_number == po_draft.po_number
    assert snap.active_po_status == POStatus.PENDING_DISPATCH_APPROVAL.value

    # Second withdrawal must NOT trigger a duplicate PO
    c2 = consume_stock(db_session, part.id, 1)
    assert c2.reorder_triggered is None

    # Dispatch po_draft and create a second PO that gets quarantined via GRN
    transition_po(db_session, po_draft, POStatus.SENT_AWAITING_ACK)
    assert get_on_order_stock(db_session, part.id) == 9

    po_quar = create_po(
        db_session,
        part_id=part.id,
        supplier_id=sup.id,
        qty=10,
        unit_price=100.0,
        initial_status=POStatus.QUARANTINED_INVOICE_MISMATCH,
    )
    db_session.add(
        GoodsReceipt(
            po_id=po_quar.id,
            received_qty=10,
            received_at=clock.now(db_session),
            condition="GOOD",
            status=GRNStatus.HELD_QUARANTINE.value,
        )
    )
    db_session.flush()

    # Per-PO GRN subtraction: po_quar has 0 remaining, po_draft still has 9 remaining!
    assert get_on_order_stock(db_session, part.id) == 9


def test_partial_delivery_three_way_match_and_cumulative_closure(db_session) -> None:
    # Pure matcher check: invoice == grn passes even when < po_qty
    match_res = evaluate_three_way_match(
        ThreeWayMatchInput(
            po_qty=100,
            po_unit_price=50.0,
            grn_received_qty=40,
            invoice_qty=40,
            invoice_unit_price=50.0,
            invoice_line_subtotal=2000.0,
        )
    )
    assert match_res.matched is True
    assert match_res.match_status == InvoiceMatchStatus.MATCHED
    assert match_res.qty_ok is True
    assert match_res.qty_diff == 0
    assert match_res.total_price_impact == 0.0

    sup = Supplier(persona_key=PersonaKey.SUP1.value, name="Sup1", email="s1@example.com")
    part = Part(
        sku="P3-PARTIAL",
        name="Partial Part",
        criticality=Criticality.STANDARD.value,
        on_hand_stock=10,
        rop=5,
        replenishment_point=50,
    )
    db_session.add_all([sup, part])
    db_session.flush()

    po = create_po(
        db_session,
        part_id=part.id,
        supplier_id=sup.id,
        qty=100,
        unit_price=50.0,
        initial_status=POStatus.ACKNOWLEDGED,
    )

    # Partial GRN 1: 40 units + matching invoice 40 units -> stays ACKNOWLEDGED (60 on order)
    grn1, res1 = record_goods_receipt(db_session, po_id=po.id, received_qty=40)
    assert res1 is None
    assert po.status == POStatus.RECEIVED_PENDING_INVOICE.value

    inv1, m1 = submit_invoice_and_match(
        db_session,
        po_id=po.id,
        invoice_number="INV-P1",
        extracted_qty=40,
        extracted_unit_price=50.0,
        extracted_total=2000.0,
    )
    assert m1 is not None and m1.matched is True
    assert grn1.status == GRNStatus.RELEASED.value
    assert inv1.match_status == InvoiceMatchStatus.MATCHED.value
    assert po.status == POStatus.ACKNOWLEDGED.value
    assert get_on_order_stock(db_session, part.id) == 60

    # Partial GRN 2: 60 units + matching invoice 60 units -> cumulative 100 >= 100 -> CLOSED
    grn2, _ = record_goods_receipt(db_session, po_id=po.id, received_qty=60)
    _, m2 = submit_invoice_and_match(
        db_session,
        po_id=po.id,
        invoice_number="INV-P2",
        extracted_qty=60,
        extracted_unit_price=50.0,
        extracted_total=3000.0,
    )
    assert m2 is not None and m2.matched is True
    assert grn2.status == GRNStatus.RELEASED.value
    assert po.status == POStatus.CLOSED.value
    assert get_on_order_stock(db_session, part.id) == 0


def test_overdue_transitions_from_unresponsive_and_sent_awaiting_ack(db_session) -> None:
    assert can_transition(POStatus.UNRESPONSIVE_ESCALATED, POStatus.OVERDUE) is True
    assert can_transition(POStatus.SENT_AWAITING_ACK, POStatus.OVERDUE) is True
    assert can_transition(POStatus.RECEIVED_PENDING_INVOICE, POStatus.ACKNOWLEDGED) is True

    sup = Supplier(persona_key=PersonaKey.SUP1.value, name="Sup1", email="s1@example.com")
    part = Part(
        sku="P3-OVR",
        name="Overdue Part",
        criticality=Criticality.CRITICAL.value,
        on_hand_stock=10,
        rop=5,
        replenishment_point=20,
    )
    db_session.add_all([sup, part])
    db_session.flush()

    sim_now = clock.now(db_session)
    po_unresp = create_po(
        db_session,
        part_id=part.id,
        supplier_id=sup.id,
        qty=10,
        unit_price=100.0,
        initial_status=POStatus.UNRESPONSIVE_ESCALATED,
    )
    po_unresp.promised_eta = sim_now + timedelta(days=5)

    po_sent = create_po(
        db_session,
        part_id=part.id,
        supplier_id=sup.id,
        qty=10,
        unit_price=100.0,
        initial_status=POStatus.SENT_AWAITING_ACK,
    )
    po_sent.promised_eta = sim_now + timedelta(days=5)
    db_session.flush()

    # Advance past promised_eta + 1 day
    clock.advance(db_session, days=6)
    events = run_sla_watcher(db_session, LexAgent(llm_client=_DummySLAQuizLLM()))
    overdue_ids = {e.po_id for e in events if e.event_type == "OVERDUE_DETECTED"}
    assert overdue_ids == {po_unresp.id, po_sent.id}

    db_session.refresh(po_unresp)
    db_session.refresh(po_sent)
    assert po_unresp.status == POStatus.OVERDUE.value
    assert po_sent.status == POStatus.OVERDUE.value
