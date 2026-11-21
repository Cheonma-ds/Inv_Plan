import json
from typing import TypeVar

from fastapi.testclient import TestClient
from pydantic import BaseModel

from backend.agents.lex import EmailDraftInput, EmailDraftResult, LexAgent
from backend.agents.llm_client import LLMResult
from backend.core.db import get_session
from backend.core.enums import EmailCategory, PersonaKey
from backend.mailroom import OutboundMessage, save_draft
from backend.main import create_app
from backend.orchestrator.deps import get_workflow_engine
from backend.orchestrator.workflows import WorkflowEngine

T = TypeVar("T", bound=BaseModel)


class _DraftMockLLM:
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
        result = EmailDraftResult(
            to_personas=[payload["recipient_persona"]],
            cc_personas=[PersonaKey.INVENTORY_MANAGER.value],
            subject=f"[{payload['po_number']}] Follow-up",
            body=f"Hello, regarding {payload['po_number']}.",
        )
        return LLMResult(
            parsed=result,  # type: ignore[arg-type]
            raw_text=result.model_dump_json(),
            input_tokens=210,
            output_tokens=65,
            latency_ms=15,
            cost_inr=0.003,
            model_name="mock-gemini",
        )


def _make_client(seeded_session) -> TestClient:
    app = create_app()

    def _override_session():
        yield seeded_session

    def _override_engine():
        return WorkflowEngine(lex_agent=LexAgent(llm_client=_DraftMockLLM()))

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_workflow_engine] = _override_engine
    return TestClient(app)


def test_observability_status_traces_and_eval_endpoints(seeded_session) -> None:
    with _make_client(seeded_session) as client:
        status_res = client.get("/api/observability/status")
        assert status_res.status_code == 200
        status_body = status_res.json()
        assert "lex" in status_body["registered_agents"]
        assert status_body["phoenix"]["mode"] in {"disabled", "active", "fallback_sqlite"}

        suite_res = client.get("/api/evals/lex/suite")
        assert suite_res.status_code == 200
        expected_cases = suite_res.json()["total_cases"]
        assert expected_cases >= 20

        missing_res = client.get("/api/evals/unknown_agent/suite")
        assert missing_res.status_code == 404

        run_res = client.post("/api/evals/lex/run", json={"mode": "offline"})
        assert run_res.status_code == 200
        run_data = run_res.json()
        assert run_data["agent_name"] == "lex"
        assert run_data["mode"] == "offline"
        assert run_data["total_cases"] == expected_cases
        assert run_data["passed_cases"] == expected_cases
        assert run_data["pass_rate"] == 100.0

        history_res = client.get("/api/evals/lex/run")
        assert history_res.status_code == 200
        history = history_res.json()
        assert len(history) == 1
        assert history[0]["id"] == run_data["id"]

        traces_res = client.get("/api/observability/traces?agent_name=lex&limit=50")
        assert traces_res.status_code == 200
        traces = traces_res.json()
        assert len(traces) == expected_cases
        assert "human_feedback" in traces[0]

        filtered_traces = client.get("/api/observability/traces?task_type=invoice_extraction")
        assert filtered_traces.status_code == 200
        assert len(filtered_traces.json()) == 4


def test_autonomy_metrics_update_on_approve_edit_and_reject(seeded_session) -> None:
    lex = LexAgent(llm_client=_DraftMockLLM())

    def _create_draft(category: EmailCategory, po_number: str) -> int:
        draft = lex.draft_email(
            seeded_session,
            EmailDraftInput(
                category=category,
                po_number=po_number,
                part_sku="SKU-001",
                part_name="Precision Ball Bearing 6205",
                qty=20,
                unit_price=1250.0,
                recipient_persona=PersonaKey.SUP1.value,
                recipient_name="Apex Precision Works",
                current_date="2026-11-20",
            ),
        )
        stored = save_draft(
            seeded_session,
            OutboundMessage(
                from_persona=PersonaKey.LEX.value,
                to_personas=draft.to_personas,
                cc_personas=draft.cc_personas,
                subject=draft.subject,
                body=draft.body,
                category=category,
            ),
        )
        seeded_session.commit()
        return stored.id

    d1 = _create_draft(EmailCategory.ACK_REMINDER, "PO-1037")
    d2 = _create_draft(EmailCategory.ACK_REMINDER, "PO-1037")
    d3 = _create_draft(EmailCategory.PRE_ETA_CHECKIN, "PO-1036")

    with _make_client(seeded_session) as client:
        # 1. Approve d1 as-is
        r1 = client.post(f"/api/mailroom/drafts/{d1}/approve", json={})
        assert r1.status_code == 200
        assert r1.json()["approval_outcome"] == "APPROVED_AS_IS"

        # 2. Approve d2 with edited body
        r2 = client.post(
            f"/api/mailroom/drafts/{d2}/approve",
            json={"edited_body": "Edited body from Inventory Manager."},
        )
        assert r2.status_code == 200
        assert r2.json()["approval_outcome"] == "APPROVED_EDITED"

        # 3. Reject d3
        r3 = client.post(f"/api/mailroom/drafts/{d3}/reject")
        assert r3.status_code == 200
        assert r3.json()["approval_outcome"] == "REJECTED"

        autonomy_res = client.get("/api/observability/autonomy?agent_name=lex")
        assert autonomy_res.status_code == 200
        policies = {p["action_category"]: p for p in autonomy_res.json()}

        ack_pol = policies[EmailCategory.ACK_REMINDER.value]
        assert ack_pol["approved_as_is_count"] == 1
        assert ack_pol["approved_edited_count"] == 1
        assert ack_pol["rejected_count"] == 0
        assert ack_pol["total_decisions"] == 2
        assert ack_pol["edit_free_approval_rate"] == 50.0

        checkin_pol = policies[EmailCategory.PRE_ETA_CHECKIN.value]
        assert checkin_pol["rejected_count"] == 1
        assert checkin_pol["total_decisions"] == 1
        assert checkin_pol["edit_free_approval_rate"] == 0.0
