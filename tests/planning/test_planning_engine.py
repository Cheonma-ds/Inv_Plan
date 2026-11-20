import math

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.core.db import get_session
from backend.core.enums import ActionItemStatus, Criticality, PersonaKey
from backend.core.models import ActionItem, Part, PartSupplierTerm, Supplier
from backend.main import create_app
from backend.planning import (
    Z_FACTORS,
    compute_demand_forecast,
    compute_effective_lead_time,
    compute_rop_recommendation,
    compute_supplier_score,
    recalculate_all,
)


def test_ewma_forecast_steady_vs_intermittent_and_manual_override(seeded_session):
    steady_part = seeded_session.scalar(select(Part).where(Part.sku == "SKU-001"))
    intermittent_part = seeded_session.scalar(select(Part).where(Part.sku == "SKU-005"))
    assert steady_part is not None and intermittent_part is not None

    steady_fc = compute_demand_forecast(seeded_session, steady_part)
    assert len(steady_fc.monthly_buckets) == 12
    assert steady_fc.is_intermittent is False
    assert steady_fc.zero_month_count <= 6
    assert steady_fc.avg_daily_demand > 0
    assert steady_fc.daily_demand_std >= 0
    assert steady_fc.effective_daily_demand == steady_fc.avg_daily_demand

    # Intermittent spare part: seeded with consumption in only 4 of 12 months (8 zero months > 50%)
    inter_fc = compute_demand_forecast(seeded_session, intermittent_part)
    assert inter_fc.is_intermittent is True
    assert inter_fc.zero_month_count == 8
    assert inter_fc.rolling_peak_floor_monthly is not None
    assert inter_fc.rolling_peak_floor_monthly >= inter_fc.ewma_monthly_demand
    assert inter_fc.avg_daily_demand >= inter_fc.ewma_monthly_demand / 30.0

    # Additive human event override increases effective daily demand
    steady_part.manual_demand_override = 0.5
    seeded_session.flush()
    override_fc = compute_demand_forecast(seeded_session, steady_part)
    assert math.isclose(
        override_fc.effective_daily_demand,
        override_fc.avg_daily_demand + 0.5,
        rel_tol=1e-3,
    )


def test_supplier_reliability_scores_rank_sup1_above_sup3_above_sup2(seeded_session):
    sup1 = seeded_session.scalar(
        select(Supplier).where(Supplier.persona_key == PersonaKey.SUP1.value)
    )
    sup2 = seeded_session.scalar(
        select(Supplier).where(Supplier.persona_key == PersonaKey.SUP2.value)
    )
    sup3 = seeded_session.scalar(
        select(Supplier).where(Supplier.persona_key == PersonaKey.SUP3.value)
    )
    assert sup1 and sup2 and sup3

    s1 = compute_supplier_score(seeded_session, sup1)
    s2 = compute_supplier_score(seeded_session, sup2)
    s3 = compute_supplier_score(seeded_session, sup3)

    assert s1.closed_po_count == 12
    assert s2.closed_po_count == 12
    assert s3.closed_po_count == 12

    # Component sums match total score
    for score in (s1, s2, s3):
        expected = round(
            score.otd_component
            + score.lead_time_consistency_component
            + score.invoice_accuracy_component
            + score.responsiveness_component,
            1,
        )
        assert abs(score.reliability_score - expected) <= 0.2

    # Sup1 benchmark > Sup3 volatile > Sup2 chronically late & invoice-prone
    assert s1.reliability_score > 85.0
    assert s1.reliability_score > s3.reliability_score > s2.reliability_score
    assert s2.otd_rate < 0.3
    assert s2.avg_days_late > 4.0
    assert s2.invoice_accuracy_rate < 0.75
    assert s3.response_rate < 0.5


def test_sup2_late_deliveries_raise_effective_lead_time_and_rop(seeded_session):
    # SKU-002 primary supplier is Sup2 (quotes 15d, actually delivers ~21-26d across 3 closed POs)
    sku2 = seeded_session.scalar(select(Part).where(Part.sku == "SKU-002"))
    sup2 = seeded_session.scalar(
        select(Supplier).where(Supplier.persona_key == PersonaKey.SUP2.value)
    )
    assert sku2 and sup2

    term = seeded_session.scalar(
        select(PartSupplierTerm).where(
            PartSupplierTerm.part_id == sku2.id,
            PartSupplierTerm.supplier_id == sup2.id,
        )
    )
    assert term is not None
    assert term.quoted_lead_time_days == 15

    # Reset effective LT to optimistic quoted LT before recalculation to prove the jump
    term.effective_lead_time_days = float(term.quoted_lead_time_days)
    term.lead_time_std_dev = 0.0
    seeded_session.flush()

    fc = compute_demand_forecast(seeded_session, sku2)
    optimistic_rec = compute_rop_recommendation(sku2, term, sup2.name, fc)

    # Now compute true effective LT from Sup2's historical deliveries
    lt_res = compute_effective_lead_time(seeded_session, term)
    assert lt_res.closed_delivery_count == 3
    assert lt_res.actual_mean_lead_time_days is not None
    assert lt_res.effective_lead_time_days > term.quoted_lead_time_days
    assert lt_res.effective_lead_time_days >= lt_res.actual_mean_lead_time_days

    # Recalculate all persists effective LT and produces a higher ROP than the optimistic quote
    summary = recalculate_all(seeded_session)
    seeded_session.refresh(term)
    assert term.effective_lead_time_days == lt_res.effective_lead_time_days
    assert term.lead_time_std_dev == lt_res.lead_time_std_dev

    sku2_rec = next(r for r in summary.recommendations if r.sku == "SKU-002")
    assert sku2_rec.effective_lead_time_days > sku2_rec.quoted_lead_time_days
    assert sku2_rec.recommended_rop > optimistic_rec.recommended_rop
    assert sku2_rec.recommended_rop > sku2.rop
    assert sku2_rec.needs_update is True

    # SKU-013 has 0 historical closed POs (< 3 deliveries) -> falls back to quoted_lead_time_days
    sku13 = seeded_session.scalar(select(Part).where(Part.sku == "SKU-013"))
    assert sku13 is not None
    term13 = seeded_session.scalar(
        select(PartSupplierTerm).where(
            PartSupplierTerm.part_id == sku13.id, PartSupplierTerm.supplier_rank == 1
        )
    )
    assert term13 is not None
    lt13 = compute_effective_lead_time(seeded_session, term13)
    assert lt13.closed_delivery_count < 3
    assert lt13.effective_lead_time_days == float(term13.quoted_lead_time_days)


def test_z_factor_monotonic_by_criticality(seeded_session):
    part = seeded_session.scalar(select(Part).where(Part.sku == "SKU-002"))
    assert part is not None
    term = seeded_session.scalar(
        select(PartSupplierTerm).where(
            PartSupplierTerm.part_id == part.id, PartSupplierTerm.supplier_rank == 1
        )
    )
    fc = compute_demand_forecast(seeded_session, part)

    recs = {}
    for crit in (Criticality.LOW.value, Criticality.STANDARD.value, Criticality.CRITICAL.value):
        part.criticality = crit
        recs[crit] = compute_rop_recommendation(part, term, "Supplier", fc)

    assert recs[Criticality.CRITICAL.value].z_factor == Z_FACTORS[Criticality.CRITICAL.value]
    crit_rec = recs[Criticality.CRITICAL.value]
    std_rec = recs[Criticality.STANDARD.value]
    low_rec = recs[Criticality.LOW.value]
    assert crit_rec.safety_stock > std_rec.safety_stock
    assert std_rec.safety_stock > low_rec.safety_stock
    assert crit_rec.recommended_rop >= low_rec.recommended_rop


def test_planning_api_recalculate_alerts_and_apply_recommendation(seeded_session):
    app = create_app()

    def override_session():
        yield seeded_session

    app.dependency_overrides[get_session] = override_session
    client = TestClient(app)

    # 1. Read-only overview
    ov_resp = client.get("/api/planning/overview")
    assert ov_resp.status_code == 200
    ov_data = ov_resp.json()
    assert ov_data["parts_evaluated"] == 15
    assert len(ov_data["supplier_scores"]) == 3

    # 2. Recalculate persists scores/LT and raises ROP_RECOMMENDATION alerts
    recalc_resp = client.post("/api/planning/recalculate")
    assert recalc_resp.status_code == 200
    recalc_data = recalc_resp.json()
    assert recalc_data["suppliers_updated"] == 3
    assert recalc_data["recommendations_needing_update"] > 0
    assert recalc_data["alerts_created"] == recalc_data["recommendations_needing_update"]

    # Re-running recalculate must NOT duplicate open ROP_RECOMMENDATION alerts
    recalc2 = client.post("/api/planning/recalculate")
    assert recalc2.status_code == 200
    assert recalc2.json()["alerts_created"] == 0

    sku2_rec = next(r for r in recalc_data["recommendations"] if r["sku"] == "SKU-002")
    part_id = sku2_rec["part_id"]

    # 3. Apply recommendation via dedicated planning endpoint updates Part & resolves alert
    apply_resp = client.post(f"/api/planning/parts/{part_id}/apply-recommendation")
    assert apply_resp.status_code == 200
    applied = apply_resp.json()
    assert applied["rop"] == sku2_rec["recommended_rop"]
    assert applied["replenishment_point"] == sku2_rec["recommended_replenishment_point"]
    assert applied["resolved_alerts"] >= 1

    # Verify Part updated & open ROP alert resolved
    part = seeded_session.get(Part, part_id)
    assert part is not None
    assert part.rop == sku2_rec["recommended_rop"]
    assert part.replenishment_point == sku2_rec["recommended_replenishment_point"]

    open_rop_alerts = list(
        seeded_session.scalars(
            select(ActionItem).where(
                ActionItem.type == "ROP_RECOMMENDATION",
                ActionItem.part_id == part_id,
                ActionItem.status == ActionItemStatus.OPEN.value,
            )
        ).all()
    )
    assert len(open_rop_alerts) == 0

    # 4. Also verify another ROP_RECOMMENDATION alert works via 1-click alert CTA (approve_rop)
    another_alert = seeded_session.scalar(
        select(ActionItem).where(
            ActionItem.type == "ROP_RECOMMENDATION",
            ActionItem.status == ActionItemStatus.OPEN.value,
        )
    )
    if another_alert is not None:
        cta_resp = client.post(
            f"/api/alerts/{another_alert.id}/execute-cta",
            json={"action": "approve_rop"},
        )
        assert cta_resp.status_code == 200
        seeded_session.refresh(another_alert)
        assert another_alert.status == ActionItemStatus.RESOLVED.value
