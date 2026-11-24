from types import SimpleNamespace
from typing import Any, TypeVar

import pytest
from pydantic import BaseModel

from backend.agents.lex import (
    EmailIntent,
    EmailTriageInput,
    EmailTriageResult,
    LexAgent,
    OpenPOContext,
)
from backend.agents.llm_client import GeminiLLMClient
from backend.agents.sandbox import (
    SandboxExecutionError,
    SandboxSecurityError,
    SandboxTimeoutError,
    run_sandbox_python,
    validate_sandbox_ast,
)
from backend.agents.tools import ToolDefinition, ToolRegistry, ToolSpec
from backend.observability import list_traces

T = TypeVar("T", bound=BaseModel)


def test_tool_registry_executes_and_formats_traces() -> None:
    registry = ToolRegistry()
    assert ToolSpec is ToolDefinition

    def _add(a: int, b: int) -> dict[str, int]:
        return {"sum": a + b}

    registry.register(
        ToolSpec(
            name="add_numbers",
            description="Add two integers",
            parameters_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            },
        ),
        _add,
    )
    assert registry.has("add_numbers")
    assert registry.names() == ["add_numbers"]

    res_ok = registry.execute("add_numbers", {"a": 12, "b": 30})
    assert res_ok.ok is True
    assert res_ok.data == {"sum": 42}
    assert res_ok.error is None
    trace = ToolRegistry.format_trace_record(res_ok, {"a": 12, "b": 30})
    assert trace["tool"] == "add_numbers"
    assert trace["args"] == {"a": 12, "b": 30}
    assert '"sum":42' in trace["result_summary"]
    assert trace["error"] is None

    res_missing = registry.execute("nonexistent", {})
    assert res_missing.ok is False
    assert res_missing.error is not None and "Unknown tool" in res_missing.error

    def _boom(x: int) -> int:
        raise ValueError(f"bad x={x}")

    registry.register_function(
        name="boom",
        description="Raises ValueError",
        parameters_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
        handler=_boom,
    )
    res_err = registry.execute("boom", {"x": 5})
    assert res_err.ok is False
    assert "ValueError: bad x=5" in (res_err.error or "")


def test_sandbox_allows_safe_math_stats_dates_decimal_json() -> None:
    script = """
import math
import statistics
import json
from datetime import datetime, timedelta
from decimal import Decimal

avg = statistics.mean(series)
ceil_val = math.ceil(avg * days)
target_dt = datetime.fromisoformat(start_iso) + timedelta(days=days)
cost = Decimal("125.50") * Decimal(ceil_val)
payload = json.loads('{"tag": "ok"}')
result = {
    "ceil_val": ceil_val,
    "target_date": target_dt.date().isoformat(),
    "cost": float(cost),
    "tag": payload["tag"],
}
"""
    out = run_sandbox_python(
        script,
        variables={
            "series": [3.0, 4.0, 5.0],
            "days": 4,
            "start_iso": "2026-10-01T07:30:00+00:00",
        },
    )
    assert out.ok is True
    assert out.result == {
        "ceil_val": 16,
        "target_date": "2026-10-05",
        "cost": 2008.0,
        "tag": "ok",
    }

    # Pre-injected bindings without import statements also work + expression tail
    expr_out = run_sandbox_python(
        "round(on_hand / burn_rate, 2)",
        variables={"on_hand": 150, "burn_rate": 6.25},
    )
    assert expr_out.ok is True
    assert expr_out.result == 24.0


@pytest.mark.parametrize(
    "bad_script",
    [
        "import os\nresult = os.listdir('.')",
        "import sys\nresult = sys.version",
        "import subprocess\nresult = subprocess.run(['ls'])",
        "import socket\nresult = socket.gethostname()",
        "from os import path\nresult = path.exists('.')",
        "result = open('/etc/passwd').read()",
        "result = eval('1 + 1')",
        "exec('x = 1')",
        "result = __import__('os')",
        "result = ().__class__.__bases__[0].__subclasses__()",
        "from datetime import datetime\nresult = datetime.now().isoformat()",
        "result = 'a' * (10 ** 9)",
        "result = 2 ** (10 ** 6)",
    ],
)
def test_sandbox_blocks_unsafe_imports_io_network_eval_and_escapes(bad_script: str) -> None:
    with pytest.raises(SandboxSecurityError):
        run_sandbox_python(bad_script)


def test_sandbox_validate_ast_and_timeout_and_runtime_errors() -> None:
    validate_sandbox_ast("x = 1 + 2\nresult = x")

    with pytest.raises(SandboxTimeoutError):
        run_sandbox_python("while True:\n    pass", timeout_seconds=0.05)

    # Even if script catches Exception, sandbox blocks broad catch & enforces timeout
    with pytest.raises(SandboxSecurityError):
        run_sandbox_python("try:\n    x = 1 / 0\nexcept Exception:\n    x = 0")

    with pytest.raises(SandboxExecutionError):
        run_sandbox_python("result = 1 / 0")


def test_gemini_client_generate_with_tools_loop_and_agent_tracer(seeded_session) -> None:
    registry = ToolRegistry()
    registry.register_function(
        name="run_sandbox_calc",
        description="Compute runway math",
        parameters_schema={
            "type": "object",
            "properties": {
                "python_script": {"type": "string"},
                "variables": {"type": "object"},
            },
            "required": ["python_script"],
        },
        handler=lambda python_script, variables=None: run_sandbox_python(
            python_script, variables or {}
        ).model_dump(mode="json"),
    )

    canned = EmailTriageResult(
        matched_po_number="PO-1037",
        intent=EmailIntent.ACKNOWLEDGED,
        confidence=0.96,
        proposed_eta_date="2026-10-14",
        details_promised_later=True,
        details_promised_days=4,
        details_promised_by_date="2026-10-05",
        summary_for_manager="Confirmed PO-1037 with 4-day dispatch details deferral.",
        suggested_action="Schedule 4-day Lex reminder.",
        reasoning="Runway verified via sandbox.",
    )

    calls: list[dict[str, Any]] = []

    class _FakeModels:
        def generate_content(self, *, model, contents, config):
            del model
            calls.append({"contents": list(contents), "config": config})
            step = len(calls)
            if step == 1:
                fc = SimpleNamespace(
                    name="run_sandbox_calc",
                    args={
                        "python_script": "result = round(on_hand / burn, 2)",
                        "variables": {"on_hand": 150, "burn": 6.25},
                    },
                )
                return SimpleNamespace(
                    function_calls=[fc],
                    candidates=[],
                    text=None,
                    usage_metadata=SimpleNamespace(
                        prompt_token_count=120,
                        candidates_token_count=30,
                    ),
                )
            if step == 2:
                # Simulate model emitting prose after tool call -> triggers final structured call
                return SimpleNamespace(
                    function_calls=[],
                    candidates=[],
                    text="Runway is 24.0 days, which safely covers the 4-day wait.",
                    usage_metadata=SimpleNamespace(
                        prompt_token_count=180,
                        candidates_token_count=25,
                    ),
                )
            return SimpleNamespace(
                function_calls=[],
                text=canned.model_dump_json(),
                parsed=canned,
                usage_metadata=SimpleNamespace(prompt_token_count=200, candidates_token_count=45),
            )

    client = GeminiLLMClient(api_key="test-key")
    client._client = SimpleNamespace(models=_FakeModels())

    lex = LexAgent(llm_client=client)
    res = lex.triage_email(
        seeded_session,
        EmailTriageInput(
            sender_persona="sup3",
            sender_name="Deccan Electro-Spares",
            subject="Re: PO-1037",
            body="Confirmed PO-1037; dispatch details after 4 days.",
            current_date="2026-10-01",
            open_pos=[
                OpenPOContext(
                    po_number="PO-1037",
                    part_sku="RLY-901",
                    part_name="PLC Safety Relay Module 24V",
                    qty=15,
                    unit_price=780.0,
                    status="SENT_AWAITING_ACK",
                    quoted_lead_time_days=14,
                )
            ],
        ),
        tool_registry=registry,
    )

    assert res.intent == EmailIntent.ACKNOWLEDGED
    assert res.details_promised_days == 4
    assert len(calls) == 3

    traces = list_traces(seeded_session, agent_name="lex", task_type="email_triage", limit=1)
    assert len(traces) == 1
    trace = traces[0]
    assert trace.input_tokens == 500
    assert trace.output_tokens == 100
    assert len(trace.tool_calls_json) == 1
    assert trace.tool_calls_json[0]["tool"] == "run_sandbox_calc"
    assert '"result":24.0' in trace.tool_calls_json[0]["result_summary"]
    assert trace.tool_calls_json[0]["error"] is None
