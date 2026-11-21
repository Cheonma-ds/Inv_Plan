from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.agents.lex import (
    EmailDraftInput,
    PendingStockRequestContext,
    StockPartContext,
    StockRequestIntent,
    StockRequestItem,
    StockRequestTriageInput,
    StockRequestTriageResult,
    ThreadMessageContext,
)
from backend.core import clock
from backend.core.enums import (
    ActionItemStatus,
    AlertSeverity,
    Criticality,
    EmailCategory,
    PersonaKey,
    POStatus,
    StockRequestStatus,
)
from backend.core.models import (
    ActionItem,
    Part,
    PartSupplierTerm,
    PurchaseOrder,
    StockRequest,
    Supplier,
)
from backend.inventory import get_part_snapshot
from backend.mailroom import (
    OutboundMessage,
    StoredEmail,
    annotate_triage,
    get_thread,
    reject_draft,
    save_draft,
)
from backend.po import create_action_item, resolve_action_item

if TYPE_CHECKING:
    from backend.orchestrator.workflows import WorkflowEngine

STOCK_REQUEST_CTA_ACTIONS = {
    "approve_stock_withdrawal",
    "issue_partial_stock",
    "ask_making_minimum",
    "reject_stock_request",
}


def _resolve_part_from_item(
    item: StockRequestItem,
    parts_by_sku: dict[str, Part],
    all_parts: list[Part],
) -> Part | None:
    sku_raw = (item.part_sku or "").strip()
    if sku_raw:
        return parts_by_sku.get(sku_raw.upper())

    name_raw = (item.part_name or "").strip().lower()
    if not name_raw:
        return None
    exact = [p for p in all_parts if p.name.strip().lower() == name_raw]
    if len(exact) == 1:
        return exact[0]
    partial = [
        p for p in all_parts if name_raw in p.name.lower() or p.name.lower() in name_raw
    ]
    return partial[0] if len(partial) == 1 else None


def _analyze_part_for_request(
    session: Session,
    part: Part,
    *,
    effective_qty: int,
    original_requested_qty: int,
    is_min_qty_confirmation: bool,
    urgency_note: str | None,
) -> dict[str, Any]:
    """Compute 100% deterministic stock, active PO, supplier term, and ROP impact."""
    snapshot = get_part_snapshot(session, part.id)

    open_po_rows = session.scalars(
        select(PurchaseOrder)
        .where(
            PurchaseOrder.part_id == part.id,
            PurchaseOrder.status.notin_((POStatus.CLOSED.value, POStatus.CANCELLED.value)),
        )
        .order_by(PurchaseOrder.id.asc())
    ).all()
    open_pos: list[dict[str, Any]] = []
    for po in open_po_rows:
        sup = session.get(Supplier, po.supplier_id)
        eta_dt = po.revised_eta or po.promised_eta
        eta_iso = clock.ensure_utc(eta_dt).date().isoformat() if eta_dt else None
        open_pos.append(
            {
                "po_id": po.id,
                "po_number": po.po_number,
                "status": po.status,
                "qty": po.qty,
                "eta": eta_iso,
                "supplier_name": sup.name if sup else "Unknown Supplier",
            }
        )

    terms = session.scalars(
        select(PartSupplierTerm)
        .where(PartSupplierTerm.part_id == part.id)
        .order_by(PartSupplierTerm.supplier_rank.asc())
    ).all()
    primary_term = terms[0] if terms else None
    backup_term = terms[1] if len(terms) > 1 else None

    primary_sup = session.get(Supplier, primary_term.supplier_id) if primary_term else None
    backup_sup = session.get(Supplier, backup_term.supplier_id) if backup_term else None

    shortfall_qty = max(0, effective_qty - snapshot.on_hand_stock)
    is_shortage = effective_qty > snapshot.on_hand_stock
    projected_on_hand = max(0, snapshot.on_hand_stock - effective_qty) if not is_shortage else 0
    projected_ip = snapshot.inventory_position - effective_qty
    will_trigger_po = (not is_shortage) and (projected_ip < snapshot.rop)

    primary_moq = primary_term.moq if primary_term else 1
    projected_order_qty: int | None = None
    if will_trigger_po:
        raw_qty = snapshot.replenishment_point - projected_ip
        projected_order_qty = max(raw_qty, primary_moq)

    return {
        "part_id": part.id,
        "sku": part.sku,
        "part_name": part.name,
        "part_criticality": part.criticality,
        "requested_qty": effective_qty,
        "effective_qty": effective_qty,
        "original_requested_qty": original_requested_qty,
        "is_min_qty_confirmation": is_min_qty_confirmation,
        "on_hand_stock": snapshot.on_hand_stock,
        "quarantined_stock": snapshot.quarantined_stock,
        "on_order_stock": snapshot.on_order_stock,
        "inventory_position": snapshot.inventory_position,
        "rop": snapshot.rop,
        "replenishment_point": snapshot.replenishment_point,
        "shortfall_qty": shortfall_qty,
        "is_shortage": is_shortage,
        "projected_on_hand": projected_on_hand,
        "projected_ip": projected_ip,
        "will_trigger_po": will_trigger_po,
        "projected_order_qty": projected_order_qty,
        "primary_supplier_id": primary_sup.id if primary_sup else None,
        "primary_supplier_name": primary_sup.name if primary_sup else None,
        "primary_unit_price": primary_term.agreed_unit_price if primary_term else None,
        "primary_effective_lt": primary_term.effective_lead_time_days if primary_term else None,
        "primary_quoted_lt": primary_term.quoted_lead_time_days if primary_term else None,
        "primary_moq": primary_moq if primary_term else None,
        "primary_reliability_score": primary_sup.reliability_score if primary_sup else None,
        "backup_supplier_name": backup_sup.name if backup_sup else None,
        "backup_unit_price": backup_term.agreed_unit_price if backup_term else None,
        "backup_effective_lt": backup_term.effective_lead_time_days if backup_term else None,
        "open_pos": open_pos,
        "urgency_note": urgency_note,
    }


def _resolve_stale_request_alerts(session: Session, stock_request_id: int) -> None:
    open_alerts = session.scalars(
        select(ActionItem).where(
            ActionItem.status == ActionItemStatus.OPEN.value,
            ActionItem.type.in_(("STOCK_SHORTAGE", "STOCK_WITHDRAWAL_REQUEST")),
        )
    ).all()
    for alert in open_alerts:
        payload = alert.cta_payload_json or {}
        if payload.get("stock_request_id") == stock_request_id:
            resolve_action_item(session, alert.id)


def _format_open_pos_snippet(open_pos: list[dict[str, Any]]) -> str:
    if not open_pos:
        return "No active open POs."
    parts = []
    for po in open_pos:
        eta_label = po["eta"] or "TBD"
        parts.append(f"{po['po_number']} ({po['qty']}u, {po['status']}, ETA {eta_label})")
    return "Active POs: " + "; ".join(parts) + "."


def _create_ambiguous_stock_alert(
    session: Session,
    email: StoredEmail,
    triage_result: StockRequestTriageResult,
) -> ActionItem:
    return create_action_item(
        session,
        type="AMBIGUOUS_STOCK_REQUEST",
        severity=AlertSeverity.HIGH,
        title="Manual review needed: stock request from Making Team",
        summary=(
            f"{triage_result.summary_for_manager} "
            f"(Confidence: {triage_result.confidence:.0%}. No stock or PO state was changed.)"
        ),
        suggested_actions=[{"action": "add_note", "label": "Log Review Note"}],
        cta_payload={"comm_id": email.id, "reasoning": triage_result.reasoning},
    )


def _create_shortage_draft(
    session: Session,
    engine: "WorkflowEngine",
    *,
    email: StoredEmail,
    part: Part,
    analysis: dict[str, Any],
    sim_now_iso: str,
) -> StoredEmail:
    draft_res = engine.lex.draft_email(
        session,
        EmailDraftInput(
            category=EmailCategory.STOCK_REQUEST_CLARIFICATION,
            po_number="",
            part_sku=part.sku,
            part_name=part.name,
            qty=analysis["requested_qty"],
            unit_price=float(analysis["primary_unit_price"] or 0.0),
            recipient_persona=PersonaKey.MAKING.value,
            recipient_name="Making Team",
            current_date=sim_now_iso,
            thread_subject=email.subject,
            context_facts={
                "requested_qty": analysis["requested_qty"],
                "on_hand_stock": analysis["on_hand_stock"],
                "quarantined_stock": analysis["quarantined_stock"],
                "shortfall_qty": analysis["shortfall_qty"],
                "open_pos": analysis["open_pos"],
            },
        ),
    )
    to_personas = [PersonaKey(p) for p in draft_res.to_personas]
    if PersonaKey.MAKING not in to_personas:
        to_personas = [PersonaKey.MAKING]
    cc_personas = [PersonaKey(p) for p in draft_res.cc_personas]
    if PersonaKey.INVENTORY_MANAGER not in cc_personas:
        cc_personas.append(PersonaKey.INVENTORY_MANAGER)

    return save_draft(
        session,
        OutboundMessage(
            from_persona=PersonaKey.LEX,
            to_personas=to_personas,
            cc_personas=cc_personas,
            subject=draft_res.subject,
            body=draft_res.body,
            po_id=None,
            thread_id=email.thread_id,
            category=EmailCategory.STOCK_REQUEST_CLARIFICATION,
        ),
    )


def process_inbound_making_email(
    session: Session,
    engine: "WorkflowEngine",
    email: StoredEmail,
) -> dict[str, Any]:
    """Triage Making Team email (CC'ing Lex), run deterministic analysis, and create alerts."""
    sim_now = clock.now(session)
    all_parts = list(session.scalars(select(Part).order_by(Part.sku.asc())).all())
    parts_by_sku = {p.sku.strip().upper(): p for p in all_parts}

    catalog_parts = [
        StockPartContext(part_sku=p.sku, part_name=p.name, criticality=p.criticality)
        for p in all_parts
    ]

    thread_emails = get_thread(session, email.thread_id)
    thread_history = [
        ThreadMessageContext(
            from_persona=m.from_persona,
            subject=m.subject,
            body=m.body,
            timestamp=m.timestamp.isoformat(),
        )
        for m in thread_emails
        if m.id != email.id
    ]

    open_thread_requests = session.scalars(
        select(StockRequest)
        .where(
            StockRequest.thread_id == email.thread_id,
            StockRequest.status.in_(
                (
                    StockRequestStatus.AWAITING_MIN_QTY.value,
                    StockRequestStatus.PENDING_REVIEW.value,
                )
            ),
        )
        .order_by(StockRequest.id.asc())
    ).all()
    pending_contexts: list[PendingStockRequestContext] = []
    for sr in open_thread_requests:
        sr_part = session.get(Part, sr.part_id)
        if sr_part is not None:
            pending_contexts.append(
                PendingStockRequestContext(
                    part_sku=sr_part.sku,
                    part_name=sr_part.name,
                    requested_qty=sr.requested_qty,
                    status=sr.status,
                )
            )

    triage_result = engine.lex.triage_stock_request(
        session,
        StockRequestTriageInput(
            sender_persona=email.from_persona,
            sender_name="Making Team",
            subject=email.subject,
            body=email.body,
            current_date=sim_now.date().isoformat(),
            catalog_parts=catalog_parts,
            thread_history=thread_history,
            pending_requests=pending_contexts,
        ),
        linked_comm_id=email.id,
    )

    annotate_triage(
        session,
        email.id,
        classification=triage_result.intent.value,
        extracted_json=triage_result.model_dump(mode="json"),
        confidence=triage_result.confidence,
    )

    if triage_result.intent == StockRequestIntent.GENERAL_MESSAGE:
        alert = create_action_item(
            session,
            type="MAKING_MESSAGE",
            severity=AlertSeverity.INFO,
            title=f"Message from Making Team: {email.subject}",
            summary=triage_result.summary_for_manager,
            suggested_actions=[],
            cta_payload={"comm_id": email.id},
        )
        return {
            "triage": triage_result,
            "stock_request_ids": [],
            "alert_ids": [alert.id],
            "draft_comm_ids": [],
        }

    if triage_result.intent == StockRequestIntent.AMBIGUOUS:
        alert = _create_ambiguous_stock_alert(session, email, triage_result)
        return {
            "triage": triage_result,
            "stock_request_ids": [],
            "alert_ids": [alert.id],
            "draft_comm_ids": [],
        }

    # Resolve & aggregate items by part_id to prevent duplicate StockRequests for the same SKU
    aggregated: dict[int, tuple[Part, int]] = {}
    for item in triage_result.requested_items:
        part = _resolve_part_from_item(item, parts_by_sku, all_parts)
        if part is None or item.requested_qty is None or item.requested_qty <= 0:
            alert = _create_ambiguous_stock_alert(session, email, triage_result)
            return {
                "triage": triage_result,
                "stock_request_ids": [],
                "alert_ids": [alert.id],
                "draft_comm_ids": [],
            }
        prev_qty = aggregated[part.id][1] if part.id in aggregated else 0
        aggregated[part.id] = (part, prev_qty + item.requested_qty)

    is_min_qty = triage_result.intent == StockRequestIntent.MINIMUM_QTY_CONFIRMATION
    reason_text = (triage_result.urgency_note or triage_result.summary_for_manager).strip()

    stock_request_ids: list[int] = []
    alert_ids: list[int] = []
    draft_comm_ids: list[int] = []

    for part, qty in aggregated.values():
        existing_sr: StockRequest | None = None
        if is_min_qty:
            existing_sr = session.scalar(
                select(StockRequest)
                .where(
                    StockRequest.thread_id == email.thread_id,
                    StockRequest.part_id == part.id,
                    StockRequest.status.in_(
                        (
                            StockRequestStatus.AWAITING_MIN_QTY.value,
                            StockRequestStatus.PENDING_REVIEW.value,
                        )
                    ),
                )
                .order_by(StockRequest.id.desc())
                .limit(1)
            )

        original_qty = existing_sr.requested_qty if existing_sr is not None else qty
        analysis = _analyze_part_for_request(
            session,
            part,
            effective_qty=qty,
            original_requested_qty=original_qty,
            is_min_qty_confirmation=is_min_qty,
            urgency_note=triage_result.urgency_note,
        )

        if existing_sr is not None:
            _resolve_stale_request_alerts(session, existing_sr.id)
            existing_sr.comm_id = email.id
            existing_sr.status = StockRequestStatus.PENDING_REVIEW.value
            existing_sr.analysis_json = analysis
            if reason_text:
                existing_sr.reason = reason_text
            session.flush()
            sr = existing_sr
        else:
            sr = StockRequest(
                comm_id=email.id,
                thread_id=email.thread_id,
                part_id=part.id,
                requested_qty=qty,
                approved_qty=None,
                status=StockRequestStatus.PENDING_REVIEW.value,
                reason=reason_text,
                analysis_json=analysis,
                created_at=sim_now,
                resolved_at=None,
            )
            session.add(sr)
            session.flush()

        stock_request_ids.append(sr.id)
        cta_payload = {"stock_request_id": sr.id, "comm_id": email.id, **analysis}
        open_pos_text = _format_open_pos_snippet(analysis["open_pos"])

        if analysis["is_shortage"]:
            clarification_draft = _create_shortage_draft(
                session,
                engine,
                email=email,
                part=part,
                analysis=analysis,
                sim_now_iso=sim_now.date().isoformat(),
            )
            draft_comm_ids.append(clarification_draft.id)
            cta_payload["draft_comm_id"] = clarification_draft.id

            quarantine_note = (
                f" ({analysis['quarantined_stock']} units quarantined on invoice mismatch.)"
                if analysis["quarantined_stock"] > 0
                else ""
            )
            suggested_actions: list[dict[str, str]] = [
                {"action": "ask_making_minimum", "label": "Approve & Send Min-Qty Inquiry"},
            ]
            if analysis["on_hand_stock"] > 0:
                suggested_actions.append(
                    {
                        "action": "issue_partial_stock",
                        "label": f"Issue Available On-Hand ({analysis['on_hand_stock']} units)",
                    }
                )
            suggested_actions.append({"action": "reject_stock_request", "label": "Reject Request"})

            severity = (
                AlertSeverity.CRITICAL
                if (
                    part.criticality == Criticality.CRITICAL.value
                    or analysis["on_hand_stock"] == 0
                )
                else AlertSeverity.HIGH
            )
            alert = create_action_item(
                session,
                type="STOCK_SHORTAGE",
                severity=severity,
                part_criticality=part.criticality,
                part_id=part.id,
                draft_comm_id=clarification_draft.id,
                title=(
                    f"Stock shortage for Making request: {part.sku} "
                    f"(need {qty}, on hand {analysis['on_hand_stock']})"
                ),
                summary=(
                    f"Making Team requested {qty} units of {part.name} ({part.sku}), but only "
                    f"{analysis['on_hand_stock']} units are on hand (shortfall: "
                    f"{analysis['shortfall_qty']}){quarantine_note}. {open_pos_text} "
                    "Lex drafted a clarification email asking Making for the minimum qty needed."
                ),
                suggested_actions=suggested_actions,
                cta_payload=cta_payload,
            )
            alert_ids.append(alert.id)

        elif analysis["will_trigger_po"]:
            severity = (
                AlertSeverity.HIGH
                if part.criticality == Criticality.CRITICAL.value
                else AlertSeverity.MEDIUM
            )
            sup_name = analysis["primary_supplier_name"] or "Primary Supplier"
            unit_price = analysis["primary_unit_price"]
            price_str = f"₹{unit_price:,.2f}/unit" if unit_price is not None else "agreed rate"
            lt_str = (
                f"{analysis['primary_effective_lt']:.1f}d"
                if analysis["primary_effective_lt"] is not None
                else "standard LT"
            )
            action_label = (
                f"Approve Min Qty ({qty}u) & Trigger PO"
                if is_min_qty
                else "Approve Issue & Trigger PO"
            )
            alert = create_action_item(
                session,
                type="STOCK_WITHDRAWAL_REQUEST",
                severity=severity,
                part_criticality=part.criticality,
                part_id=part.id,
                title=f"Approve stock issue & PO trigger: {part.sku} ({qty} units)",
                summary=(
                    f"Making Team requested {qty} units of {part.name} ({part.sku}). "
                    f"Approving reduces on-hand from {analysis['on_hand_stock']} to "
                    f"{analysis['projected_on_hand']} and IP from {analysis['inventory_position']} "
                    f"to {analysis['projected_ip']} (< ROP {analysis['rop']}), triggering a "
                    f"replenishment PO for {analysis['projected_order_qty']} units via {sup_name} "
                    f"({price_str}, eff. LT {lt_str}). {open_pos_text}"
                ),
                suggested_actions=[
                    {"action": "approve_stock_withdrawal", "label": action_label},
                    {"action": "reject_stock_request", "label": "Reject Request"},
                ],
                cta_payload=cta_payload,
            )
            alert_ids.append(alert.id)

        else:
            severity = (
                AlertSeverity.MEDIUM
                if part.criticality == Criticality.CRITICAL.value
                else AlertSeverity.INFO
            )
            action_label = (
                f"Approve & Issue Min Qty ({qty} units)"
                if is_min_qty
                else "Approve & Issue Stock"
            )
            title = (
                f"Approve min-qty stock issue: {part.sku} ({qty} units)"
                if is_min_qty
                else f"Approve stock withdrawal: {part.sku} ({qty} units)"
            )
            alert = create_action_item(
                session,
                type="STOCK_WITHDRAWAL_REQUEST",
                severity=severity,
                part_criticality=part.criticality,
                part_id=part.id,
                title=title,
                summary=(
                    f"Making Team requested {qty} units of {part.name} ({part.sku}). "
                    f"On-hand stock ({analysis['on_hand_stock']}) is healthy and post-issue IP "
                    f"({analysis['projected_ip']}) remains >= ROP ({analysis['rop']}). "
                    f"{open_pos_text}"
                ),
                suggested_actions=[
                    {"action": "approve_stock_withdrawal", "label": action_label},
                    {"action": "reject_stock_request", "label": "Reject Request"},
                ],
                cta_payload=cta_payload,
            )
            alert_ids.append(alert.id)

    return {
        "triage": triage_result,
        "stock_request_ids": stock_request_ids,
        "alert_ids": alert_ids,
        "draft_comm_ids": draft_comm_ids,
    }


def mark_stock_requests_awaiting_min_qty(session: Session, thread_id: str) -> None:
    rows = session.scalars(
        select(StockRequest).where(
            StockRequest.thread_id == thread_id,
            StockRequest.status == StockRequestStatus.PENDING_REVIEW.value,
        )
    ).all()
    for sr in rows:
        sr.status = StockRequestStatus.AWAITING_MIN_QTY.value
    session.flush()


def _load_active_stock_request(
    session: Session,
    item: ActionItem,
    params: dict[str, Any],
) -> StockRequest:
    cta = item.cta_payload_json or {}
    sr_id = int(params.get("stock_request_id") or cta.get("stock_request_id") or 0)
    if not sr_id:
        raise ValueError("No stock_request_id linked to this alert")
    sr = session.get(StockRequest, sr_id)
    if sr is None:
        raise ValueError(f"StockRequest {sr_id} not found")
    if sr.status in (
        StockRequestStatus.APPROVED.value,
        StockRequestStatus.PARTIALLY_APPROVED.value,
        StockRequestStatus.REJECTED.value,
    ):
        raise ValueError(f"StockRequest {sr.id} is already finalized ({sr.status})")
    return sr


def _reject_unused_clarification_draft(session: Session, item: ActionItem) -> None:
    if not item.draft_comm_id:
        return
    try:
        reject_draft(session, item.draft_comm_id)
    except ValueError:
        pass


def _finalize_stock_withdrawal(
    session: Session,
    engine: "WorkflowEngine",
    item: ActionItem,
    sr: StockRequest,
    *,
    issue_qty: int,
    note: str,
    reject_unused_draft: bool = False,
) -> dict[str, Any]:
    withdraw_res = engine.withdraw_stock(
        session,
        part_id=sr.part_id,
        qty=issue_qty,
        note=note,
    )
    sr.approved_qty = issue_qty
    sr.status = (
        StockRequestStatus.PARTIALLY_APPROVED.value
        if issue_qty < sr.requested_qty
        else StockRequestStatus.APPROVED.value
    )
    sr.resolved_at = clock.now(session)
    if reject_unused_draft:
        _reject_unused_clarification_draft(session, item)
    resolve_action_item(session, item.id)
    session.flush()
    return {
        "stock_request_id": sr.id,
        "status": sr.status,
        "part_id": sr.part_id,
        "approved_qty": issue_qty,
        "on_hand_after": withdraw_res["consume"].snapshot.on_hand_stock,
        "created_po_id": withdraw_res["created_po"].id if withdraw_res["created_po"] else None,
        "created_po_number": (
            withdraw_res["created_po"].po_number if withdraw_res["created_po"] else None
        ),
        "draft_comm_id": withdraw_res["draft_email"].id if withdraw_res["draft_email"] else None,
        "alert_ids": [a.id for a in withdraw_res["alerts"]],
    }


def execute_stock_request_cta(
    session: Session,
    engine: "WorkflowEngine",
    item: ActionItem,
    *,
    action: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    cta = item.cta_payload_json or {}
    sr = _load_active_stock_request(session, item, params)

    if action == "approve_stock_withdrawal":
        issue_qty = int(
            params.get("qty")
            or cta.get("effective_qty")
            or cta.get("requested_qty")
            or sr.requested_qty
        )
        if issue_qty <= 0:
            raise ValueError("Withdrawal quantity must be positive")
        return _finalize_stock_withdrawal(
            session,
            engine,
            item,
            sr,
            issue_qty=issue_qty,
            note=f"Approved Making Team StockRequest #{sr.id} (thread {sr.thread_id})",
        )

    if action == "issue_partial_stock":
        part = session.get(Part, sr.part_id)
        if part is None:
            raise ValueError(f"Part {sr.part_id} not found")
        partial_qty = (
            int(params["qty"])
            if params.get("qty") is not None
            else min(sr.requested_qty, part.on_hand_stock)
        )
        if partial_qty <= 0 or partial_qty > part.on_hand_stock:
            raise ValueError(
                f"Cannot issue {partial_qty} partial units; on_hand_stock={part.on_hand_stock}"
            )
        return _finalize_stock_withdrawal(
            session,
            engine,
            item,
            sr,
            issue_qty=partial_qty,
            note=f"Partial stock issue for Making Team StockRequest #{sr.id}",
            reject_unused_draft=True,
        )

    if action == "ask_making_minimum":
        draft_id = int(
            params.get("draft_comm_id") or item.draft_comm_id or cta.get("draft_comm_id") or 0
        )
        if not draft_id:
            raise ValueError("No clarification draft linked to this alert")
        approved = engine.approve_lex_draft(
            session,
            draft_id,
            edited_subject=params.get("edited_subject"),
            edited_body=params.get("edited_body"),
        )
        sr.status = StockRequestStatus.AWAITING_MIN_QTY.value
        session.flush()
        return {
            "stock_request_id": sr.id,
            "status": sr.status,
            "draft_comm_id": draft_id,
            "approval_outcome": approved["approval_outcome"],
        }

    if action == "reject_stock_request":
        sr.approved_qty = 0
        sr.status = StockRequestStatus.REJECTED.value
        sr.resolved_at = clock.now(session)
        note = str(params.get("note") or "").strip()
        if note:
            sr.analysis_json = {**(sr.analysis_json or {}), "rejection_note": note}
        _reject_unused_clarification_draft(session, item)
        resolve_action_item(session, item.id)
        session.flush()
        return {
            "stock_request_id": sr.id,
            "status": sr.status,
        }

    raise ValueError(f"Unsupported stock request CTA action: '{action}'")
