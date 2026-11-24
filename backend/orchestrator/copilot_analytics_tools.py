import math
import statistics
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.models import Part, PartSupplierTerm, Supplier, SupplierMemoryNote
from backend.inventory import get_part_snapshot
from backend.planning import (
    build_cycle_analytics,
    compute_part_planning_metrics,
    get_planning_overview,
)

VALID_CHART_TYPES = frozenset({"bar", "grouped_bar", "line", "area", "donut", "scatter"})
VALID_DIAGRAM_TYPES = frozenset({"flow", "decision", "timeline"})
VALID_NODE_TONES = frozenset({"neutral", "info", "success", "warning", "danger"})
DEFAULT_SERIES_COLORS = ("#0066CC", "#1F7A4D", "#D97706", "#5E5CE6", "#C9342D", "#0D9488")


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_vals[lo])
    frac = pos - lo
    return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac)


def _ols_trend(values: list[float]) -> tuple[float, float]:
    n = len(values)
    if n < 2:
        return 0.0, 0.0
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    ss_xx = sum((i - mean_x) ** 2 for i in range(n))
    if ss_xx <= 1e-12:
        return 0.0, 0.0
    ss_xy = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(values))
    ss_yy = sum((y - mean_y) ** 2 for y in values)
    slope = ss_xy / ss_xx
    r_sq = (ss_xy**2) / (ss_xx * ss_yy) if ss_yy > 1e-12 else 0.0
    return round(slope, 4), round(max(0.0, min(1.0, r_sq)), 4)


def compute_series_stats(
    values: list[float],
    labels: list[str] | None = None,
) -> dict[str, Any]:
    clean = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v))]
    if not clean:
        return {"ok": False, "error": "No finite numeric values provided."}

    n = len(clean)
    s_sorted = sorted(clean)
    mean_v = statistics.mean(clean)
    median_v = statistics.median(clean)
    stdev_v = statistics.stdev(clean) if n >= 2 else 0.0
    var_v = statistics.variance(clean) if n >= 2 else 0.0
    cv_v = (stdev_v / mean_v) if mean_v > 1e-9 else 0.0

    p25 = _percentile(s_sorted, 25)
    p75 = _percentile(s_sorted, 75)
    iqr = p75 - p25
    iqr_upper = p75 + 1.5 * iqr

    outliers: list[dict[str, Any]] = []
    for idx, val in enumerate(clean):
        z = ((val - mean_v) / stdev_v) if stdev_v > 1e-9 else 0.0
        if (iqr > 0 and val > iqr_upper) or abs(z) >= 2.0:
            outliers.append(
                {
                    "index": idx,
                    "label": labels[idx] if labels and idx < len(labels) else str(idx),
                    "value": round(val, 4),
                    "z_score": round(z, 2),
                }
            )

    slope, r_sq = _ols_trend(clean)
    direction = "rising" if slope > 0.02 else ("falling" if slope < -0.02 else "flat")

    return {
        "ok": True,
        "count": n,
        "sum": round(sum(clean), 4),
        "mean": round(mean_v, 4),
        "median": round(median_v, 4),
        "stdev": round(stdev_v, 4),
        "variance": round(var_v, 4),
        "cv": round(cv_v, 4),
        "min": round(s_sorted[0], 4),
        "max": round(s_sorted[-1], 4),
        "p25": round(p25, 4),
        "p50": round(_percentile(s_sorted, 50), 4),
        "p75": round(p75, 4),
        "p90": round(_percentile(s_sorted, 90), 4),
        "p95": round(_percentile(s_sorted, 95), 4),
        "iqr_upper_fence": round(iqr_upper, 4),
        "outliers": outliers[:15],
        "ma_7": round(statistics.mean(clean[-7:]), 4),
        "ma_14": round(statistics.mean(clean[-14:]), 4),
        "ma_30": round(statistics.mean(clean[-30:]), 4),
        "trend_slope_per_step": slope,
        "trend_r_squared": r_sq,
        "trend_direction": direction,
    }


def _resolve_part_local(session: Session, sku_or_part_id: str | int) -> Part | None:
    if isinstance(sku_or_part_id, int):
        return session.get(Part, sku_or_part_id)
    raw = str(sku_or_part_id).strip()
    if not raw:
        return None
    if raw.isdigit():
        by_id = session.get(Part, int(raw))
        if by_id is not None:
            return by_id
    return session.scalar(select(Part).where(func.lower(Part.sku) == raw.lower()))


def run_statistical_analysis(
    session: Session,
    *,
    mode: str,
    part_sku_or_id: str | int | None = None,
    days: int = 90,
    urgency_filter: str | None = None,
    values: list[float] | None = None,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """Execute deterministic descriptive, Pareto, and supplier statistical analyses."""
    clean_mode = (mode or "").strip().lower()
    sim_now = clock.now(session)

    if clean_mode == "series_stats":
        return {"mode": clean_mode, **compute_series_stats(values or [], labels)}

    if clean_mode == "part_demand_stats":
        if part_sku_or_id is None:
            return {"ok": False, "error": "part_sku_or_id is required for part_demand_stats"}
        part = _resolve_part_local(session, part_sku_or_id)
        if part is None:
            return {"ok": False, "error": f"Part '{part_sku_or_id}' not found"}
        window = max(7, min(365, int(days or 90)))
        analytics = build_cycle_analytics(session, part, sim_now=sim_now)
        pts = analytics.history[-window:]
        cons = [float(p.consumption) for p in pts]
        date_labels = [p.date for p in pts]
        stats = compute_series_stats(cons, date_labels)
        zero_days = sum(1 for c in cons if c <= 0)
        weekday_vals: list[float] = []
        weekend_vals: list[float] = []
        for p in pts:
            from datetime import date as _date

            dt = _date.fromisoformat(p.date[:10])
            if dt.weekday() >= 5:
                weekend_vals.append(float(p.consumption))
            else:
                weekday_vals.append(float(p.consumption))
        last_7 = statistics.mean(cons[-7:]) if len(cons) >= 7 else statistics.mean(cons)
        prev_7 = (
            statistics.mean(cons[-14:-7])
            if len(cons) >= 14
            else (statistics.mean(cons[:-7]) if len(cons) > 7 else last_7)
        )
        return {
            "mode": clean_mode,
            "part_id": part.id,
            "sku": part.sku,
            "part_name": part.name,
            "criticality": part.criticality,
            "window_days": len(pts),
            "ewma_daily_burn": round(analytics.metrics.daily_burn_ewma, 4),
            "ewma_trend_pct": round(analytics.metrics.trend_pct, 2),
            "is_intermittent": analytics.metrics.is_intermittent,
            "zero_days": zero_days,
            "zero_days_pct": round((zero_days / len(cons)) * 100.0, 2) if cons else 0.0,
            "last_7d_avg": round(last_7, 4),
            "prev_7d_avg": round(prev_7, 4),
            "weekday_avg": round(statistics.mean(weekday_vals), 4) if weekday_vals else 0.0,
            "weekend_avg": round(statistics.mean(weekend_vals), 4) if weekend_vals else 0.0,
            **stats,
        }

    if clean_mode == "inventory_abc_pareto":
        parts = list(session.scalars(select(Part).order_by(Part.sku.asc())).all())
        terms = list(
            session.scalars(
                select(PartSupplierTerm).order_by(
                    PartSupplierTerm.part_id.asc(), PartSupplierTerm.supplier_rank.asc()
                )
            ).all()
        )
        primary_term_by_part: dict[int, PartSupplierTerm] = {}
        for t in terms:
            primary_term_by_part.setdefault(t.part_id, t)

        wanted: set[str] | None = None
        if urgency_filter and urgency_filter.strip():
            wanted = {u.strip().upper() for u in urgency_filter.split(",") if u.strip()}

        rows: list[dict[str, Any]] = []
        for part in parts:
            snap = get_part_snapshot(session, part.id, persist_dismiss_resolution=False)
            metrics = compute_part_planning_metrics(session, part, sim_now=sim_now)
            if wanted is not None and metrics.stockout_urgency not in wanted:
                continue
            term = primary_term_by_part.get(part.id)
            unit_price = float(term.agreed_unit_price) if term else 0.0
            daily_spend = metrics.daily_burn_ewma * unit_price
            rows.append(
                {
                    "part_id": part.id,
                    "sku": part.sku,
                    "name": part.name,
                    "criticality": part.criticality,
                    "stockout_urgency": metrics.stockout_urgency,
                    "on_hand_stock": snap.on_hand_stock,
                    "days_to_stockout": (
                        round(metrics.days_to_stockout, 2)
                        if metrics.days_to_stockout is not None
                        else None
                    ),
                    "daily_burn_ewma": round(metrics.daily_burn_ewma, 4),
                    "unit_price_inr": round(unit_price, 2),
                    "daily_spend_inr": round(daily_spend, 2),
                    "monthly_30d_spend_inr": round(daily_spend * 30.0, 2),
                    "on_hand_value_inr": round(snap.on_hand_stock * unit_price, 2),
                }
            )

        rows.sort(key=lambda r: r["daily_spend_inr"], reverse=True)
        total_daily_spend = sum(r["daily_spend_inr"] for r in rows)
        cum = 0.0
        abc_counts = {"A": 0, "B": 0, "C": 0}
        for r in rows:
            share = (
                (r["daily_spend_inr"] / total_daily_spend * 100.0)
                if total_daily_spend > 0
                else 0.0
            )
            cum += share
            cls = "A" if cum <= 80.0 else ("B" if cum <= 95.0 else "C")
            abc_counts[cls] += 1
            r["spend_share_pct"] = round(share, 2)
            r["cumulative_spend_pct"] = round(min(100.0, cum), 2)
            r["abc_class"] = cls

        runways = [r["days_to_stockout"] for r in rows if r["days_to_stockout"] is not None]
        return {
            "ok": True,
            "mode": clean_mode,
            "total_skus": len(rows),
            "total_daily_spend_inr": round(total_daily_spend, 2),
            "total_30d_spend_inr": round(total_daily_spend * 30.0, 2),
            "total_on_hand_value_inr": round(sum(r["on_hand_value_inr"] for r in rows), 2),
            "abc_counts": abc_counts,
            "runway_stats": compute_series_stats(runways) if runways else None,
            "items": rows,
        }

    if clean_mode == "supplier_benchmark":
        overview = get_planning_overview(session)
        notes_by_sup: dict[int, list[str]] = {}
        for note in session.scalars(
            select(SupplierMemoryNote).order_by(SupplierMemoryNote.id.asc())
        ).all():
            notes_by_sup.setdefault(note.supplier_id, []).append(note.note)

        sups_list = [
            {
                "supplier_id": s.supplier_id,
                "persona_key": s.persona_key,
                "name": s.name,
                "reliability_score": round(s.reliability_score, 2),
                "otd_rate_pct": round(s.otd_rate * 100.0, 1),
                "avg_days_late": round(s.avg_days_late, 2),
                "lead_time_std_dev": round(s.lead_time_std_dev, 2),
                "invoice_accuracy_pct": round(s.invoice_accuracy_rate * 100.0, 1),
                "response_rate_pct": round(s.response_rate * 100.0, 1),
                "avg_response_days": round(s.avg_response_days, 2),
                "cycles_90d": s.cycles_90d,
                "memory_notes": notes_by_sup.get(s.supplier_id, []),
            }
            for s in sorted(
                overview.supplier_scores, key=lambda x: x.reliability_score, reverse=True
            )
        ]

        part_comparison: dict[str, Any] | None = None
        if part_sku_or_id is not None:
            part = _resolve_part_local(session, part_sku_or_id)
            if part is not None:
                terms = list(
                    session.scalars(
                        select(PartSupplierTerm)
                        .where(PartSupplierTerm.part_id == part.id)
                        .order_by(PartSupplierTerm.supplier_rank.asc())
                    ).all()
                )
                sups_map = {s.id: s for s in session.scalars(select(Supplier)).all()}
                term_rows = [
                    {
                        "supplier_rank": t.supplier_rank,
                        "supplier_id": t.supplier_id,
                        "supplier_name": sups_map[t.supplier_id].name
                        if t.supplier_id in sups_map
                        else None,
                        "persona_key": sups_map[t.supplier_id].persona_key
                        if t.supplier_id in sups_map
                        else None,
                        "agreed_unit_price_inr": t.agreed_unit_price,
                        "quoted_lead_time_days": t.quoted_lead_time_days,
                        "effective_lead_time_days": round(t.effective_lead_time_days, 2),
                        "lt_overrun_days": round(
                            t.effective_lead_time_days - t.quoted_lead_time_days, 2
                        ),
                        "moq": t.moq,
                    }
                    for t in terms
                ]
                deltas: dict[str, Any] = {}
                if len(term_rows) >= 2:
                    p0, p1 = term_rows[0], term_rows[1]
                    deltas = {
                        "backup_price_diff_inr": round(
                            p1["agreed_unit_price_inr"] - p0["agreed_unit_price_inr"], 2
                        ),
                        "backup_lt_savings_days": round(
                            p0["effective_lead_time_days"] - p1["effective_lead_time_days"], 2
                        ),
                    }
                part_comparison = {"sku": part.sku, "terms": term_rows, **deltas}

        return {
            "ok": True,
            "mode": clean_mode,
            "suppliers": sups_list,
            "part_comparison": part_comparison,
        }

    return {
        "ok": False,
        "error": (
            f"Unsupported statistical analysis mode '{mode}'. "
            "Use one of: part_demand_stats, inventory_abc_pareto, supplier_benchmark, series_stats."
        ),
    }


def validate_and_build_chart(
    *,
    chart_type: str,
    title: str,
    labels: list[str],
    series: list[dict[str, Any]],
    subtitle: str = "",
    x_label: str = "",
    y_label: str = "",
    unit: str = "",
    reference_lines: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ctype = (chart_type or "").strip().lower()
    if ctype not in VALID_CHART_TYPES:
        raise ValueError(
            f"Unsupported chart_type '{chart_type}'. Must be one of {sorted(VALID_CHART_TYPES)}."
        )
    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("Chart title must not be empty.")
    clean_labels = [str(lbl) for lbl in (labels or [])][:120]
    if not clean_labels:
        raise ValueError("Chart labels must not be empty.")
    if not series:
        raise ValueError("Chart requires at least one data series.")

    clean_series: list[dict[str, Any]] = []
    for idx, s in enumerate(series[:8]):
        s_name = str(s.get("name") or f"Series {idx + 1}").strip()
        raw_vals = list(s.get("values") or [])
        if len(raw_vals) != len(clean_labels):
            raise ValueError(
                f"Series '{s_name}' has {len(raw_vals)} values, expected {len(clean_labels)}."
            )
        vals: list[float] = []
        for v in raw_vals:
            f_val = float(v)
            if not math.isfinite(f_val):
                raise ValueError(f"Non-finite value in series '{s_name}'.")
            vals.append(round(f_val, 4))
        color = str(s.get("color") or DEFAULT_SERIES_COLORS[idx % len(DEFAULT_SERIES_COLORS)])
        clean_series.append({"name": s_name, "values": vals, "color": color})

    clean_refs: list[dict[str, Any]] = []
    for ref in reference_lines or []:
        r_label = str(ref.get("label") or "").strip()
        r_val = float(ref.get("value"))
        if not math.isfinite(r_val):
            raise ValueError("Reference line value must be finite.")
        clean_refs.append(
            {
                "label": r_label or f"{r_val}",
                "value": round(r_val, 4),
                "color": str(ref.get("color") or "#D96B00"),
            }
        )

    spec = {
        "chart_type": ctype,
        "title": clean_title,
        "subtitle": (subtitle or "").strip(),
        "x_label": (x_label or "").strip(),
        "y_label": (y_label or "").strip(),
        "unit": (unit or "").strip(),
        "labels": clean_labels,
        "series": clean_series,
        "reference_lines": clean_refs,
    }
    return {"rendered": True, "kind": "chart", "title": clean_title, "spec": spec}


def validate_and_build_diagram(
    *,
    diagram_type: str,
    title: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]] | None = None,
    subtitle: str = "",
) -> dict[str, Any]:
    dtype = (diagram_type or "").strip().lower()
    if dtype not in VALID_DIAGRAM_TYPES:
        raise ValueError(
            f"Unsupported diagram_type '{diagram_type}'. "
            f"Must be one of {sorted(VALID_DIAGRAM_TYPES)}."
        )
    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("Diagram title must not be empty.")
    if not nodes:
        raise ValueError("Diagram requires at least one node.")

    clean_nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    for n in nodes[:30]:
        nid = str(n.get("id") or "").strip()
        nlabel = str(n.get("label") or "").strip()
        if not nid or not nlabel:
            raise ValueError("Each diagram node requires non-empty 'id' and 'label'.")
        if nid in node_ids:
            raise ValueError(f"Duplicate diagram node id '{nid}'.")
        node_ids.add(nid)
        tone = str(n.get("tone") or "neutral").strip().lower()
        if tone not in VALID_NODE_TONES:
            tone = "neutral"
        clean_nodes.append(
            {
                "id": nid,
                "label": nlabel,
                "detail": str(n.get("detail") or "").strip(),
                "tone": tone,
            }
        )

    clean_edges: list[dict[str, Any]] = []
    for e in edges or []:
        src = str(e.get("source") or "").strip()
        dst = str(e.get("target") or "").strip()
        if src not in node_ids or dst not in node_ids:
            raise ValueError(f"Edge ({src} -> {dst}) references an unknown node id.")
        clean_edges.append(
            {
                "source": src,
                "target": dst,
                "label": str(e.get("label") or "").strip(),
            }
        )

    spec = {
        "diagram_type": dtype,
        "title": clean_title,
        "subtitle": (subtitle or "").strip(),
        "nodes": clean_nodes,
        "edges": clean_edges,
    }
    return {"rendered": True, "kind": "diagram", "title": clean_title, "spec": spec}
