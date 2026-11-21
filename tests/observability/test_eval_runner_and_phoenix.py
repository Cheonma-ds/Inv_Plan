import sys
import types
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend.agents.lex import LexEvalExecutor
from backend.core.config import settings
from backend.observability import (
    AgentTracer,
    evaluate_expectations,
    get_phoenix_status,
    list_eval_runs,
    list_traces,
    load_eval_suite,
    run_eval_suite,
    serialize_eval_run,
)
from backend.observability import tracer as tracer_mod

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _StubGenericExecutor:
    def __init__(self, outputs: dict[str, dict[str, Any]], fail_task: str | None = None) -> None:
        self.outputs = outputs
        self.fail_task = fail_task

    def execute_eval_case(
        self,
        session: Session,
        *,
        task: str,
        case_input: dict[str, Any],
        mode: str,
        mock_response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if task == self.fail_task:
            raise RuntimeError("simulated executor crash")
        return self.outputs[str(case_input["key"])]


def test_evaluate_expectations_supports_all_rules_and_reports_failures() -> None:
    output = {
        "intent": "PRICE_CHANGE",
        "confidence": 0.91,
        "proposed_unit_price": 135.004,
        "line_items": [{"qty": 10, "unit_price": 135.0}],
        "cc_personas": ["inventory_manager"],
        "body": "We cannot accept the new unit price without manager approval.",
    }
    expect = {
        "intent": "PRICE_CHANGE",
        "min_confidence": 0.85,
        "proposed_unit_price": {"approx": 135.0, "tol": 0.01},
        "line_items.0.qty": 10,
        "cc_personas": {"contains": "inventory_manager"},
        "body": {
            "not_null": True,
            "min_len": 20,
            "not_contains_any_ci": ["we accept the price hike", "approved for payment"],
        },
    }
    results = evaluate_expectations(output, expect)
    assert len(results) == 8
    assert all(r.passed for r in results)

    failing = evaluate_expectations(
        {"intent": "ACKNOWLEDGED", "confidence": 0.72, "body": "We accept the overcharge."},
        {
            "intent": "PRICE_CHANGE",
            "min_confidence": 0.85,
            "body": {"not_contains_any_ci": ["we accept the overcharge"]},
        },
    )
    assert all(not r.passed for r in failing)
    assert "expected 'PRICE_CHANGE', got 'ACKNOWLEDGED'" in failing[0].message
    assert "expected >= 0.85, got 0.72" in failing[1].message
    assert "forbidden phrase" in failing[2].message


def test_run_eval_suite_persists_eval_run_and_handles_failures(seeded_session, tmp_path) -> None:
    suite_file = tmp_path / "generic_agent.yaml"
    suite_file.write_text(
        """
suite_name: generic_smoke
agent_name: planner_copilot
cases:
  - id: case_pass
    name: "Passing case"
    task: analyze
    input: { key: a }
    expect: { status: ok, min_confidence: 0.8 }
  - id: case_fail
    name: "Assertion failure case"
    task: analyze
    input: { key: b }
    expect: { status: ok }
  - id: case_error
    name: "Raised exception case"
    task: crash_task
    input: { key: a }
    expect: { status: ok }
""",
        encoding="utf-8",
    )

    executor = _StubGenericExecutor(
        outputs={
            "a": {"status": "ok", "confidence": 0.9},
            "b": {"status": "bad", "confidence": 0.9},
        },
        fail_task="crash_task",
    )
    report = run_eval_suite(
        seeded_session,
        agent_name="planner_copilot",
        suite_path=suite_file,
        executor=executor,
        mode="offline",
    )
    seeded_session.commit()

    assert report.agent_name == "planner_copilot"
    assert report.total_cases == 3
    assert report.passed_cases == 1
    assert report.pass_rate == 33.3
    assert report.results[0].passed is True
    assert report.results[1].passed is False
    assert "expected 'ok', got 'bad'" in report.results[1].reason
    assert report.results[2].passed is False
    assert "Execution error: simulated executor crash" in report.results[2].reason

    stored = list_eval_runs(seeded_session, agent_name="planner_copilot")
    assert len(stored) == 1
    serialized = serialize_eval_run(stored[0])
    assert serialized["passed_cases"] == 1
    assert serialized["total_cases"] == 3


def test_lex_yaml_suite_runs_cleanly_offline_and_logs_traces(seeded_session) -> None:
    suite_path = PROJECT_ROOT / "backend" / "evals" / "lex.yaml"
    suite = load_eval_suite(suite_path)
    expected_count = len(suite.cases)
    assert expected_count >= 20

    report = run_eval_suite(
        seeded_session,
        agent_name="lex",
        suite_path=suite_path,
        executor=LexEvalExecutor(),
        mode="offline",
    )
    seeded_session.commit()

    failures = [f"{r.id}: {r.reason}" for r in report.results if not r.passed]
    assert not failures, "Lex offline eval failures:\n" + "\n".join(failures)
    assert report.total_cases == expected_count
    assert report.passed_cases == expected_count
    assert report.pass_rate == 100.0

    traces = list_traces(seeded_session, agent_name="lex", limit=100)
    assert len(traces) == expected_count
    task_types = {t.task_type for t in traces}
    assert {
        "email_triage",
        "invoice_extraction",
        "email_draft",
        "stock_request_triage",
    }.issubset(task_types)


def test_phoenix_tracing_disabled_and_enabled_hooks(seeded_session, monkeypatch) -> None:
    # 1. Default disabled mode
    monkeypatch.setattr(settings, "enable_phoenix", False)
    monkeypatch.setattr(tracer_mod, "_phoenix_initialized", False)
    status_off = get_phoenix_status()
    assert status_off["enable_phoenix"] is False
    assert status_off["mode"] == "disabled"

    with AgentTracer.span(
        seeded_session,
        agent_name="lex",
        task_type="email_triage",
        trigger_summary="Disabled phoenix test",
    ) as span:
        span.output_summary = "ok"
    assert span.trace_id is not None

    # 2. Enabled mode with mock phoenix.otel.register
    calls: list[str] = []
    fake_phoenix = types.ModuleType("phoenix")
    fake_phoenix_otel = types.ModuleType("phoenix.otel")

    def _fake_register(*, project_name: str) -> None:
        calls.append(project_name)

    fake_phoenix_otel.register = _fake_register  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "phoenix", fake_phoenix)
    monkeypatch.setitem(sys.modules, "phoenix.otel", fake_phoenix_otel)
    monkeypatch.setattr(settings, "enable_phoenix", True)
    monkeypatch.setattr(tracer_mod, "_phoenix_initialized", False)

    with AgentTracer.span(
        seeded_session,
        agent_name="lex",
        task_type="email_triage",
        trigger_summary="Enabled phoenix test",
    ) as span2:
        span2.output_summary = "ok-otel"
    assert calls == ["inv-plan"]
    assert span2.trace_id is not None

    # 3. Enabled mode degrades gracefully when phoenix.otel.register raises
    def _broken_register(*, project_name: str) -> None:
        raise RuntimeError("phoenix server offline")

    fake_phoenix_otel.register = _broken_register  # type: ignore[attr-defined]
    monkeypatch.setattr(tracer_mod, "_phoenix_initialized", False)
    with AgentTracer.span(
        seeded_session,
        agent_name="lex",
        task_type="email_triage",
        trigger_summary="Graceful fallback test",
    ) as span3:
        span3.output_summary = "fallback-ok"
    assert span3.trace_id is not None
