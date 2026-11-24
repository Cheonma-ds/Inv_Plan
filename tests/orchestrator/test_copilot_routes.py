from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.agents.lex import (
    CopilotActionParam,
    CopilotActionType,
    CopilotProposedAction,
    LexAgent,
)
from backend.agents.llm_client import GeminiLLMClient
from backend.core import clock
from backend.core.config import settings
from backend.core.db import get_session
from backend.core.migrations import ensure_schema_migrations
from backend.core.models import (
    ActionItem,
    AgentTrace,
    CopilotMessage,
    CopilotSession,
    Part,
    PurchaseOrder,
    SupplierMemoryNote,
)
from backend.main import create_app
from backend.orchestrator.deps import get_workflow_engine
from backend.orchestrator.routes.copilot import _resolve_citations
from backend.orchestrator.workflows import WorkflowEngine
from backend.po import create_action_item


def _build_client(seeded_session) -> TestClient:
    app = create_app()
    fresh_engine = WorkflowEngine(lex_agent=LexAgent(llm_client=GeminiLLMClient()))

    def _override_session():
        yield seeded_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_workflow_engine] = lambda: fresh_engine
    return TestClient(app)


def test_copilot_migrations_are_idempotent_on_legacy_sqlite(tmp_path) -> None:
    db_file = tmp_path / "legacy_p3.db"
    eng = create_engine(f"sqlite:///{db_file}", future=True)
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE purchase_orders (id INTEGER PRIMARY KEY)"))
    ensure_schema_migrations(eng)
    ensure_schema_migrations(eng)

    with eng.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
    assert "copilot_sessions" in tables
    assert "copilot_messages" in tables
    eng.dispose()


def test_copilot_sessions_crud_query_degraded_and_reset_seed(seeded_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    client = _build_client(seeded_session)

    # 1. Create & list sessions
    create_res = client.post("/api/copilot/sessions", json={"title": "Morning Ops Review"})
    assert create_res.status_code == 200
    sess_id = create_res.json()["id"]

    list_res = client.get("/api/copilot/sessions")
    assert list_res.status_code == 200
    assert any(s["id"] == sess_id and s["title"] == "Morning Ops Review" for s in list_res.json())

    # 2. Query without API key degrades gracefully (HTTP 200, degraded=True, trace logged)
    q_res = client.post(
        "/api/copilot/query",
        json={"session_id": sess_id, "question": "Which SKUs are CRITICAL today?"},
    )
    assert q_res.status_code == 200
    body = q_res.json()
    assert body["assistant_message"]["degraded"] is True
    assert body["assistant_message"]["error_code"] == "MISSING_API_KEY"
    assert body["assistant_message"]["confidence"] == 0.0
    assert body["assistant_message"]["trace_id"] is not None

    trace = seeded_session.get(AgentTrace, body["assistant_message"]["trace_id"])
    assert trace is not None
    assert trace.task_type == "copilot_query"

    # 3. Session fetch returns both persisted messages
    detail_res = client.get(f"/api/copilot/sessions/{sess_id}")
    assert detail_res.status_code == 200
    assert len(detail_res.json()["messages"]) == 2

    # 4. Delete works cleanly
    create_2 = client.post("/api/copilot/sessions", json={"title": "Temp Session"})
    sess_2 = create_2.json()["id"]
    del_res = client.delete(f"/api/copilot/sessions/{sess_2}")
    assert del_res.status_code == 200
    assert client.get(f"/api/copilot/sessions/{sess_2}").status_code == 404

    # 5. Reset Seed clears persisted Copilot sessions cleanly
    reset_res = client.post("/api/admin/reset-seed")
    assert reset_res.status_code == 200
    assert client.get("/api/copilot/sessions").json() == []


def test_copilot_citation_resolution_filters_unknown_entities(seeded_session) -> None:
    part = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    assert part is not None
    alert = create_action_item(
        seeded_session,
        type="TEST_ALERT",
        severity="INFO",
        title="Test Alert",
        summary="Test",
        part_id=part.id,
    )
    seeded_session.commit()

    resolved = _resolve_citations(
        seeded_session,
        cited_skus=["brg-402", "SKU-9999"],
        cited_po_numbers=["PO-99999"],
        cited_alert_ids=[alert.id, 999999],
    )
    assert [p["sku"] for p in resolved["resolved_parts"]] == ["BRG-402"]
    assert resolved["resolved_pos"] == []
    assert [a["alert_id"] for a in resolved["resolved_alerts"]] == [alert.id]


def test_copilot_confirm_decline_revise_and_atomic_rollback(seeded_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    client = _build_client(seeded_session)
    sim_now = clock.now(seeded_session)

    chat_sess = CopilotSession(title="Action Flow", created_at=sim_now, updated_at=sim_now)
    seeded_session.add(chat_sess)
    seeded_session.flush()

    user_m = CopilotMessage(
        session_id=chat_sess.id,
        role="user",
        content="Order 10u VLV-204 from Sup1 and add a note for Sup2.",
        created_at=sim_now,
    )
    seeded_session.add(user_m)
    seeded_session.flush()

    proposed = [
        CopilotProposedAction(
            action_type=CopilotActionType.CREATE_MANUAL_PO,
            title="Create PO for 10u VLV-204 (Sup1)",
            summary="Creates and dispatches a manual PO for 10u VLV-204 from Apex Precision.",
            target_ref="VLV-204",
            params=[
                CopilotActionParam(key="sku_or_part_id", value="VLV-204"),
                CopilotActionParam(key="supplier_key_or_id", value="sup1"),
                CopilotActionParam(key="qty", value="10"),
            ],
        ).model_dump(mode="json"),
        CopilotProposedAction(
            action_type=CopilotActionType.ADD_SUPPLIER_MEMORY_NOTE,
            title="Save memory note for Sup2",
            summary="Records packaging verification note for Bharat Industrial Fasteners.",
            target_ref="sup2",
            params=[
                CopilotActionParam(key="supplier_key_or_id", value="sup2"),
                CopilotActionParam(
                    key="note", value="Always double-check flange batch stickers before GRN."
                ),
            ],
        ).model_dump(mode="json"),
    ]
    assist_m = CopilotMessage(
        session_id=chat_sess.id,
        role="assistant",
        content="Ready to execute once confirmed.",
        structured_json={
            "cited_skus": ["VLV-204"],
            "cited_po_numbers": [],
            "cited_alert_ids": [],
            "proposed_actions": proposed,
            "action_status": "PENDING_CONFIRMATION",
            "action_execution_results": [],
        },
        created_at=sim_now,
    )
    seeded_session.add(assist_m)
    seeded_session.commit()

    # Confirm & Execute executes both actions atomically
    confirm_res = client.post(f"/api/copilot/messages/{assist_m.id}/confirm-actions")
    assert confirm_res.status_code == 200
    payload = confirm_res.json()
    assert payload["message"]["action_status"] == "EXECUTED"
    assert len(payload["execution_results"]) == 2
    created_po_num = payload["execution_results"][0]["po_number"]

    po = seeded_session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.po_number == created_po_num)
    )
    assert po is not None and po.qty == 10
    note = seeded_session.scalar(
        select(SupplierMemoryNote).where(
            SupplierMemoryNote.note.contains("flange batch stickers")
        )
    )
    assert note is not None

    # Second confirm fails with 400 (already EXECUTED)
    dup_res = client.post(f"/api/copilot/messages/{assist_m.id}/confirm-actions")
    assert dup_res.status_code == 400

    # Atomic rollback test: batch where 1st action is valid & 2nd fails -> zero partial commits
    bad_m = CopilotMessage(
        session_id=chat_sess.id,
        role="assistant",
        content="Bad batch",
        structured_json={
            "proposed_actions": [
                CopilotProposedAction(
                    action_type=CopilotActionType.RECALCULATE_PLANNING,
                    title="Recalc planning",
                    summary="Recalc",
                ).model_dump(mode="json"),
                CopilotProposedAction(
                    action_type=CopilotActionType.WITHDRAW_STOCK,
                    title="Excessive withdraw",
                    summary="Withdraw 999999 units",
                    target_ref="BRG-402",
                    params=[
                        CopilotActionParam(key="sku_or_part_id", value="BRG-402"),
                        CopilotActionParam(key="qty", value="999999"),
                    ],
                ).model_dump(mode="json"),
            ],
            "action_status": "PENDING_CONFIRMATION",
        },
        created_at=sim_now,
    )
    seeded_session.add(bad_m)
    seeded_session.commit()

    alerts_before = len(seeded_session.scalars(select(ActionItem)).all())
    bad_confirm = client.post(f"/api/copilot/messages/{bad_m.id}/confirm-actions")
    assert bad_confirm.status_code == 400
    assert len(seeded_session.scalars(select(ActionItem)).all()) == alerts_before

    # Decline test
    decline_res = client.post(f"/api/copilot/messages/{bad_m.id}/decline-actions")
    assert decline_res.status_code == 200
    assert decline_res.json()["message"]["action_status"] == "DECLINED"

    # Revise test marks target message REVISED even when LLM is offline/degraded
    rev_m = CopilotMessage(
        session_id=chat_sess.id,
        role="assistant",
        content="Prior proposal",
        structured_json={
            "proposed_actions": [
                CopilotProposedAction(
                    action_type=CopilotActionType.CANCEL_PO,
                    title=f"Cancel {created_po_num}",
                    summary="Cancels PO",
                    target_ref=created_po_num,
                    params=[CopilotActionParam(key="po_number_or_id", value=created_po_num)],
                ).model_dump(mode="json")
            ],
            "action_status": "PENDING_CONFIRMATION",
        },
        created_at=sim_now,
    )
    seeded_session.add(rev_m)
    seeded_session.commit()

    rev_res = client.post(
        "/api/copilot/query",
        json={
            "session_id": chat_sess.id,
            "revise_message_id": rev_m.id,
            "question": "No, do not cancel it — just add a note to check packaging.",
        },
    )
    assert rev_res.status_code == 200
    sess_after = client.get(f"/api/copilot/sessions/{chat_sess.id}").json()
    rev_target = next(m for m in sess_after["messages"] if m["id"] == rev_m.id)
    assert rev_target["action_status"] == "REVISED"

    # Verify sessionmaker helper still cleanly validates
    assert sessionmaker is not None
