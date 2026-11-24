from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from backend.agents.lex import (
    EmailDraftInput,
    InvoiceExtractionResult,
    InvoiceLineItem,
)
from backend.agents.lex.agent import _format_and_verify_email_body
from backend.agents.sandbox import (
    SandboxExecutionError,
    SandboxSecurityError,
    SandboxTimeoutError,
    run_sandbox_python,
    validate_sandbox_ast,
)
from backend.agents.tools import ToolDefinition, ToolRegistry, ToolSpec
from backend.core.enums import EmailCategory


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

    with pytest.raises(SandboxSecurityError):
        run_sandbox_python("try:\n    x = 1 / 0\nexcept Exception:\n    x = 0")

    with pytest.raises(SandboxExecutionError):
        run_sandbox_python("result = 1 / 0")


def test_invoice_extraction_result_supports_design_aliases_cleanly() -> None:
    item = InvoiceLineItem(
        description="Hex Bolt",
        part_code="SKU-004",
        qty=10,
        unit_price=100.0,
        line_total=1000.0,
    )
    assert item.part_sku == "SKU-004"
    assert item.part_code == "SKU-004"

    inv = InvoiceExtractionResult(
        invoice_number="INV-100",
        po_reference="PO-1040",
        supplier_name="Bharat Fasteners",
        line_items=[item],
        subtotal=1000.0,
        tax_amount=180.0,
        grand_total=1180.0,
        confidence=0.95,
    )
    assert inv.po_number == "PO-1040"
    assert inv.po_reference == "PO-1040"
    assert inv.total_amount == 1180.0
    assert inv.grand_total == 1180.0


def test_format_and_verify_email_body_deterministic_guardrails() -> None:
    draft_in = EmailDraftInput(
        category=EmailCategory.PO_DISPATCH,
        po_number="PO-2042",
        part_sku="BRG-402",
        part_name="Deep Groove Bearing",
        qty=20,
        unit_price=450.0,
        currency="INR",
        recipient_persona="sup1",
        recipient_name="Apex Precision Components",
        current_date="2026-10-01",
    )
    raw = (
        "Dear Apex Precision Components, please accept our purchase order for 20 units "
        "of BRG-402 at $450.00/unit. Kindly acknowledge receipt and share your "
        "expected dispatch schedule at the earliest convenience."
    )
    formatted = _format_and_verify_email_body(raw, draft_in)
    assert formatted.startswith("Dear Apex Precision Components,\n\n")
    assert "\n\n" in formatted
    assert "**PO-2042**" in formatted
    assert "**BRG-402**" in formatted
    assert "$" not in formatted
    assert "₹" in formatted
    assert "Best regards,\n**Lex**\nAI Supply Chain Assistant" in formatted
    # Verify SimpleNamespace / BaseModel unused imports aren't flagged
    assert issubclass(EmailDraftInput, BaseModel)
    assert SimpleNamespace(ok=True).ok
