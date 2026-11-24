import json
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
    CommStatus,
    POStatus,
)
from backend.core.models import (
    ActionItem,
    Part,
    PartSupplierTerm,
    POCommunication,
    PurchaseOrder,
)
from backend.main import create_app
from backend.orchestrator.deps import get_workflow_engine
from backend.orchestrator.workflows import WorkflowEngine
from backend.po import create_action_item

T = TypeVar("T", bound=BaseModel)


class _MockDraftLLM:
    def generate_structured(
        self,
        *,
        prompt: str,
        response_schema: type[T],
        system_instruction: str | None = None,
        temperature: float = 0.0,
        pdf_bytes: bytes | None = None,
    ) -> LLMResult[T]:
        payload = json.loads(prompt)
        po_num = payload.get("po_number", "")
        recipient = payload.get("recipient_persona", "sup1")
        result = EmailDraftResult(
            to_personas=[recipient],
            cc_personas=["inventory_manager"],
            subject=f"[{po_num}] Purchase Order {po_num}".strip(),
            body=(
                f"Dear {payload.get('recipient_name', 'Supplier')},\n\n"
                f"Please confirm PO {po_num} for {payload.get('qty')} units.\n\n"
                "Best regards,\nLex"
            ),
        )
        return LLMResult(
            parsed=result,  # type: ignore[arg-type]
            raw_text=result.model_dump_json(),
            input_tokens=50,
            output_tokens=50,
            latency_ms=1,
            cost_inr=0.0,
            model_name="mock-gemini",
        )


def _make_client(session, engine: WorkflowEngine | None = None) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    resolved_engine = engine or WorkflowEngine(lex_agent=LexAgent(llm_client=_MockDraftLLM()))
    app.dependency_overrides[get_workflow_engine] = lambda: resolved_engine
    return TestClient(app)


def test_po_dispatch_approval_exposes_edit_po_and_dispatch_edited_flow(seeded_session):
    client = _make_client(seeded_session)
    part = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    assert part is not None

    # Trigger auto-replenishment by withdrawing enough stock to drop below ROP
    withdraw_qty = max(1, part.on_hand_stock - max(0, part.rop - 2))
    resp = client.post(
        f"/api/parts/{part.id}/consume",
        json={"qty": withdraw_qty, "note": "Test auto replenishment"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["created_po"] is not None
    po_id = data["created_po"]["id"]
    orig_draft_comm_id = data["draft_comm_id"]
    assert orig_draft_comm_id is not None

    # 1. PO_DISPATCH_APPROVAL alert exposes edit_po instead of reject_draft
    alerts_resp = client.get("/api/alerts", params={"status": "OPEN"})
    assert alerts_resp.status_code == 200
    po_alerts = [
        a for a in alerts_resp.json() if a["type"] == "PO_DISPATCH_APPROVAL" and a["po_id"] == po_id
    ]
    assert len(po_alerts) == 1
    alert = po_alerts[0]
    action_keys = [a["action"] for a in alert["suggested_actions"]]
    assert "approve_draft" in action_keys
    assert "edit_po" in action_keys
    assert "reject_draft" not in action_keys

    # Find backup supplier term for this part
    terms = list(
        seeded_session.scalars(
            select(PartSupplierTerm)
            .where(PartSupplierTerm.part_id == part.id)
            .order_by(PartSupplierTerm.supplier_rank.asc())
        ).all()
    )
    assert len(terms) >= 2
    backup_term = terms[1]

    po_count_before = len(
        seeded_session.scalars(select(PurchaseOrder).where(PurchaseOrder.part_id == part.id)).all()
    )

    # 2. POST /api/pos/{po_id}/dispatch-edited mutates & dispatches existing PO
    edit_resp = client.post(
        f"/api/pos/{po_id}/dispatch-edited",
        json={
            "supplier_id": backup_term.supplier_id,
            "qty": backup_term.moq + 5,
            "subject": "Edited PO Dispatch",
            "body": "Please confirm this updated purchase order.",
            "note": "Switched supplier and adjusted qty via Edit PO.",
        },
    )
    assert edit_resp.status_code == 200
    edit_data = edit_resp.json()
    assert edit_data["po"]["id"] == po_id
    assert edit_data["po"]["status"] == POStatus.SENT_AWAITING_ACK.value
    assert edit_data["po"]["supplier_id"] == backup_term.supplier_id
    assert edit_data["po"]["qty"] == backup_term.moq + 5
    assert edit_data["po"]["unit_price"] == backup_term.agreed_unit_price
    assert edit_data["po"]["promised_eta"] is not None
    assert orig_draft_comm_id in edit_data["superseded_comm_ids"]
    assert edit_data["resolved_alerts"] == 1

    # No duplicate PO created
    po_count_after = len(
        seeded_session.scalars(select(PurchaseOrder).where(PurchaseOrder.part_id == part.id)).all()
    )
    assert po_count_after == po_count_before

    # Old draft comm is REJECTED; next_sla_check_at is set
    old_comm = seeded_session.get(POCommunication, orig_draft_comm_id)
    assert old_comm is not None
    assert old_comm.status == CommStatus.REJECTED.value

    po_obj = seeded_session.get(PurchaseOrder, po_id)
    assert po_obj is not None
    assert po_obj.next_sla_check_at is not None

    # Parent alert is RESOLVED
    alert_obj = seeded_session.get(ActionItem, alert["id"])
    assert alert_obj is not None
    assert alert_obj.status == ActionItemStatus.RESOLVED.value


def test_edit_po_cta_dispatches_existing_draft_po(seeded_session):
    client = _make_client(seeded_session)
    part = seeded_session.scalar(select(Part).where(Part.sku == "FLT-108"))
    assert part is not None

    withdraw_qty = max(1, part.on_hand_stock - max(0, part.rop - 2))
    resp = client.post(
        f"/api/parts/{part.id}/consume",
        json={"qty": withdraw_qty, "note": "Trigger draft PO for edit_po CTA"},
    )
    assert resp.status_code == 200
    po_id = resp.json()["created_po"]["id"]
    alert_id = resp.json()["alert_ids"][0]

    cta_resp = client.post(
        f"/api/alerts/{alert_id}/execute-cta",
        json={
            "action": "edit_po",
            "params": {"qty": 55, "subject": "Edited via CTA", "body": "Confirmed PO qty 55."},
        },
    )
    assert cta_resp.status_code == 200
    cta_data = cta_resp.json()
    assert cta_data["po_id"] == po_id
    assert cta_data["po_status"] == POStatus.SENT_AWAITING_ACK.value

    po_obj = seeded_session.get(PurchaseOrder, po_id)
    assert po_obj is not None
    assert po_obj.qty == 55
    alert_obj = seeded_session.get(ActionItem, alert_id)
    assert alert_obj is not None
    assert alert_obj.status == ActionItemStatus.RESOLVED.value


def test_reject_lex_draft_does_not_resolve_parent_alert(seeded_session):
    client = _make_client(seeded_session)
    part = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    assert part is not None

    withdraw_qty = max(1, part.on_hand_stock - max(0, part.rop - 2))
    resp = client.post(
        f"/api/parts/{part.id}/consume",
        json={"qty": withdraw_qty, "note": "Trigger PO for reject_lex_draft"},
    )
    assert resp.status_code == 200
    draft_comm_id = resp.json()["draft_comm_id"]
    alert_id = resp.json()["alert_ids"][0]

    reject_resp = client.post(f"/api/mailroom/drafts/{draft_comm_id}/reject")
    assert reject_resp.status_code == 200
    assert reject_resp.json()["approval_outcome"] == "REJECTED"

    comm = seeded_session.get(POCommunication, draft_comm_id)
    assert comm is not None
    assert comm.status == CommStatus.REJECTED.value

    alert = seeded_session.get(ActionItem, alert_id)
    assert alert is not None
    assert alert.status == ActionItemStatus.OPEN.value
    assert alert.resolved_at is None


def test_dismiss_hides_draft_surfaces_part_warning_and_supports_reopen_and_ack(seeded_session):
    client = _make_client(seeded_session)
    part = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    assert part is not None

    withdraw_qty = max(1, part.on_hand_stock - max(0, part.rop - 2))
    resp = client.post(
        f"/api/parts/{part.id}/consume",
        json={"qty": withdraw_qty, "note": "Trigger PO for dismiss lifecycle"},
    )
    assert resp.status_code == 200
    po_id = resp.json()["created_po"]["id"]
    draft_comm_id = resp.json()["draft_comm_id"]
    alert_id = resp.json()["alert_ids"][0]

    # Dismiss alert
    dismiss_resp = client.post(f"/api/alerts/{alert_id}/dismiss")
    assert dismiss_resp.status_code == 200
    assert dismiss_resp.json()["status"] == ActionItemStatus.DISMISSED.value

    # Linked draft comm is rejected
    comm = seeded_session.get(POCommunication, draft_comm_id)
    assert comm is not None
    assert comm.status == CommStatus.REJECTED.value

    # GET /api/alerts?status=DISMISSED returns draft_preview = None
    dismissed_list = client.get("/api/alerts", params={"status": "DISMISSED"}).json()
    matched = [a for a in dismissed_list if a["id"] == alert_id]
    assert len(matched) == 1
    assert matched[0]["draft_preview"] is None

    # GET /api/parts and GET /api/parts/{id} surface unresolved_dismissed_alerts
    parts_list = client.get("/api/parts").json()
    part_row = next(p for p in parts_list if p["part_id"] == part.id)
    assert len(part_row["unresolved_dismissed_alerts"]) == 1
    assert part_row["unresolved_dismissed_alerts"][0]["id"] == alert_id

    part_detail = client.get(f"/api/parts/{part.id}").json()
    assert len(part_detail["unresolved_dismissed_alerts"]) == 1

    # Reopen alert -> back to OPEN, draft restored, removed from unresolved_dismissed_alerts
    reopen_resp = client.post(f"/api/alerts/{alert_id}/reopen")
    assert reopen_resp.status_code == 200
    assert reopen_resp.json()["status"] == ActionItemStatus.OPEN.value
    seeded_session.refresh(comm)
    assert comm.status == CommStatus.DRAFT_PENDING_APPROVAL.value
    part_detail_after_reopen = client.get(f"/api/parts/{part.id}").json()
    assert part_detail_after_reopen["unresolved_dismissed_alerts"] == []

    # Dismiss again, then acknowledge-dismissed -> keeps DISMISSED, sets dismiss_resolved_at
    assert client.post(f"/api/alerts/{alert_id}/dismiss").status_code == 200
    ack_resp = client.post(f"/api/alerts/{alert_id}/acknowledge-dismissed")
    assert ack_resp.status_code == 200
    assert ack_resp.json()["status"] == ActionItemStatus.DISMISSED.value
    assert ack_resp.json()["dismiss_resolved_at"] is not None
    part_detail_after_ack = client.get(f"/api/parts/{part.id}").json()
    assert part_detail_after_ack["unresolved_dismissed_alerts"] == []

    # Now test auto-clearing dismiss_resolved_at when PO transitions to SENT_AWAITING_ACK
    assert client.post(f"/api/alerts/{alert_id}/reopen").status_code == 200
    assert client.post(f"/api/alerts/{alert_id}/dismiss").status_code == 200
    assert len(client.get(f"/api/parts/{part.id}").json()["unresolved_dismissed_alerts"]) == 1

    dispatch_resp = client.post(
        f"/api/pos/{po_id}/dispatch-edited",
        json={"subject": "Dispatching after dismiss", "body": "Order confirmed."},
    )
    assert dispatch_resp.status_code == 200

    # Warning auto-cleared and dismiss_resolved_at populated
    part_detail_final = client.get(f"/api/parts/{part.id}").json()
    assert part_detail_final["unresolved_dismissed_alerts"] == []
    alert_obj = seeded_session.get(ActionItem, alert_id)
    assert alert_obj is not None
    assert alert_obj.status == ActionItemStatus.DISMISSED.value
    assert alert_obj.dismiss_resolved_at is not None


def test_part_level_dismissed_alert_auto_resolves_when_stock_replenished(seeded_session):
    client = _make_client(seeded_session)
    part = seeded_session.scalar(select(Part).where(Part.sku == "WSH-104"))
    assert part is not None

    # Create a part-level STOCKOUT_ACTIVE alert while part is below ROP
    part.on_hand_stock = 0
    seeded_session.flush()
    alert = create_action_item(
        seeded_session,
        type="STOCKOUT_ACTIVE",
        severity=AlertSeverity.CRITICAL,
        part_criticality=part.criticality,
        title=f"Stockout active: {part.sku}",
        summary="On hand is 0.",
        part_id=part.id,
        suggested_actions=[],
    )
    seeded_session.commit()

    assert client.post(f"/api/alerts/{alert.id}/dismiss").status_code == 200
    part_data = client.get(f"/api/parts/{part.id}").json()
    assert len(part_data["unresolved_dismissed_alerts"]) == 1

    # Replenish stock above ROP
    part.on_hand_stock = part.rop + 20
    seeded_session.commit()

    part_data_after = client.get(f"/api/parts/{part.id}").json()
    assert part_data_after["unresolved_dismissed_alerts"] == []
    refreshed_alert = seeded_session.get(ActionItem, alert.id)
    assert refreshed_alert is not None
    assert refreshed_alert.dismiss_resolved_at is not None


def test_rop_recommendation_uses_rop_t_naming(seeded_session):
    client = _make_client(seeded_session)
    part = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    assert part is not None

    # Perturb ROP so recalculate_planning emits a ROP_RECOMMENDATION alert
    part.rop = max(1, part.rop - 10)
    part.replenishment_point = max(part.rop + 1, part.replenishment_point - 10)
    seeded_session.commit()

    engine = WorkflowEngine(lex_agent=LexAgent(llm_client=_MockDraftLLM()))
    engine.recalculate_planning(seeded_session)
    seeded_session.commit()

    alerts = client.get("/api/alerts", params={"status": "OPEN"}).json()
    rop_alerts = [
        a for a in alerts if a["type"] == "ROP_RECOMMENDATION" and a["part_id"] == part.id
    ]
    assert len(rop_alerts) == 1
    assert "ROP/T" in rop_alerts[0]["title"]
    assert "ROP/S" not in rop_alerts[0]["title"]
    assert " T " in rop_alerts[0]["title"]
    assert " T " in rop_alerts[0]["suggested_actions"][0]["label"]
    _ = clock.now(seeded_session)
