from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.models import Part, PartSupplierTerm, Supplier
from backend.planning.forecast import compute_demand_forecast
from backend.planning.rop import compute_rop_recommendation
from backend.planning.schemas import (
    EffectiveLeadTimeResult,
    PlanningRecalculationSummary,
    ROPRecommendation,
    SupplierScoreResult,
)
from backend.planning.scoring import compute_effective_lead_time, compute_supplier_score


def _primary_term_for_part(session: Session, part_id: int) -> PartSupplierTerm | None:
    return session.scalar(
        select(PartSupplierTerm)
        .where(PartSupplierTerm.part_id == part_id)
        .order_by(PartSupplierTerm.supplier_rank.asc())
        .limit(1)
    )


def evaluate_part_recommendation(
    session: Session,
    part_id: int,
) -> ROPRecommendation:
    """Compute a live ROP/S recommendation for a single part without mutating DB tables."""
    part = session.get(Part, part_id)
    if part is None:
        raise ValueError(f"Part {part_id} not found")
    sim_now = clock.now(session)
    forecast = compute_demand_forecast(session, part, sim_now=sim_now)
    primary_term = _primary_term_for_part(session, part.id)
    supplier_name: str | None = None
    if primary_term is not None:
        sup = session.get(Supplier, primary_term.supplier_id)
        supplier_name = sup.name if sup is not None else None
    return compute_rop_recommendation(part, primary_term, supplier_name, forecast)


def get_planning_overview(session: Session) -> PlanningRecalculationSummary:
    """Read-only planning overview computing live scores, lead times, and ROP recommendations."""
    sim_now = clock.now(session)

    suppliers = list(session.scalars(select(Supplier).order_by(Supplier.id.asc())).all())
    suppliers_by_id = {s.id: s for s in suppliers}
    supplier_scores = [compute_supplier_score(session, s, sim_now=sim_now) for s in suppliers]

    terms = list(
        session.scalars(
            select(PartSupplierTerm).order_by(
                PartSupplierTerm.part_id.asc(), PartSupplierTerm.supplier_rank.asc()
            )
        ).all()
    )
    lt_results: list[EffectiveLeadTimeResult] = []
    lt_by_term_id: dict[int, EffectiveLeadTimeResult] = {}
    for term in terms:
        lt_res = compute_effective_lead_time(session, term, sim_now=sim_now)
        lt_results.append(lt_res)
        lt_by_term_id[term.id] = lt_res

    parts = list(session.scalars(select(Part).order_by(Part.sku.asc())).all())
    primary_by_part: dict[int, PartSupplierTerm] = {}
    for term in terms:
        primary_by_part.setdefault(term.part_id, term)

    recommendations: list[ROPRecommendation] = []
    for part in parts:
        forecast = compute_demand_forecast(session, part, sim_now=sim_now)
        primary_term = primary_by_part.get(part.id)
        sup_name = (
            suppliers_by_id[primary_term.supplier_id].name
            if primary_term is not None and primary_term.supplier_id in suppliers_by_id
            else None
        )
        # Use freshly computed effective LT & std dev without mutating the ORM object
        if primary_term is not None and primary_term.id in lt_by_term_id:
            lt_calc = lt_by_term_id[primary_term.id]
            proxy_term = PartSupplierTerm(
                id=primary_term.id,
                part_id=primary_term.part_id,
                supplier_id=primary_term.supplier_id,
                supplier_rank=primary_term.supplier_rank,
                agreed_unit_price=primary_term.agreed_unit_price,
                quoted_lead_time_days=primary_term.quoted_lead_time_days,
                effective_lead_time_days=lt_calc.effective_lead_time_days,
                lead_time_std_dev=lt_calc.lead_time_std_dev,
                moq=primary_term.moq,
                last_validated_at=primary_term.last_validated_at,
            )
        else:
            proxy_term = primary_term

        recommendations.append(compute_rop_recommendation(part, proxy_term, sup_name, forecast))

    return PlanningRecalculationSummary(
        calculated_at=sim_now,
        suppliers_updated=len(supplier_scores),
        terms_updated=len(lt_results),
        parts_evaluated=len(recommendations),
        recommendations_needing_update=sum(1 for r in recommendations if r.needs_update),
        supplier_scores=supplier_scores,
        lead_time_updates=lt_results,
        recommendations=recommendations,
    )


def recalculate_all(session: Session) -> PlanningRecalculationSummary:
    """Recalculate and persist supplier scores, effective lead times, and compute ROP targets.

    Persists updated quantitative metrics on ``suppliers`` and ``part_supplier_terms``.
    Does NOT silently overwrite ``parts.rop`` or ``parts.replenishment_point``; instead returns
    ``ROPRecommendation`` objects so managers can approve updates explicitly.
    """
    sim_now = clock.now(session)

    suppliers = list(session.scalars(select(Supplier).order_by(Supplier.id.asc())).all())
    suppliers_by_id = {s.id: s for s in suppliers}
    supplier_scores: list[SupplierScoreResult] = []
    for sup in suppliers:
        score = compute_supplier_score(session, sup, sim_now=sim_now)
        sup.reliability_score = score.reliability_score
        sup.otd_rate = score.otd_rate
        sup.invoice_accuracy_rate = score.invoice_accuracy_rate
        sup.response_rate = score.response_rate
        supplier_scores.append(score)

    terms = list(
        session.scalars(
            select(PartSupplierTerm).order_by(
                PartSupplierTerm.part_id.asc(), PartSupplierTerm.supplier_rank.asc()
            )
        ).all()
    )
    lt_results: list[EffectiveLeadTimeResult] = []
    primary_by_part: dict[int, PartSupplierTerm] = {}
    for term in terms:
        lt_res = compute_effective_lead_time(session, term, sim_now=sim_now)
        term.effective_lead_time_days = lt_res.effective_lead_time_days
        term.lead_time_std_dev = lt_res.lead_time_std_dev
        lt_results.append(lt_res)
        primary_by_part.setdefault(term.part_id, term)

    session.flush()

    parts = list(session.scalars(select(Part).order_by(Part.sku.asc())).all())
    recommendations: list[ROPRecommendation] = []
    for part in parts:
        forecast = compute_demand_forecast(session, part, sim_now=sim_now)
        primary_term = primary_by_part.get(part.id)
        sup_name = (
            suppliers_by_id[primary_term.supplier_id].name
            if primary_term is not None and primary_term.supplier_id in suppliers_by_id
            else None
        )
        recommendations.append(compute_rop_recommendation(part, primary_term, sup_name, forecast))

    return PlanningRecalculationSummary(
        calculated_at=sim_now,
        suppliers_updated=len(supplier_scores),
        terms_updated=len(lt_results),
        parts_evaluated=len(recommendations),
        recommendations_needing_update=sum(1 for r in recommendations if r.needs_update),
        supplier_scores=supplier_scores,
        lead_time_updates=lt_results,
        recommendations=recommendations,
    )
