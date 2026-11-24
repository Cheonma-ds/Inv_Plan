import json
import math
from datetime import timedelta
from typing import TypeVar

from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select

from backend.agents.lex import EmailDraftResult, LexAgent
from backend.agents.llm_client import LLMResult
from backend.core import clock
from backend.core.db import get_session
from backend.core.enums import (
    ActionItemStatus,
    AlertSeverity,
    InventoryTxType,
    PersonaKey,
    POStatus,
)
from backend.core.models import ActionItem, InventoryTransaction, Part, Supplier
from backend.main import create_app
from backend.orchestrator.deps import get_workflow_engine
from backend.orchestrator.workflows import WorkflowEngine
from backend.planning import (
    DAILY_EWMA_ALPHA,
    build_cycle_analytics,
    compute_daily_ewma,
    compute_part_planning_metrics,
    compute_po_suggestion,
)
from backend.po import create_po, transition_po

T = TypeVar("T", bound=BaseModel)


class _QuietDraftLLM:
    def generate_structured(
        self,
        *,
        prompt: str,
        response_schema: type[T],
        system_instruction: str | None = None,
        temperature: float = 0.0,
        pdf_bytes: bytes | None = None,
    ) -> LLMResult[T]:
        del system_instruction, temperature, pdf_bytes
        payload = json.loads(prompt)
        if response_schema is EmailDraftResult:
            po_num = payload.get("po_number") or "PO-NEW"
            res = EmailDraftResult(
                to_personas=[payload["recipient_persona"]],
                cc_personas=[PersonaKey.INVENTORY_MANAGER.value],
                subject=f"[{po_num}] PO Dispatch",
                body=f"Dear {payload['recipient_name']}, please confirm {po_num}.",
            )
            return LLMResult(
                parsed=res,  # type: ignore[arg-type]
                raw_text=res.model_dump_json(),
                input_tokens=100,
                output_tokens=40,
                latency_ms=5,
                cost_inr=0.001,
                model_name="mock-gemini",
            )
        raise AssertionError(f"Unexpected schema {response_schema}")


def test_same_day_withdraw_freezes_daily_burn_and_updates_stockout_runway(
    seeded_session,
) -> None:
    part = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    assert part is not None

    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=_QuietDraftLLM()))
    before_metrics = compute_part_planning_metrics(seeded_session, part)
    before_on_hand = part.on_hand_stock
    before_ewma = part.daily_burn_ewma
    before_rop = part.rop
    before_t = part.replenishment_point
    assert before_metrics.days_to_stockout is not None

    # Same-day withdrawal reduces on_hand & days_to_stockout immediately,
    # while leaving daily_burn_ewma & ROP/T frozen until the IST day completes.
    engine.withdraw_stock(seeded_session, part_id=part.id, qty=10, note="Same-day pull")
    seeded_session.refresh(part)

    after_metrics = compute_part_planning_metrics(seeded_session, part)
    after_daily = compute_daily_ewma(seeded_session, part)
    assert part.on_hand_stock == before_on_hand - 10
    assert part.daily_burn_ewma == before_ewma
    assert after_daily.daily_burn_ewma == before_ewma
    assert part.rop == before_rop
    assert part.replenishment_point == before_t
    assert after_metrics.days_to_stockout is not None
    assert after_metrics.days_to_stockout < before_metrics.days_to_stockout


def test_pure_predicted_burn_tick_keeps_ewma_and_rop_t_unchanged(seeded_session) -> None:
    part = seeded_session.scalar(select(Part).where(Part.sku == "FLT-108"))
    assert part is not None

    before_on_hand = part.on_hand_stock
    before_ewma = part.daily_burn_ewma
    before_std = part.daily_demand_std
    before_rop = part.rop
    before_t = part.replenishment_point
    expected_deduct = int(math.floor(part.burn_carry + before_ewma + 1e-9))
    assert expected_deduct > 0

    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=_QuietDraftLLM()))
    engine.advance_clock_and_watch(seeded_session, days=1)
    seeded_session.refresh(part)

    assert part.on_hand_stock == before_on_hand - expected_deduct
    assert part.daily_burn_ewma == before_ewma
    assert part.daily_demand_std == before_std
    assert part.rop == before_rop
    assert part.replenishment_point == before_t
    assert part.last_burn_settled_date == "2026-10-01"

    sim_tx = seeded_session.scalar(
        select(InventoryTransaction)
        .where(
            InventoryTransaction.part_id == part.id,
            InventoryTransaction.is_predicted_burn.is_(True),
        )
        .order_by(InventoryTransaction.id.desc())
        .limit(1)
    )
    assert sim_tx is not None
    assert sim_tx.type == InventoryTxType.CONSUMPTION.value
    assert sim_tx.qty_delta == -expected_deduct


def test_unpredicted_demand_day_updates_ewma_std_and_rop_t(seeded_session) -> None:
    part = seeded_session.scalar(select(Part).where(Part.sku == "FLT-108"))
    assert part is not None

    prev_ewma = part.daily_burn_ewma
    prev_std = part.daily_demand_std
    prev_rop = part.rop
    prev_t = part.replenishment_point
    extra_qty = 25

    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=_QuietDraftLLM()))
    # Withdraw extra units on Oct 1 -> same-day EWMA stays frozen
    engine.withdraw_stock(seeded_session, part_id=part.id, qty=extra_qty, note="Unpredicted spike")
    seeded_session.refresh(part)
    assert part.daily_burn_ewma == prev_ewma

    # Advance +1d -> Oct 1 settles with extra_events == 25 -> EWMA (alpha=0.3), std, & ROP/T update
    engine.advance_clock_and_watch(seeded_session, days=1)
    seeded_session.refresh(part)

    expected_ewma = (
        DAILY_EWMA_ALPHA * (prev_ewma + extra_qty)
        + (1.0 - DAILY_EWMA_ALPHA) * prev_ewma
    )
    assert math.isclose(part.daily_burn_ewma, expected_ewma, rel_tol=1e-9)
    assert part.daily_demand_std > prev_std
    assert part.rop > prev_rop
    assert part.replenishment_point > prev_t


def test_accelerated_burn_pre_zero_and_stockout_active_deduplication(seeded_session) -> None:
    part = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    sup1 = seeded_session.scalar(
        select(Supplier).where(Supplier.persona_key == PersonaKey.SUP1.value)
    )
    assert part is not None and sup1 is not None

    # Create an open dispatched PO arriving in 10 days while burn is ~8.21/day
    sim_now = clock.now(seeded_session)
    po = create_po(
        seeded_session,
        part_id=part.id,
        supplier_id=sup1.id,
        qty=120,
        unit_price=450.0,
        initial_status=POStatus.SENT_AWAITING_ACK,
    )
    transition_po(
        seeded_session,
        po,
        POStatus.ACKNOWLEDGED,
        promised_eta=sim_now + timedelta(days=10),
    )
    # Put on_hand at 40 units (~4.9d runway < 10d ETA)
    part.on_hand_stock = 40
    seeded_session.flush()

    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=_QuietDraftLLM()))

    # 1. Withdraw 8 -> 32 units (~3.9d runway < 10d ETA, still > 0) -> ACCELERATED_BURN fires
    res1 = engine.withdraw_stock(seeded_session, part_id=part.id, qty=8, note="Burst 1")
    burn_alerts_1 = [a for a in res1["alerts"] if a.type == "ACCELERATED_BURN"]
    assert len(burn_alerts_1) == 1
    assert burn_alerts_1[0].severity in (AlertSeverity.HIGH.value, AlertSeverity.CRITICAL.value)

    # 2. Withdraw another 8 -> 24 units -> ACCELERATED_BURN is deduplicated while OPEN
    res2 = engine.withdraw_stock(seeded_session, part_id=part.id, qty=8, note="Burst 2")
    assert [a for a in res2["alerts"] if a.type == "ACCELERATED_BURN"] == []

    # 3. Exhaust remaining 24 -> 0 on hand -> STOCKOUT_ACTIVE (CRITICAL) fires
    res3 = engine.withdraw_stock(seeded_session, part_id=part.id, qty=24, note="Stockout")
    so_alerts = [a for a in res3["alerts"] if a.type == "STOCKOUT_ACTIVE"]
    assert len(so_alerts) == 1
    assert so_alerts[0].severity == AlertSeverity.CRITICAL.value

    open_so = seeded_session.scalars(
        select(ActionItem).where(
            ActionItem.part_id == part.id,
            ActionItem.type == "STOCKOUT_ACTIVE",
            ActionItem.status == ActionItemStatus.OPEN.value,
        )
    ).all()
    assert len(open_so) == 1


def test_cycle_analytics_and_supplier_specific_po_suggestion_routes(seeded_session) -> None:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: seeded_session
    app.dependency_overrides[get_workflow_engine] = lambda: WorkflowEngine(
        lex_agent=LexAgent(llm_client=_QuietDraftLLM())
    )
    client = TestClient(app)

    # 1. GET /api/parts and GET /api/suppliers include Phase 3 enrichment fields
    parts_resp = client.get("/api/parts")
    assert parts_resp.status_code == 200
    parts = parts_resp.json()
    assert len(parts) == 15

    vlv = next(p for p in parts if p["sku"] == "VLV-204")
    for field in (
        "daily_burn_ewma",
        "daily_burn_prev_7d",
        "trend_pct",
        "days_to_stockout",
        "predicted_stockout_date",
        "stockout_urgency",
        "comm_buffer_days",
        "primary_effective_lt",
    ):
        assert field in vlv
    assert vlv["stockout_urgency"] in ("CRITICAL", "WARNING")

    sups_resp = client.get("/api/suppliers")
    assert sups_resp.status_code == 200
    for sup in sups_resp.json():
        assert "avg_response_days" in sup
        assert "cycles_90d" in sup
        assert sup["cycles_90d"] > 0

    # 2. Cycle analytics final history point equals live on_hand_stock before & after withdraw
    ca_resp = client.get(f"/api/parts/{vlv['part_id']}/cycle-analytics")
    assert ca_resp.status_code == 200
    ca = ca_resp.json()
    assert ca["ewma_alpha"] == 0.3
    assert ca["history"][-1]["closing_on_hand"] == vlv["on_hand_stock"]
    assert any(ev["event_type"] == "PO_PLACED" for ev in ca["events"])
    assert any(ev["event_type"] == "REPLENISHMENT" for ev in ca["events"])
    assert len(ca["projected"]) >= 2

    part_obj = seeded_session.get(Part, vlv["part_id"])
    assert part_obj is not None
    client.post(f"/api/parts/{vlv['part_id']}/consume", json={"qty": 5, "note": "Check cycles"})
    seeded_session.refresh(part_obj)
    ca_after = build_cycle_analytics(seeded_session, part_obj)
    assert ca_after.history[-1].closing_on_hand == part_obj.on_hand_stock

    # 3. PO Suggestion varies meaningfully between Primary (Sup2, ~20d LT) and Backup (Sup1, 12d LT)
    sup2_id = next(
        s["supplier_id"] for s in vlv["suppliers"] if s["supplier_persona"] == PersonaKey.SUP2.value
    )
    sup1_id = next(
        s["supplier_id"] for s in vlv["suppliers"] if s["supplier_persona"] == PersonaKey.SUP1.value
    )
    sugg_sup2 = client.get(
        f"/api/parts/{vlv['part_id']}/po-suggestion?supplier_id={sup2_id}"
    )
    sugg_sup1 = client.get(
        f"/api/parts/{vlv['part_id']}/po-suggestion?supplier_id={sup1_id}"
    )
    assert sugg_sup2.status_code == 200 and sugg_sup1.status_code == 200
    d2 = sugg_sup2.json()
    d1 = sugg_sup1.json()
    assert d2["effective_lead_time_days"] > d1["effective_lead_time_days"]
    assert d2["target_stock_t"] > d1["target_stock_t"]
    assert d2["suggested_qty"] > d1["suggested_qty"]
    assert "EWMA α=0.30" in d2["reasoning"]

    # Direct helper check matches endpoint
    direct = compute_po_suggestion(seeded_session, part_obj, supplier_id=sup1_id)
    assert direct.suggested_qty == d1["suggested_qty"]
