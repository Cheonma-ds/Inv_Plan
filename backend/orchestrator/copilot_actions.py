from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.agents.lex import CopilotActionType, CopilotProposedAction
from backend.core.enums import CommStatus
from backend.core.models import (
    ActionItem,
    Part,
    PartSupplierTerm,
    POCommunication,
    PurchaseOrder,
    Supplier,
)
from backend.mailroom import reject_draft
from backend.orchestrator.lex_tools import (
    parse_iso_date_utc,
    propose_and_save_memory_note,
    schedule_po_lex_reminder,
)
from backend.orchestrator.logistics_workflow import parse_iso_date_utc as parse_logistics_date
from backend.orchestrator.workflows import WorkflowEngine
from backend.planning import compute_po_suggestion
from backend.po import dismiss_action_item, reopen_action_item, update_po_terms


def _params_dict(action: CopilotProposedAction) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in action.params:
        k = item.key.strip()
        if k:
            out[k] = str(item.value).strip()
    return out


def _resolve_part(session: Session, ref: str | int | None) -> Part:
    raw = str(ref or "").strip()
    if not raw:
        raise ValueError("Part SKU or ID is required.")
    part: Part | None = None
    if raw.isdigit():
        part = session.get(Part, int(raw))
    if part is None:
        part = session.scalar(select(Part).where(func.lower(Part.sku) == raw.lower()))
    if part is None:
        raise ValueError(f"Part '{raw}' not found.")
    return part


def _resolve_po(session: Session, ref: str | int | None) -> PurchaseOrder:
    raw = str(ref or "").strip()
    if not raw:
        raise ValueError("PO number or ID is required.")
    po: PurchaseOrder | None = None
    if raw.isdigit():
        po = session.get(PurchaseOrder, int(raw))
    if po is None:
        po = session.scalar(
            select(PurchaseOrder).where(func.lower(PurchaseOrder.po_number) == raw.lower())
        )
    if po is None:
        raise ValueError(f"PurchaseOrder '{raw}' not found.")
    return po


def _resolve_supplier(session: Session, ref: str | int | None) -> Supplier:
    raw = str(ref or "").strip()
    if not raw:
        raise ValueError("Supplier key, name, or ID is required.")
    sup: Supplier | None = None
    if raw.isdigit():
        sup = session.get(Supplier, int(raw))
    if sup is None:
        sup = session.scalar(
            select(Supplier).where(func.lower(Supplier.persona_key) == raw.lower())
        )
    if sup is None:
        sup = session.scalar(select(Supplier).where(func.lower(Supplier.name) == raw.lower()))
    if sup is None:
        raise ValueError(f"Supplier '{raw}' not found.")
    return sup


def _resolve_pending_comm_id(session: Session, params: dict[str, str], target_ref: str) -> int:
    raw_comm = params.get("comm_id") or ""
    if raw_comm.isdigit():
        return int(raw_comm)
    po_ref = params.get("po_number") or params.get("po_number_or_id") or target_ref
    if po_ref:
        po = _resolve_po(session, po_ref)
        comm = session.scalar(
            select(POCommunication)
            .where(
                POCommunication.po_id == po.id,
                POCommunication.status == CommStatus.DRAFT_PENDING_APPROVAL.value,
            )
            .order_by(POCommunication.id.desc())
        )
        if comm is not None:
            return comm.id
    if target_ref.isdigit():
        return int(target_ref)
    raise ValueError("Could not resolve a pending draft communication ID.")


def execute_single_copilot_action(
    session: Session,
    engine: WorkflowEngine,
    action: CopilotProposedAction,
) -> dict[str, Any]:
    """Execute one Manager-confirmed Copilot action through orchestrator workflows."""
    params = _params_dict(action)
    atype = action.action_type

    if atype == CopilotActionType.CREATE_MANUAL_PO:
        part = _resolve_part(session, params.get("sku_or_part_id") or action.target_ref)
        sup_ref = params.get("supplier_key_or_id") or params.get("supplier")
        if sup_ref:
            supplier = _resolve_supplier(session, sup_ref)
        else:
            primary_term = session.scalar(
                select(PartSupplierTerm)
                .where(PartSupplierTerm.part_id == part.id)
                .order_by(PartSupplierTerm.supplier_rank.asc())
            )
            if primary_term is None:
                raise ValueError(f"No supplier configured for {part.sku}")
            supplier = session.get(Supplier, primary_term.supplier_id)
            assert supplier is not None

        qty_raw = params.get("qty")
        if qty_raw and qty_raw.isdigit() and int(qty_raw) > 0:
            qty = int(qty_raw)
        else:
            suggestion = compute_po_suggestion(session, part, supplier_id=supplier.id)
            qty = suggestion.suggested_qty

        term = session.scalar(
            select(PartSupplierTerm).where(
                PartSupplierTerm.part_id == part.id,
                PartSupplierTerm.supplier_id == supplier.id,
            )
        )
        unit_price = term.agreed_unit_price if term else 0.0
        try:
            draft = engine.generate_manual_po_draft(
                session, part_id=part.id, supplier_id=supplier.id, qty=qty
            )
            subject = str(draft["subject"])
            body = str(draft["body"])
        except Exception:
            subject = f"Purchase Order: {qty} units of {part.sku} ({part.name})"
            body = (
                f"Dear {supplier.name},\n\n"
                f"Please treat this email as our formal Purchase Order for **{qty}** units of "
                f"**{part.sku}** ({part.name}) at **₹{unit_price:,.2f}**/unit "
                f"(Total: **₹{qty * unit_price:,.2f}**).\n\n"
                "Please acknowledge receipt and confirm your expected dispatch and delivery "
                "schedule.\n\nBest regards,\n**Lex**\nAI Supply Chain Assistant"
            )
        res = engine.create_manual_po(
            session,
            part_id=part.id,
            supplier_id=supplier.id,
            qty=qty,
            subject=subject,
            body=body,
        )
        po = res["po"]
        return {
            "action_type": atype.value,
            "title": action.title,
            "status": "EXECUTED",
            "summary": (
                f"Created and dispatched {po.po_number} ({qty}u {part.sku} → {supplier.name})."
            ),
            "po_id": po.id,
            "po_number": po.po_number,
            "part_id": part.id,
        }

    if atype == CopilotActionType.APPROVE_DRAFT_EMAIL:
        comm_id = _resolve_pending_comm_id(session, params, action.target_ref)
        res = engine.approve_lex_draft(
            session,
            comm_id,
            edited_subject=params.get("subject"),
            edited_body=params.get("body"),
        )
        email = res["email"]
        return {
            "action_type": atype.value,
            "title": action.title,
            "status": "EXECUTED",
            "summary": (
                f"Approved and sent email #{email.id} ({email.category})"
                + (
                    f" — PO transitioned to {res['po_transitioned_to']}."
                    if res.get("po_transitioned_to")
                    else "."
                )
            ),
            "po_id": email.po_id,
        }

    if atype == CopilotActionType.REJECT_DRAFT_EMAIL:
        comm_id = _resolve_pending_comm_id(session, params, action.target_ref)
        res = engine.reject_lex_draft(session, comm_id)
        email = res["email"]
        return {
            "action_type": atype.value,
            "title": action.title,
            "status": "EXECUTED",
            "summary": f"Rejected draft email #{email.id}.",
            "po_id": email.po_id,
        }

    if atype == CopilotActionType.DISPATCH_EDITED_PO:
        po = _resolve_po(session, params.get("po_number_or_id") or action.target_ref)
        supplier_id = (
            _resolve_supplier(session, params["supplier_key_or_id"]).id
            if params.get("supplier_key_or_id")
            else None
        )
        qty = int(params["qty"]) if params.get("qty") else None
        unit_price = float(params["unit_price"]) if params.get("unit_price") else None
        res = engine.dispatch_edited_po(
            session,
            po.id,
            supplier_id=supplier_id,
            qty=qty,
            unit_price=unit_price,
            note=params.get("note"),
        )
        updated_po = res["po"]
        return {
            "action_type": atype.value,
            "title": action.title,
            "status": "EXECUTED",
            "summary": (
                f"Dispatched {updated_po.po_number} "
                f"({updated_po.qty}u @ ₹{updated_po.unit_price:,.2f})."
            ),
            "po_id": updated_po.id,
            "po_number": updated_po.po_number,
            "part_id": updated_po.part_id,
        }

    if atype == CopilotActionType.SWITCH_BACKUP_SUPPLIER:
        po = _resolve_po(session, params.get("po_number_or_id") or action.target_ref)
        res = engine.switch_to_backup_supplier(
            session, po.id, note=params.get("reason") or "Confirmed via Copilot"
        )
        new_po = res["new_po"]
        return {
            "action_type": atype.value,
            "title": action.title,
            "status": "EXECUTED",
            "summary": f"Cancelled {po.po_number} and created backup order {new_po.po_number}.",
            "po_id": new_po.id,
            "po_number": new_po.po_number,
            "part_id": new_po.part_id,
            "alert_id": res.get("alert_id"),
        }

    if atype == CopilotActionType.CANCEL_PO:
        po = _resolve_po(session, params.get("po_number_or_id") or action.target_ref)
        res = engine.cancel_po_workflow(
            session, po.id, note=params.get("reason") or "Cancelled via Copilot confirmation"
        )
        return {
            "action_type": atype.value,
            "title": action.title,
            "status": "EXECUTED",
            "summary": (
                f"Cancelled {res['po_number']} (resolved {res['resolved_alerts']} alert(s))."
            ),
            "po_id": po.id,
            "po_number": po.po_number,
            "part_id": po.part_id,
        }

    if atype == CopilotActionType.UPDATE_PO_NOTES_OR_ETA:
        po = _resolve_po(session, params.get("po_number_or_id") or action.target_ref)
        eta_dt = (
            parse_logistics_date(params["revised_eta"]) if params.get("revised_eta") else None
        )
        updated = update_po_terms(
            session,
            po.id,
            revised_eta=eta_dt,
            note=params.get("note") or None,
        )
        return {
            "action_type": atype.value,
            "title": action.title,
            "status": "EXECUTED",
            "summary": f"Updated {updated.po_number} terms/notes.",
            "po_id": updated.id,
            "po_number": updated.po_number,
        }

    if atype == CopilotActionType.WITHDRAW_STOCK:
        part = _resolve_part(session, params.get("sku_or_part_id") or action.target_ref)
        qty = int(params.get("qty") or "0")
        res = engine.withdraw_stock(
            session,
            part_id=part.id,
            qty=qty,
            note=params.get("note") or "Confirmed via Copilot",
        )
        snap = res["consume"].snapshot
        created_po = res.get("created_po")
        return {
            "action_type": atype.value,
            "title": action.title,
            "status": "EXECUTED",
            "summary": (
                f"Withdrew {qty}u {part.sku} (remaining On Hand: {snap.on_hand_stock}u)"
                + (f"; triggered draft {created_po.po_number}." if created_po else ".")
            ),
            "part_id": part.id,
            "po_id": created_po.id if created_po else None,
            "po_number": created_po.po_number if created_po else None,
        }

    if atype == CopilotActionType.RECORD_GRN:
        po = _resolve_po(session, params.get("po_number_or_id") or action.target_ref)
        qty = int(params.get("received_qty") or str(po.qty))
        condition = (params.get("condition") or "GOOD").strip().upper()
        res = engine.record_grn(session, po_id=po.id, received_qty=qty, condition=condition)
        return {
            "action_type": atype.value,
            "title": action.title,
            "status": "EXECUTED",
            "summary": (
                f"Logged GRN #{res['grn_id']} ({qty}u {condition}) on "
                f"{po.po_number} → {res['po_status']}."
            ),
            "po_id": po.id,
            "po_number": po.po_number,
            "part_id": po.part_id,
            "alert_id": res.get("alert_id"),
        }

    if atype == CopilotActionType.DISMISS_ALERT:
        raw_id = params.get("alert_id") or action.target_ref.replace("alert:", "")
        alert_id = int(raw_id)
        existing = session.get(ActionItem, alert_id)
        if existing is not None and existing.draft_comm_id is not None:
            comm = session.get(POCommunication, existing.draft_comm_id)
            if comm is not None and comm.status == CommStatus.DRAFT_PENDING_APPROVAL.value:
                reject_draft(session, comm.id)
        item = dismiss_action_item(session, alert_id)
        return {
            "action_type": atype.value,
            "title": action.title,
            "status": "EXECUTED",
            "summary": f"Dismissed alert #{item.id} ({item.title}).",
            "alert_id": item.id,
        }

    if atype == CopilotActionType.REOPEN_ALERT:
        raw_id = params.get("alert_id") or action.target_ref.replace("alert:", "")
        item = reopen_action_item(session, int(raw_id))
        return {
            "action_type": atype.value,
            "title": action.title,
            "status": "EXECUTED",
            "summary": f"Reopened alert #{item.id} ({item.title}).",
            "alert_id": item.id,
        }

    if atype == CopilotActionType.APPLY_ROP_RECOMMENDATION:
        part = _resolve_part(session, params.get("sku_or_part_id") or action.target_ref)
        res = engine.apply_rop_recommendation(session, part_id=part.id)
        return {
            "action_type": atype.value,
            "title": action.title,
            "status": "EXECUTED",
            "summary": f"Updated {part.sku} ROP/T to {res['rop']}/{res['replenishment_point']}.",
            "part_id": part.id,
        }

    if atype == CopilotActionType.RECALCULATE_PLANNING:
        res = engine.recalculate_planning(session)
        summary_obj = res["summary"]
        return {
            "action_type": atype.value,
            "title": action.title,
            "status": "EXECUTED",
            "summary": (
                f"Recalculated planning for {summary_obj.parts_evaluated} parts "
                f"({summary_obj.recommendations_needing_update} need ROP/T update)."
            ),
        }

    if atype == CopilotActionType.ADD_SUPPLIER_MEMORY_NOTE:
        supplier = _resolve_supplier(
            session, params.get("supplier_key_or_id") or action.target_ref
        )
        note_text = params.get("note") or action.summary
        res = propose_and_save_memory_note(
            session,
            supplier_id=supplier.id,
            note=note_text,
            reasoning=params.get("reasoning") or "Confirmed by Manager via Copilot",
            create_alert=False,
        )
        return {
            "action_type": atype.value,
            "title": action.title,
            "status": "EXECUTED",
            "summary": (
                f"Supplier memory ({res['action']}) for {supplier.name}: "
                f'"{res.get("note", note_text)}".'
            ),
        }

    if atype == CopilotActionType.SCHEDULE_LEX_REMINDER:
        po = _resolve_po(session, params.get("po_number_or_id") or action.target_ref)
        remind_dt = parse_iso_date_utc(params.get("remind_at_iso"))
        if remind_dt is None:
            raise ValueError("Invalid remind_at_iso date/datetime for reminder.")
        res = schedule_po_lex_reminder(
            session,
            po_id=po.id,
            remind_at=remind_dt,
            reason=params.get("reason") or action.summary,
        )
        return {
            "action_type": atype.value,
            "title": action.title,
            "status": "EXECUTED",
            "summary": (
                f"Scheduled reminder #{res['reminder_id']} for {po.po_number} "
                f"at {res['remind_at']}."
            ),
            "po_id": po.id,
            "po_number": po.po_number,
        }

    raise ValueError(f"Unsupported Copilot action type '{atype}'.")


def execute_copilot_actions(
    session: Session,
    engine: WorkflowEngine,
    actions: list[CopilotProposedAction],
) -> list[dict[str, Any]]:
    """Execute a batch of Manager-confirmed actions atomically inside the caller transaction."""
    if not actions:
        raise ValueError("No proposed actions to execute.")
    return [execute_single_copilot_action(session, engine, act) for act in actions]
