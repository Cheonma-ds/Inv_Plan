import statistics
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import InvoiceMatchStatus, POStatus
from backend.core.models import (
    GoodsReceipt,
    Invoice,
    PartSupplierTerm,
    PurchaseOrder,
    Supplier,
)
from backend.planning.schemas import EffectiveLeadTimeResult, SupplierScoreResult

ROLLING_WINDOW_DAYS = 365
MIN_DELIVERIES_FOR_EFFECTIVE_LT = 3
OTD_WEIGHT = 40.0
LT_CONSISTENCY_WEIGHT = 25.0
INVOICE_ACCURACY_WEIGHT = 20.0
RESPONSIVENESS_WEIGHT = 15.0
LATE_PENALTY_PER_DAY = 2.0
LATE_PENALTY_CAP = 20.0


def _actual_lead_time_days(po: PurchaseOrder, grn: GoodsReceipt) -> float | None:
    anchor = po.sent_at or po.created_at
    if anchor is None or grn.received_at is None:
        return None
    elapsed = clock.ensure_utc(grn.received_at) - clock.ensure_utc(anchor)
    delta_days = elapsed.total_seconds() / 86400.0
    return max(0.0, delta_days)


def _quoted_lead_time_for_po(
    po: PurchaseOrder, term_lookup: dict[tuple[int, int], int]
) -> float:
    if po.sent_at is not None and po.promised_eta is not None:
        days = (
            clock.ensure_utc(po.promised_eta) - clock.ensure_utc(po.sent_at)
        ).total_seconds() / 86400.0
        if days > 0:
            return days
    return float(term_lookup.get((po.part_id, po.supplier_id), 14))


def compute_supplier_score(
    session: Session,
    supplier: Supplier,
    *,
    sim_now: datetime | None = None,
) -> SupplierScoreResult:
    """Compute weighted 0-100 Supplier Reliability Score across rolling 12-month history.

    Weights (DESIGN.md §7):
      - OTD Rate (40%): % delivered <= promised_eta, penalized -2 pts/day avg late (capped).
      - Lead Time Consistency (25%): based on sigma_L relative to quoted lead time.
      - Invoice Accuracy (20%): % of POs matched on invoice v1 without quarantine.
      - Responsiveness (15%): % of POs acknowledged without needing follow-up nudges.
    """
    now = clock.ensure_utc(sim_now) if sim_now is not None else clock.now(session)
    cutoff = now - timedelta(days=ROLLING_WINDOW_DAYS)

    pos = list(
        session.scalars(
            select(PurchaseOrder)
            .where(
                PurchaseOrder.supplier_id == supplier.id,
                PurchaseOrder.created_at >= cutoff,
                PurchaseOrder.created_at <= now,
            )
            .order_by(PurchaseOrder.created_at.asc())
        ).all()
    )

    if not pos:
        return SupplierScoreResult(
            supplier_id=supplier.id,
            persona_key=supplier.persona_key,
            name=supplier.name,
            closed_po_count=0,
            otd_rate=supplier.otd_rate,
            avg_days_late=0.0,
            otd_component=round(supplier.otd_rate * OTD_WEIGHT, 2),
            lead_time_std_dev=0.0,
            lead_time_consistency_component=LT_CONSISTENCY_WEIGHT,
            invoice_accuracy_rate=supplier.invoice_accuracy_rate,
            invoice_accuracy_component=round(
                supplier.invoice_accuracy_rate * INVOICE_ACCURACY_WEIGHT, 2
            ),
            response_rate=supplier.response_rate,
            responsiveness_component=round(supplier.response_rate * RESPONSIVENESS_WEIGHT, 2),
            reliability_score=round(supplier.reliability_score, 1),
        )

    po_ids = [po.id for po in pos]
    grns_by_po: dict[int, GoodsReceipt] = {}
    for grn in session.scalars(
        select(GoodsReceipt)
        .where(GoodsReceipt.po_id.in_(po_ids))
        .order_by(GoodsReceipt.received_at.asc())
    ).all():
        # Earliest receipt represents delivery arrival
        grns_by_po.setdefault(grn.po_id, grn)

    invoices_by_po: dict[int, list[Invoice]] = {pid: [] for pid in po_ids}
    for inv in session.scalars(
        select(Invoice)
        .where(Invoice.po_id.in_(po_ids))
        .order_by(Invoice.version.asc(), Invoice.id.asc())
    ).all():
        invoices_by_po[inv.po_id].append(inv)

    terms = list(
        session.scalars(
            select(PartSupplierTerm).where(PartSupplierTerm.supplier_id == supplier.id)
        ).all()
    )
    term_lookup = {(t.part_id, t.supplier_id): t.quoted_lead_time_days for t in terms}

    # 1. OTD Rate (40%) & 2. Lead Time Consistency (25%) over delivered / overdue POs
    otd_evaluated_count = 0
    on_time_count = 0
    lateness_days: list[float] = []
    lt_deltas: list[float] = []
    quoted_lts: list[float] = []

    for po in pos:
        grn = grns_by_po.get(po.id)
        if grn is None:
            if po.status == POStatus.OVERDUE.value:
                otd_evaluated_count += 1
                eta_anchor = po.promised_eta or po.revised_eta
                if eta_anchor is not None:
                    raw_late = (
                        now - clock.ensure_utc(eta_anchor)
                    ).total_seconds() / 86400.0
                    late_days = max(1.0, raw_late)
                else:
                    late_days = 1.0
                lateness_days.append(late_days)
            continue

        otd_evaluated_count += 1
        actual_lt = _actual_lead_time_days(po, grn)
        if actual_lt is not None:
            quoted_lt = _quoted_lead_time_for_po(po, term_lookup)
            lt_deltas.append(actual_lt - quoted_lt)
            quoted_lts.append(quoted_lt)
        if po.promised_eta is not None:
            late_days = (
                clock.ensure_utc(grn.received_at) - clock.ensure_utc(po.promised_eta)
            ).total_seconds() / 86400.0
            if late_days <= 0.0:
                on_time_count += 1
                lateness_days.append(0.0)
            else:
                lateness_days.append(late_days)

    if otd_evaluated_count > 0:
        otd_rate = on_time_count / otd_evaluated_count
        avg_days_late = sum(lateness_days) / len(lateness_days) if lateness_days else 0.0
    else:
        otd_rate = 1.0
        avg_days_late = 0.0

    late_penalty = min(LATE_PENALTY_CAP, avg_days_late * LATE_PENALTY_PER_DAY)
    otd_component = max(0.0, (otd_rate * OTD_WEIGHT) - late_penalty)

    lt_std_dev = statistics.stdev(lt_deltas) if len(lt_deltas) >= 2 else 0.0
    avg_quoted_lt = (sum(quoted_lts) / len(quoted_lts)) if quoted_lts else 14.0
    cv_ratio = lt_std_dev / max(avg_quoted_lt, 1.0)
    # cv_ratio = 0 -> full 25 pts; cv_ratio >= 0.5 -> 0 pts
    lt_consistency_component = LT_CONSISTENCY_WEIGHT * max(0.0, 1.0 - min(1.0, cv_ratio * 2.0))

    # 3. Invoice Accuracy (20%): % of invoiced POs matched cleanly on v1 without quarantine
    invoiced_pos = [pid for pid, invs in invoices_by_po.items() if invs]
    if invoiced_pos:
        clean_v1 = 0
        for pid in invoiced_pos:
            invs = invoices_by_po[pid]
            v1 = next((i for i in invs if i.version == 1), invs[0])
            if v1.match_status == InvoiceMatchStatus.MATCHED.value and len(invs) == 1:
                clean_v1 += 1
        invoice_accuracy_rate = clean_v1 / len(invoiced_pos)
    else:
        invoice_accuracy_rate = 1.0
    invoice_accuracy_component = invoice_accuracy_rate * INVOICE_ACCURACY_WEIGHT

    # 4. Responsiveness (15%): % of dispatched POs acknowledged with zero follow-up nudges
    dispatched_pos = [
        po
        for po in pos
        if po.sent_at is not None or po.status != POStatus.PENDING_DISPATCH_APPROVAL.value
    ]
    if dispatched_pos:
        no_nudge_count = sum(1 for po in dispatched_pos if po.follow_up_count == 0)
        response_rate = no_nudge_count / len(dispatched_pos)
    else:
        response_rate = 1.0
    responsiveness_component = response_rate * RESPONSIVENESS_WEIGHT

    total_score = max(
        0.0,
        min(
            100.0,
            otd_component
            + lt_consistency_component
            + invoice_accuracy_component
            + responsiveness_component,
        ),
    )

    closed_po_count = sum(1 for po in pos if po.status == POStatus.CLOSED.value)

    return SupplierScoreResult(
        supplier_id=supplier.id,
        persona_key=supplier.persona_key,
        name=supplier.name,
        closed_po_count=closed_po_count,
        otd_rate=round(otd_rate, 4),
        avg_days_late=round(avg_days_late, 2),
        otd_component=round(otd_component, 2),
        lead_time_std_dev=round(lt_std_dev, 2),
        lead_time_consistency_component=round(lt_consistency_component, 2),
        invoice_accuracy_rate=round(invoice_accuracy_rate, 4),
        invoice_accuracy_component=round(invoice_accuracy_component, 2),
        response_rate=round(response_rate, 4),
        responsiveness_component=round(responsiveness_component, 2),
        reliability_score=round(total_score, 1),
    )


def compute_effective_lead_time(
    session: Session,
    term: PartSupplierTerm,
    *,
    sim_now: datetime | None = None,
) -> EffectiveLeadTimeResult:
    """Compute Effective Lead Time and lead time std dev for a (part, supplier) term.

    Rule (DESIGN.md §7):
      - If < 3 closed deliveries -> ``quoted_lead_time_days``.
      - If >= 3 closed deliveries -> ``max(actual_mean, 0.25 * quoted + 0.75 * actual_mean)``
        so chronically late suppliers cannot hide behind optimistic quotes.
    """
    now = clock.ensure_utc(sim_now) if sim_now is not None else clock.now(session)
    cutoff = now - timedelta(days=ROLLING_WINDOW_DAYS)

    closed_pos = list(
        session.scalars(
            select(PurchaseOrder)
            .where(
                PurchaseOrder.part_id == term.part_id,
                PurchaseOrder.supplier_id == term.supplier_id,
                PurchaseOrder.status == POStatus.CLOSED.value,
                PurchaseOrder.created_at >= cutoff,
                PurchaseOrder.created_at <= now,
            )
            .order_by(PurchaseOrder.created_at.asc())
        ).all()
    )

    actual_lts: list[float] = []
    if closed_pos:
        po_ids = [po.id for po in closed_pos]
        grns_by_po: dict[int, GoodsReceipt] = {}
        for grn in session.scalars(
            select(GoodsReceipt)
            .where(GoodsReceipt.po_id.in_(po_ids))
            .order_by(GoodsReceipt.received_at.asc())
        ).all():
            grns_by_po.setdefault(grn.po_id, grn)

        for po in closed_pos:
            grn = grns_by_po.get(po.id)
            if grn is None:
                continue
            lt_days = _actual_lead_time_days(po, grn)
            if lt_days is not None:
                actual_lts.append(lt_days)

    delivery_count = len(actual_lts)
    quoted = float(term.quoted_lead_time_days)

    if delivery_count < MIN_DELIVERIES_FOR_EFFECTIVE_LT:
        actual_mean = round(sum(actual_lts) / delivery_count, 2) if delivery_count > 0 else None
        effective_lt = quoted
        lt_std = round(statistics.stdev(actual_lts), 2) if delivery_count >= 2 else 0.0
    else:
        raw_mean = sum(actual_lts) / delivery_count
        blended = 0.25 * quoted + 0.75 * raw_mean
        effective_lt = round(max(raw_mean, blended), 2)
        actual_mean = round(raw_mean, 2)
        lt_std = round(statistics.stdev(actual_lts), 2)

    return EffectiveLeadTimeResult(
        term_id=term.id,
        part_id=term.part_id,
        supplier_id=term.supplier_id,
        supplier_rank=term.supplier_rank,
        quoted_lead_time_days=term.quoted_lead_time_days,
        closed_delivery_count=delivery_count,
        actual_mean_lead_time_days=actual_mean,
        effective_lead_time_days=effective_lt,
        lead_time_std_dev=lt_std,
    )
