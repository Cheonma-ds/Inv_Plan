import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.agents.sandbox import run_sandbox_python
from backend.agents.tools import ToolRegistry
from backend.core import clock
from backend.core.enums import (
    ActionItemStatus,
    AlertSeverity,
    Criticality,
    DispatchDetailsStatus,
    LexReminderStatus,
    POStatus,
    SupplierMemorySource,
)
from backend.core.models import (
    ActionItem,
    GoodsReceipt,
    InventoryTransaction,
    Invoice,
    LexReminder,
    Part,
    PartSupplierTerm,
    POCommunication,
    PurchaseOrder,
    Supplier,
    SupplierMemoryNote,
)
from backend.inventory import get_part_snapshot
from backend.mailroom import get_thread
from backend.planning import (
    build_cycle_analytics,
    compute_part_planning_metrics,
    compute_supplier_score,
)
from backend.po import create_action_item

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "with",
    }
)


@dataclass
class LexToolTracker:
    """Tracks side effects executed via Lex tool calls within a single workflow turn."""

    scheduled_reminders_by_po: dict[int, int] = field(default_factory=dict)
    created_memory_note_ids: list[int] = field(default_factory=list)
    created_alert_ids: list[int] = field(default_factory=list)
    handled_memory_texts: set[str] = field(default_factory=set)


def parse_iso_date_utc(date_str: str | None) -> datetime | None:
    """Parse an ISO date or timestamp string into a UTC-aware datetime (date-only -> 07:30Z)."""
    if not date_str:
        return None
    try:
        cleaned = date_str.strip()
        if len(cleaned) == 10:
            dt = datetime.fromisoformat(f"{cleaned}T07:30:00+00:00")
        else:
            dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _normalize_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _meaningful_tokens(text: str) -> set[str]:
    return {t for t in _normalize_text(text).split() if t and t not in _STOPWORDS}


def _token_jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def _resolve_part(session: Session, sku_or_part_id: str | int) -> Part | None:
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


def _resolve_po(session: Session, po_number_or_id: str | int) -> PurchaseOrder | None:
    if isinstance(po_number_or_id, int):
        return session.get(PurchaseOrder, po_number_or_id)
    raw = str(po_number_or_id).strip()
    if not raw:
        return None
    if raw.isdigit():
        by_id = session.get(PurchaseOrder, int(raw))
        if by_id is not None:
            return by_id
    return session.scalar(
        select(PurchaseOrder).where(func.lower(PurchaseOrder.po_number) == raw.lower())
    )


def _resolve_supplier(session: Session, supplier_key_or_id: str | int) -> Supplier | None:
    if isinstance(supplier_key_or_id, int):
        return session.get(Supplier, supplier_key_or_id)
    raw = str(supplier_key_or_id).strip()
    if not raw:
        return None
    if raw.isdigit():
        by_id = session.get(Supplier, int(raw))
        if by_id is not None:
            return by_id
    by_key = session.scalar(select(Supplier).where(func.lower(Supplier.persona_key) == raw.lower()))
    if by_key is not None:
        return by_key
    return session.scalar(select(Supplier).where(func.lower(Supplier.name) == raw.lower()))


def _build_contextual_memory_alert(
    session: Session,
    *,
    supplier: Supplier,
    note_row: SupplierMemoryNote,
    po: PurchaseOrder | None,
    comm_id: int | None,
    reasoning: str = "",
) -> ActionItem:
    po_ref = f" ({po.po_number})" if po is not None else ""
    part = session.get(Part, po.part_id) if po is not None else None
    risk_keywords = ("late", "delay", "mismatch", "price", "hike", "slow", "damage", "sla")
    has_risk_signal = any(k in note_row.note.lower() for k in risk_keywords)
    severity = (
        AlertSeverity.MEDIUM
        if (has_risk_signal and part is not None and part.criticality == Criticality.CRITICAL.value)
        else AlertSeverity.INFO
    )
    context_clause = (
        f" [Supplier reliability: {supplier.reliability_score:.1f}/100, "
        f"OTD: {supplier.otd_rate:.0%}, Invoice accuracy: {supplier.invoice_accuracy_rate:.0%}]"
    )
    source_clause = f"Learned from email #{comm_id}" if comm_id is not None else "Recorded by Lex"
    reason_clause = f" Rationale: {reasoning.strip()}." if reasoning.strip() else ""
    return create_action_item(
        session,
        type="SUPPLIER_MEMORY_LEARNED",
        severity=severity,
        part_criticality=part.criticality if part is not None else None,
        title=f"Lex saved supplier memory note for {supplier.name}{po_ref}",
        summary=f'{source_clause}: "{note_row.note}".{reason_clause}{context_clause}',
        po_id=po.id if po is not None else None,
        part_id=po.part_id if po is not None else None,
        suggested_actions=[],
        cta_payload={
            "supplier_id": supplier.id,
            "memory_note_id": note_row.id,
            "note": note_row.note,
            "po_id": po.id if po is not None else None,
            "comm_id": comm_id,
            "reasoning": reasoning.strip(),
            "reliability_score": supplier.reliability_score,
        },
    )


def propose_and_save_memory_note(
    session: Session,
    *,
    supplier_id: int,
    note: str | None,
    reasoning: str = "",
    po: PurchaseOrder | None = None,
    comm_id: int | None = None,
    create_alert: bool = False,
    tracker: LexToolTracker | None = None,
) -> dict[str, Any]:
    """Deduplicate/consolidate a proposed SupplierMemoryNote against existing supplier notes."""
    if not note or not note.strip():
        return {"action": "ignored", "saved": False, "reason": "Empty note"}

    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise ValueError(f"Supplier {supplier_id} not found")

    cleaned = note.strip()
    norm_new = _normalize_text(cleaned)
    tokens_new = _meaningful_tokens(cleaned)
    existing_rows = list(
        session.scalars(
            select(SupplierMemoryNote)
            .where(SupplierMemoryNote.supplier_id == supplier.id)
            .order_by(SupplierMemoryNote.id.asc())
        ).all()
    )

    sim_now = clock.now(session)
    for row in existing_rows:
        norm_old = _normalize_text(row.note)
        tokens_old = _meaningful_tokens(row.note)
        if norm_old == norm_new:
            if tracker is not None:
                tracker.handled_memory_texts.add(cleaned.lower())
            return {
                "action": "deduplicated",
                "saved": False,
                "existing_note_id": row.id,
                "note": row.note,
                "reason": "Exact duplicate of existing supplier memory note.",
            }

        jaccard = _token_jaccard(tokens_new, tokens_old)
        is_substring = bool(
            norm_old
            and norm_new
            and min(len(tokens_old), len(tokens_new)) >= 3
            and (norm_old in norm_new or norm_new in norm_old)
        )
        if jaccard >= 0.65 or is_substring:
            # Consolidate only if existing note is LEX_LEARNED and new note adds meaningful detail
            if (
                row.source == SupplierMemorySource.LEX_LEARNED.value
                and len(cleaned) > len(row.note) + 12
                and tokens_old.issubset(tokens_new)
            ):
                row.note = cleaned
                row.updated_at = sim_now
                session.flush()
                if tracker is not None:
                    tracker.handled_memory_texts.add(cleaned.lower())
                return {
                    "action": "consolidated",
                    "saved": True,
                    "note_id": row.id,
                    "note": row.note,
                    "reason": "Consolidated expanded detail into existing Lex memory note.",
                }
            if tracker is not None:
                tracker.handled_memory_texts.add(cleaned.lower())
            return {
                "action": "deduplicated",
                "saved": False,
                "existing_note_id": row.id,
                "note": row.note,
                "reason": (
                    f"Overlaps existing memory note #{row.id} "
                    f"(similarity={jaccard:.2f}); skipped duplicate."
                ),
            }

    note_row = SupplierMemoryNote(
        supplier_id=supplier.id,
        note=cleaned,
        source=SupplierMemorySource.LEX_LEARNED.value,
        created_at=sim_now,
        updated_at=sim_now,
    )
    session.add(note_row)
    session.flush()
    if tracker is not None:
        tracker.created_memory_note_ids.append(note_row.id)
        tracker.handled_memory_texts.add(cleaned.lower())

    alert_id: int | None = None
    if create_alert:
        alert = _build_contextual_memory_alert(
            session,
            supplier=supplier,
            note_row=note_row,
            po=po,
            comm_id=comm_id,
            reasoning=reasoning,
        )
        alert_id = alert.id
        if tracker is not None:
            tracker.created_alert_ids.append(alert.id)

    return {
        "action": "created",
        "saved": True,
        "note_id": note_row.id,
        "supplier_id": supplier.id,
        "note": note_row.note,
        "alert_id": alert_id,
    }


def create_grounded_advisory_alert(
    session: Session,
    *,
    title: str,
    summary: str,
    severity: str = "INFO",
    po_id: int | None = None,
    part_id: int | None = None,
    advisory_kind: str = "general",
    cta_payload: dict[str, Any] | None = None,
    sim_now: datetime | None = None,
    tracker: LexToolTracker | None = None,
) -> dict[str, Any]:
    """Create a deduplicated Manager advisory alert grounded in PO/part/memory context."""
    clean_title = title.strip()
    clean_summary = summary.strip()
    if not clean_title or not clean_summary:
        raise ValueError("Advisory alert requires non-empty title and summary")

    sev_upper = severity.strip().upper()
    try:
        sev_enum = AlertSeverity(sev_upper)
    except ValueError:
        sev_enum = AlertSeverity.INFO

    po: PurchaseOrder | None = session.get(PurchaseOrder, int(po_id)) if po_id else None
    resolved_part_id = part_id if part_id is not None else (po.part_id if po is not None else None)
    part: Part | None = session.get(Part, int(resolved_part_id)) if resolved_part_id else None

    days_to_stockout: float | None = None
    if part is not None:
        eval_now = sim_now or clock.now(session)
        try:
            metrics = compute_part_planning_metrics(session, part, sim_now=eval_now)
            days_to_stockout = metrics.days_to_stockout
        except OverflowError:
            if part.on_hand_stock <= 0:
                days_to_stockout = 0.0
            elif part.daily_burn_ewma > 0.0:
                days_to_stockout = float(part.on_hand_stock) / float(part.daily_burn_ewma)

    # Deduplicate against existing OPEN advisory alerts on same PO / part
    open_stmt = select(ActionItem).where(
        ActionItem.type == "LEX_ADVISORY_ALERT",
        ActionItem.status == ActionItemStatus.OPEN.value,
    )
    if po is not None:
        open_stmt = open_stmt.where(ActionItem.po_id == po.id)
    elif part is not None:
        open_stmt = open_stmt.where(ActionItem.part_id == part.id)

    title_tokens = _meaningful_tokens(clean_title)
    for existing in session.scalars(open_stmt).all():
        existing_kind = (existing.cta_payload_json or {}).get("advisory_kind")
        if (
            (advisory_kind != "general" and existing_kind == advisory_kind)
            or _normalize_text(existing.title) == _normalize_text(clean_title)
            or _token_jaccard(title_tokens, _meaningful_tokens(existing.title)) >= 0.70
        ):
            return {
                "created": False,
                "deduplicated": True,
                "alert_id": existing.id,
                "title": existing.title,
                "severity": existing.severity,
            }

    payload = {
        "advisory_kind": advisory_kind,
        "po_id": po.id if po is not None else None,
        "part_id": part.id if part is not None else None,
        **(cta_payload or {}),
    }
    alert = create_action_item(
        session,
        type="LEX_ADVISORY_ALERT",
        severity=sev_enum,
        part_criticality=part.criticality if part is not None else None,
        days_to_stockout=days_to_stockout,
        title=clean_title[:250],
        summary=clean_summary,
        po_id=po.id if po is not None else None,
        part_id=part.id if part is not None else None,
        suggested_actions=[{"action": "add_note", "label": "Acknowledge / Log Note"}],
        cta_payload=payload,
    )
    if tracker is not None:
        tracker.created_alert_ids.append(alert.id)
    return {
        "created": True,
        "deduplicated": False,
        "alert_id": alert.id,
        "title": alert.title,
        "severity": alert.severity,
    }


def schedule_po_lex_reminder(
    session: Session,
    *,
    po_id: int,
    remind_at: datetime,
    reason: str,
    suggested_action: str = "",
    comm_id: int | None = None,
    context_extra: dict[str, Any] | None = None,
    tracker: LexToolTracker | None = None,
) -> dict[str, Any]:
    """Insert or update a pending LexReminder and sync PO.dispatch_details_due_at."""
    po = session.get(PurchaseOrder, int(po_id))
    if po is None:
        raise ValueError(f"PurchaseOrder {po_id} not found")
    if po.status in (POStatus.CLOSED.value, POStatus.CANCELLED.value):
        raise ValueError(f"Cannot schedule reminder on {po.status} PO {po.po_number}")

    sim_now = clock.now(session)
    remind_utc = clock.ensure_utc(remind_at)
    clean_reason = reason.strip() or f"Follow up on {po.po_number}"
    clean_action = suggested_action.strip()

    kind = (context_extra or {}).get("kind", "dispatch_details_followup")
    existing = None
    pending_rows = list(
        session.scalars(
            select(LexReminder)
            .where(
                LexReminder.po_id == po.id,
                LexReminder.status == LexReminderStatus.PENDING.value,
            )
            .order_by(LexReminder.id.desc())
        ).all()
    )
    for cand in pending_rows:
        cand_kind = (cand.context_json or {}).get("kind")
        if cand_kind == kind or cand.remind_at == remind_utc:
            existing = cand
            break

    context_payload: dict[str, Any] = {
        "kind": kind,
        "suggested_action": clean_action,
        **(context_extra or {}),
    }

    if existing is not None:
        existing.remind_at = remind_utc
        existing.reason = clean_reason
        if comm_id is not None:
            existing.comm_id = comm_id
        existing.context_json = {**(existing.context_json or {}), **context_payload}
        row = existing
        action = "updated"
    else:
        row = LexReminder(
            po_id=po.id,
            part_id=po.part_id,
            supplier_id=po.supplier_id,
            comm_id=comm_id,
            remind_at=remind_utc,
            reason=clean_reason,
            context_json=context_payload,
            status=LexReminderStatus.PENDING.value,
            created_at=sim_now,
        )
        session.add(row)
        action = "scheduled"

    # Sync PO.dispatch_details_due_at dynamically whenever dispatch details are deferred/missing
    if (
        po.dispatch_details_status == DispatchDetailsStatus.DEFERRED_2D.value
        or bool(po.missing_dispatch_fields_json)
    ) and po.dispatch_details_status != DispatchDetailsStatus.COMPLETE.value:
        po.dispatch_details_due_at = remind_utc

    session.flush()
    if tracker is not None:
        tracker.scheduled_reminders_by_po[po.id] = row.id

    return {
        "action": action,
        "reminder_id": row.id,
        "po_id": po.id,
        "po_number": po.po_number,
        "remind_at": clock.ensure_utc(row.remind_at).isoformat(),
        "status": row.status,
        "reason": row.reason,
    }


def cancel_pending_lex_reminders_for_po(
    session: Session,
    po_id: int,
    *,
    reason: str = "",
) -> int:
    """Cancel any PENDING LexReminder rows for a PO when resolved/cancelled/delivered."""
    rows = list(
        session.scalars(
            select(LexReminder).where(
                LexReminder.po_id == po_id,
                LexReminder.status == LexReminderStatus.PENDING.value,
            )
        ).all()
    )
    for row in rows:
        row.status = LexReminderStatus.CANCELLED.value
        if reason:
            row.context_json = {**(row.context_json or {}), "cancel_reason": reason}
    if rows:
        session.flush()
    return len(rows)


def evaluate_memory_advisory_alerts(
    session: Session,
    *,
    po: PurchaseOrder,
    part: Part,
    supplier: Supplier,
    trigger_stage: str,
    sim_now: datetime | None = None,
    tracker: LexToolTracker | None = None,
) -> ActionItem | None:
    """Surface contextual, deduplicated memory advisories when history flags risk."""
    notes = list(
        session.scalars(
            select(SupplierMemoryNote.note)
            .where(SupplierMemoryNote.supplier_id == supplier.id)
            .order_by(SupplierMemoryNote.id.asc())
        ).all()
    )
    notes_lower = [n.lower() for n in notes]

    if trigger_stage == "pre_delivery":
        invoice_habit_notes = [
            n
            for n, nl in zip(notes, notes_lower)
            if any(
                kw in nl
                for kw in (
                    "invoice",
                    "mismatch",
                    "pdf",
                    "billing",
                    "overcharge",
                    "discrepancy",
                    "unit prices on",
                )
            )
        ]
        if not invoice_habit_notes and supplier.invoice_accuracy_rate >= 0.85:
            return None
        cited = (
            f'Memory note: "{invoice_habit_notes[0]}". '
            if invoice_habit_notes
            else ""
        )
        sev = (
            AlertSeverity.MEDIUM.value
            if part.criticality == Criticality.CRITICAL.value
            else AlertSeverity.INFO.value
        )
        res = create_grounded_advisory_alert(
            session,
            title=f"Memory advisory: verify invoice pricing on {po.po_number} ({part.sku})",
            summary=(
                f"{supplier.name} has an invoice accuracy rate of "
                f"{supplier.invoice_accuracy_rate:.0%}. {cited}"
                f"Verify unit prices on the upcoming PDF invoice against contracted "
                f"₹{po.unit_price:,.2f}/unit ({po.qty} units) before releasing stock."
            ),
            severity=sev,
            po_id=po.id,
            part_id=part.id,
            advisory_kind="invoice_mismatch_watch",
            cta_payload={
                "supplier_id": supplier.id,
                "invoice_accuracy_rate": supplier.invoice_accuracy_rate,
                "matched_notes": invoice_habit_notes,
            },
            sim_now=sim_now,
            tracker=tracker,
        )
        if res.get("created"):
            return session.get(ActionItem, res["alert_id"])
        return None

    if trigger_stage == "po_created":
        if part.criticality != Criticality.CRITICAL.value:
            return None
        risk_notes = [
            n
            for n, nl in zip(notes, notes_lower)
            if any(
                kw in nl
                for kw in (
                    "price hike",
                    "price",
                    "slow",
                    "48h",
                    "late",
                    "delay",
                    "chronically",
                    "unresponsive",
                )
            )
        ]
        if not risk_notes and supplier.reliability_score >= 75.0:
            return None
        cited = f'Supplier memory: "{risk_notes[0]}". ' if risk_notes else ""
        res = create_grounded_advisory_alert(
            session,
            title=f"Memory advisory: supplier risk on critical PO {po.po_number} ({part.sku})",
            summary=(
                f"Critical part {part.sku} ordered from {supplier.name} "
                f"(reliability {supplier.reliability_score:.1f}/100, "
                f"avg response {supplier.avg_response_days:.1f}d). {cited}"
                "Monitor acknowledgment SLA and price confirmation closely."
            ),
            severity=AlertSeverity.MEDIUM.value,
            po_id=po.id,
            part_id=part.id,
            advisory_kind="critical_supplier_risk",
            cta_payload={
                "supplier_id": supplier.id,
                "reliability_score": supplier.reliability_score,
                "matched_notes": risk_notes,
            },
            sim_now=sim_now,
            tracker=tracker,
        )
        if res.get("created"):
            return session.get(ActionItem, res["alert_id"])
        return None

    return None


def build_lex_tool_registry(
    session: Session,
    *,
    context_comm_id: int | None = None,
    tracker: LexToolTracker | None = None,
) -> ToolRegistry:
    """Bind all 10 domain DB, planning, sandbox, reminder, and memory tools for Lex."""
    registry = ToolRegistry()

    def _get_part_snapshot(sku_or_part_id: str | int) -> dict[str, Any]:
        part = _resolve_part(session, sku_or_part_id)
        if part is None:
            return {"found": False, "error": f"Part '{sku_or_part_id}' not found"}
        sim_now = clock.now(session)
        snap = get_part_snapshot(session, part.id)
        metrics = compute_part_planning_metrics(session, part, sim_now=sim_now)
        terms = list(
            session.scalars(
                select(PartSupplierTerm)
                .where(PartSupplierTerm.part_id == part.id)
                .order_by(PartSupplierTerm.supplier_rank.asc())
            ).all()
        )
        suppliers_payload: list[dict[str, Any]] = []
        for t in terms:
            sup = session.get(Supplier, t.supplier_id)
            suppliers_payload.append(
                {
                    "supplier_id": t.supplier_id,
                    "supplier_name": sup.name if sup else None,
                    "persona_key": sup.persona_key if sup else None,
                    "supplier_rank": t.supplier_rank,
                    "reliability_score": sup.reliability_score if sup else None,
                    "agreed_unit_price": t.agreed_unit_price,
                    "currency": t.currency,
                    "quoted_lead_time_days": t.quoted_lead_time_days,
                    "effective_lead_time_days": t.effective_lead_time_days,
                    "moq": t.moq,
                }
            )
        return {
            "found": True,
            **snap.model_dump(mode="json"),
            "daily_burn_ewma": round(metrics.daily_burn_ewma, 4),
            "daily_burn_prev_7d": round(metrics.daily_burn_prev_7d, 4),
            "trend_pct": round(metrics.trend_pct, 2),
            "daily_demand_std": round(metrics.daily_demand_std, 4),
            "is_intermittent": metrics.is_intermittent,
            "days_to_stockout": (
                round(metrics.days_to_stockout, 2)
                if metrics.days_to_stockout is not None
                else None
            ),
            "predicted_stockout_date": (
                metrics.predicted_stockout_date.isoformat()
                if metrics.predicted_stockout_date is not None
                else None
            ),
            "stockout_urgency": metrics.stockout_urgency,
            "comm_buffer_days": round(metrics.comm_buffer_days, 2),
            "primary_supplier_id": metrics.primary_supplier_id,
            "primary_supplier_name": metrics.primary_supplier_name,
            "primary_quoted_lt": metrics.primary_quoted_lt,
            "primary_effective_lt": metrics.primary_effective_lt,
            "primary_avg_response_days": metrics.primary_avg_response_days,
            "safety_stock": round(metrics.safety_stock, 2),
            "safety_days": round(metrics.safety_days, 2),
            "earliest_po_eta": (
                metrics.earliest_po_eta.isoformat()
                if metrics.earliest_po_eta is not None
                else None
            ),
            "suppliers": suppliers_payload,
        }

    def _get_consumption_history(part_id: int | str, days: int = 90) -> dict[str, Any]:
        part = _resolve_part(session, part_id)
        if part is None:
            return {"found": False, "error": f"Part '{part_id}' not found"}
        window = max(1, min(365, int(days)))
        sim_now = clock.now(session)
        analytics = build_cycle_analytics(session, part, sim_now=sim_now)
        recent_withdrawals = list(
            session.scalars(
                select(InventoryTransaction)
                .where(
                    InventoryTransaction.part_id == part.id,
                    InventoryTransaction.type == "CONSUMPTION",
                    InventoryTransaction.is_predicted_burn.is_(False),
                )
                .order_by(InventoryTransaction.timestamp.desc(), InventoryTransaction.id.desc())
                .limit(10)
            ).all()
        )
        return {
            "found": True,
            "part_id": part.id,
            "sku": part.sku,
            "days": window,
            "daily_burn_ewma": round(analytics.metrics.daily_burn_ewma, 4),
            "trend_pct": round(analytics.metrics.trend_pct, 2),
            "daily_demand_std": round(analytics.metrics.daily_demand_std, 4),
            "is_intermittent": analytics.metrics.is_intermittent,
            "daily_history": [
                pt.model_dump(mode="json") for pt in analytics.history[-window:]
            ],
            "recent_withdrawals": [
                {
                    "id": tx.id,
                    "qty": abs(tx.qty_delta),
                    "timestamp": clock.ensure_utc(tx.timestamp).isoformat(),
                    "note": tx.note,
                }
                for tx in recent_withdrawals
            ],
            "cycle_events": [ev.model_dump(mode="json") for ev in analytics.events[-20:]],
        }

    def _get_po_details(po_number_or_id: str | int) -> dict[str, Any]:
        po = _resolve_po(session, po_number_or_id)
        if po is None:
            return {"found": False, "error": f"PurchaseOrder '{po_number_or_id}' not found"}
        part = session.get(Part, po.part_id)
        supplier = session.get(Supplier, po.supplier_id)
        term = session.scalar(
            select(PartSupplierTerm).where(
                PartSupplierTerm.part_id == po.part_id,
                PartSupplierTerm.supplier_id == po.supplier_id,
            )
        )
        grns = list(
            session.scalars(
                select(GoodsReceipt)
                .where(GoodsReceipt.po_id == po.id)
                .order_by(GoodsReceipt.id.asc())
            ).all()
        )
        invoices = list(
            session.scalars(
                select(Invoice).where(Invoice.po_id == po.id).order_by(Invoice.version.asc())
            ).all()
        )
        comms = list(
            session.scalars(
                select(POCommunication)
                .where(POCommunication.po_id == po.id)
                .order_by(POCommunication.timestamp.asc(), POCommunication.id.asc())
            ).all()
        )
        reminders = list(
            session.scalars(
                select(LexReminder)
                .where(
                    LexReminder.po_id == po.id,
                    LexReminder.status == LexReminderStatus.PENDING.value,
                )
                .order_by(LexReminder.remind_at.asc())
            ).all()
        )
        return {
            "found": True,
            "id": po.id,
            "po_number": po.po_number,
            "part_id": po.part_id,
            "part_sku": part.sku if part else None,
            "part_name": part.name if part else None,
            "part_criticality": part.criticality if part else None,
            "supplier_id": po.supplier_id,
            "supplier_name": supplier.name if supplier else None,
            "supplier_persona": supplier.persona_key if supplier else None,
            "qty": po.qty,
            "unit_price": po.unit_price,
            "total_amount": round(po.qty * po.unit_price, 2),
            "currency": term.currency if term else "INR",
            "status": po.status,
            "created_at": clock.ensure_utc(po.created_at).isoformat(),
            "sent_at": clock.ensure_utc(po.sent_at).isoformat() if po.sent_at else None,
            "acknowledged_at": (
                clock.ensure_utc(po.acknowledged_at).isoformat() if po.acknowledged_at else None
            ),
            "promised_eta": (
                clock.ensure_utc(po.promised_eta).isoformat() if po.promised_eta else None
            ),
            "revised_eta": clock.ensure_utc(po.revised_eta).isoformat() if po.revised_eta else None,
            "promised_dispatch_date": (
                clock.ensure_utc(po.promised_dispatch_date).isoformat()
                if po.promised_dispatch_date
                else None
            ),
            "actual_dispatch_date": (
                clock.ensure_utc(po.actual_dispatch_date).isoformat()
                if po.actual_dispatch_date
                else None
            ),
            "carrier_name": po.carrier_name,
            "tracking_number": po.tracking_number,
            "dispatch_details_status": po.dispatch_details_status,
            "dispatch_details_due_at": (
                clock.ensure_utc(po.dispatch_details_due_at).isoformat()
                if po.dispatch_details_due_at
                else None
            ),
            "missing_dispatch_fields": list(po.missing_dispatch_fields_json or []),
            "follow_up_count": po.follow_up_count,
            "notes": po.notes,
            "goods_receipts": [
                {
                    "id": g.id,
                    "received_qty": g.received_qty,
                    "condition": g.condition,
                    "status": g.status,
                    "received_at": clock.ensure_utc(g.received_at).isoformat(),
                }
                for g in grns
            ],
            "invoices": [
                {
                    "id": inv.id,
                    "invoice_number": inv.invoice_number,
                    "extracted_qty": inv.extracted_qty,
                    "extracted_unit_price": inv.extracted_unit_price,
                    "extracted_total": inv.extracted_total,
                    "match_status": inv.match_status,
                    "version": inv.version,
                }
                for inv in invoices
            ],
            "communications": [
                {
                    "id": c.id,
                    "thread_id": c.thread_id,
                    "direction": c.direction,
                    "status": c.status,
                    "from_persona": c.from_persona,
                    "subject": c.subject,
                    "timestamp": clock.ensure_utc(c.timestamp).isoformat(),
                }
                for c in comms
            ],
            "pending_reminders": [
                {
                    "id": r.id,
                    "remind_at": clock.ensure_utc(r.remind_at).isoformat(),
                    "reason": r.reason,
                    "status": r.status,
                }
                for r in reminders
            ],
        }

    def _list_open_pos(
        supplier_id: int | str | None = None,
        part_id: int | str | None = None,
    ) -> dict[str, Any]:
        stmt = select(PurchaseOrder).where(
            PurchaseOrder.status.notin_((POStatus.CLOSED.value, POStatus.CANCELLED.value))
        )
        if supplier_id is not None:
            sup = _resolve_supplier(session, supplier_id)
            if sup is None:
                return {"count": 0, "open_pos": []}
            stmt = stmt.where(PurchaseOrder.supplier_id == sup.id)
        if part_id is not None:
            part = _resolve_part(session, part_id)
            if part is None:
                return {"count": 0, "open_pos": []}
            stmt = stmt.where(PurchaseOrder.part_id == part.id)
        rows = list(session.scalars(stmt.order_by(PurchaseOrder.id.desc())).all())
        parts_map = {p.id: p for p in session.scalars(select(Part)).all()}
        sups_map = {s.id: s for s in session.scalars(select(Supplier)).all()}
        return {
            "count": len(rows),
            "open_pos": [
                {
                    "id": po.id,
                    "po_number": po.po_number,
                    "part_id": po.part_id,
                    "part_sku": parts_map[po.part_id].sku if po.part_id in parts_map else None,
                    "supplier_id": po.supplier_id,
                    "supplier_name": (
                        sups_map[po.supplier_id].name if po.supplier_id in sups_map else None
                    ),
                    "qty": po.qty,
                    "unit_price": po.unit_price,
                    "status": po.status,
                    "promised_eta": (
                        clock.ensure_utc(po.promised_eta).isoformat() if po.promised_eta else None
                    ),
                    "revised_eta": (
                        clock.ensure_utc(po.revised_eta).isoformat() if po.revised_eta else None
                    ),
                    "dispatch_details_status": po.dispatch_details_status,
                    "missing_dispatch_fields": list(po.missing_dispatch_fields_json or []),
                }
                for po in rows
            ],
        }

    def _get_supplier_profile(supplier_key_or_id: str | int) -> dict[str, Any]:
        sup = _resolve_supplier(session, supplier_key_or_id)
        if sup is None:
            return {"found": False, "error": f"Supplier '{supplier_key_or_id}' not found"}
        sim_now = clock.now(session)
        score = compute_supplier_score(session, sup, sim_now=sim_now)
        notes = list(
            session.scalars(
                select(SupplierMemoryNote)
                .where(SupplierMemoryNote.supplier_id == sup.id)
                .order_by(SupplierMemoryNote.id.asc())
            ).all()
        )
        terms = list(
            session.scalars(
                select(PartSupplierTerm)
                .where(PartSupplierTerm.supplier_id == sup.id)
                .order_by(PartSupplierTerm.part_id.asc())
            ).all()
        )
        parts_map = {p.id: p for p in session.scalars(select(Part)).all()}
        return {
            "found": True,
            "id": sup.id,
            "persona_key": sup.persona_key,
            "name": sup.name,
            "email": sup.email,
            "reliability_score": score.reliability_score,
            "otd_rate": score.otd_rate,
            "invoice_accuracy_rate": score.invoice_accuracy_rate,
            "response_rate": score.response_rate,
            "avg_response_days": score.avg_response_days,
            "cycles_90d": score.cycles_90d,
            "memory_notes": [
                {
                    "id": n.id,
                    "note": n.note,
                    "source": n.source,
                    "created_at": clock.ensure_utc(n.created_at).isoformat(),
                }
                for n in notes
            ],
            "part_terms": [
                {
                    "part_id": t.part_id,
                    "part_sku": parts_map[t.part_id].sku if t.part_id in parts_map else None,
                    "supplier_rank": t.supplier_rank,
                    "agreed_unit_price": t.agreed_unit_price,
                    "currency": t.currency,
                    "quoted_lead_time_days": t.quoted_lead_time_days,
                    "effective_lead_time_days": t.effective_lead_time_days,
                    "lead_time_std_dev": t.lead_time_std_dev,
                    "moq": t.moq,
                }
                for t in terms
            ],
        }

    def _get_mailroom_thread(thread_id: str) -> dict[str, Any]:
        msgs = get_thread(session, thread_id)
        return {
            "thread_id": thread_id,
            "message_count": len(msgs),
            "messages": [
                {
                    "id": m.id,
                    "direction": m.direction,
                    "status": m.status,
                    "category": m.category,
                    "from_persona": m.from_persona,
                    "to_personas": list(m.to_personas),
                    "cc_personas": list(m.cc_personas),
                    "subject": m.subject,
                    "body": m.body,
                    "po_id": m.po_id,
                    "timestamp": m.timestamp.isoformat(),
                    "attachment_paths": list(m.attachment_paths or []),
                }
                for m in msgs
            ],
        }

    def _run_sandbox_calc(
        python_script: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        res = run_sandbox_python(python_script, variables or {})
        return res.model_dump(mode="json")

    def _schedule_lex_reminder(
        po_id: int,
        remind_at_iso: str,
        reason: str,
        suggested_action: str = "",
    ) -> dict[str, Any]:
        remind_dt = parse_iso_date_utc(remind_at_iso)
        if remind_dt is None:
            raise ValueError(f"Invalid ISO datetime/date for remind_at_iso: '{remind_at_iso}'")
        return schedule_po_lex_reminder(
            session,
            po_id=int(po_id),
            remind_at=remind_dt,
            reason=reason,
            suggested_action=suggested_action,
            comm_id=context_comm_id,
            tracker=tracker,
        )

    def _propose_memory_note(
        supplier_id: int | str,
        note: str,
        reasoning: str = "",
    ) -> dict[str, Any]:
        sup = _resolve_supplier(session, supplier_id)
        if sup is None:
            raise ValueError(f"Supplier '{supplier_id}' not found")
        return propose_and_save_memory_note(
            session,
            supplier_id=sup.id,
            note=note,
            reasoning=reasoning,
            comm_id=context_comm_id,
            create_alert=True,
            tracker=tracker,
        )

    def _create_advisory_alert(
        title: str,
        summary: str,
        severity: str = "INFO",
        po_id: int | None = None,
        part_id: int | None = None,
    ) -> dict[str, Any]:
        return create_grounded_advisory_alert(
            session,
            title=title,
            summary=summary,
            severity=severity,
            po_id=po_id,
            part_id=part_id,
            tracker=tracker,
        )

    registry.register_function(
        name="get_part_snapshot",
        description=(
            "Fetch live stock balances, on-order stock, daily EWMA burn, days to stockout, "
            "dynamic ROP/T, and primary/backup suppliers by SKU or part_id."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "sku_or_part_id": {
                    "type": "string",
                    "description": "Part SKU (e.g., 'BRG-402') or integer part ID.",
                }
            },
            "required": ["sku_or_part_id"],
        },
        handler=_get_part_snapshot,
        read_only=True,
    )
    registry.register_function(
        name="get_consumption_history",
        description=(
            "Fetch trailing daily consumption history, recent withdrawals, and cycle events."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "part_id": {"type": "integer", "description": "Numeric part ID."},
                "days": {"type": "integer", "description": "Lookback days (1-365, default 90)."},
            },
            "required": ["part_id"],
        },
        handler=_get_consumption_history,
        read_only=True,
    )
    registry.register_function(
        name="get_po_details",
        description=(
            "Fetch full PurchaseOrder details including ETA, dispatch logistics, "
            "GRNs, invoices, communications, and pending Lex reminders."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "po_number_or_id": {
                    "type": "string",
                    "description": "PO number (e.g., 'PO-1037') or numeric PO ID.",
                }
            },
            "required": ["po_number_or_id"],
        },
        handler=_get_po_details,
        read_only=True,
    )
    registry.register_function(
        name="list_open_pos",
        description="List currently open (non-closed, non-cancelled) PurchaseOrders.",
        parameters_schema={
            "type": "object",
            "properties": {
                "supplier_id": {"type": "integer"},
                "part_id": {"type": "integer"},
            },
        },
        handler=_list_open_pos,
        read_only=True,
    )
    registry.register_function(
        name="get_supplier_profile",
        description=(
            "Fetch supplier reliability score, OTD, invoice accuracy, avg response days, "
            "90d cycles, part terms, and all stored SupplierMemoryNotes."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "supplier_key_or_id": {
                    "type": "string",
                    "description": "Supplier persona key (e.g., 'sup2'), name, or numeric ID.",
                }
            },
            "required": ["supplier_key_or_id"],
        },
        handler=_get_supplier_profile,
        read_only=True,
    )
    registry.register_function(
        name="get_mailroom_thread",
        description="Fetch full chronological email thread history by thread_id.",
        parameters_schema={
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"],
        },
        handler=_get_mailroom_thread,
        read_only=True,
    )
    registry.register_function(
        name="run_sandbox_calc",
        description=(
            "Execute AST-sandboxed Python calculation scripts (allowing math, statistics, "
            "datetime, timedelta, decimal, json) to verify stockout runway, deferral risk, "
            "partial-delivery gaps, or spend impact."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "python_script": {
                    "type": "string",
                    "description": "Python script assigning final value to `result`.",
                },
                "variables": {
                    "type": "object",
                    "description": "Input variables injected into the sandbox namespace.",
                },
            },
            "required": ["python_script"],
        },
        handler=_run_sandbox_calc,
        read_only=True,
    )
    registry.register_function(
        name="schedule_lex_reminder",
        description=(
            "Schedule a future LexReminder wakeup (PENDING) for a PO when dispatch details "
            "are deferred and runway is safe, syncing PO dispatch_details_due_at."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "po_id": {"type": "integer"},
                "remind_at_iso": {"type": "string"},
                "reason": {"type": "string"},
                "suggested_action": {"type": "string"},
            },
            "required": ["po_id", "remind_at_iso", "reason"],
        },
        handler=_schedule_lex_reminder,
        read_only=False,
    )
    registry.register_function(
        name="propose_memory_note",
        description=(
            "Deduplicate/consolidate a durable supplier habit against existing memory notes "
            "before saving to SupplierMemoryNote."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "supplier_id": {"type": "integer"},
                "note": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["supplier_id", "note"],
        },
        handler=_propose_memory_note,
        read_only=False,
    )
    registry.register_function(
        name="create_advisory_alert",
        description=(
            "Raise a grounded Manager advisory alert when supplier memory or runway math "
            "reveals a concrete operational risk."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "severity": {
                    "type": "string",
                    "enum": ["CRITICAL", "HIGH", "MEDIUM", "INFO"],
                },
                "po_id": {"type": "integer"},
                "part_id": {"type": "integer"},
            },
            "required": ["title", "summary"],
        },
        handler=_create_advisory_alert,
        read_only=False,
    )

    return registry
