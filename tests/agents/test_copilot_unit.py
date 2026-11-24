import json

from sqlalchemy import select

from backend.agents.lex import (
    CopilotActionParam,
    CopilotActionType,
    CopilotProposedAction,
    CopilotQueryInput,
    CopilotQueryResult,
    LexAgent,
)
from backend.agents.llm_client import GeminiLLMClient
from backend.agents.tools import ToolRegistry
from backend.core import clock
from backend.core.config import settings
from backend.core.models import AgentTrace, Part
from backend.orchestrator.lex_tools import LexToolTracker, build_lex_tool_registry


def test_copilot_schema_has_no_additional_properties() -> None:
    """Gemini Developer API rejects dict[str, Any] / additionalProperties in response_schema."""
    schema_text = json.dumps(CopilotQueryResult.model_json_schema())
    assert "additionalProperties" not in schema_text

    sample = CopilotQueryResult(
        answer="VLV-204 is CRITICAL.",
        cited_skus=["VLV-204"],
        cited_po_numbers=[],
        cited_alert_ids=[1],
        proposed_actions=[
            CopilotProposedAction(
                action_type=CopilotActionType.CREATE_MANUAL_PO,
                title="Order 15u VLV-204 from Sup1",
                summary="Creates a PO for 15 units of VLV-204 from Apex Precision.",
                target_ref="VLV-204",
                params=[
                    CopilotActionParam(key="sku_or_part_id", value="VLV-204"),
                    CopilotActionParam(key="supplier_key_or_id", value="sup1"),
                    CopilotActionParam(key="qty", value="15"),
                ],
            )
        ],
        confidence=0.95,
    )
    assert sample.proposed_actions[0].action_type == CopilotActionType.CREATE_MANUAL_PO


def test_read_only_lex_tool_registry_excludes_mutating_tools(seeded_session) -> None:
    tracker = LexToolTracker()
    full_registry = build_lex_tool_registry(seeded_session, tracker=tracker)
    assert set(full_registry.names()) >= {
        "schedule_lex_reminder",
        "propose_memory_note",
        "create_advisory_alert",
    }

    ro_registry = build_lex_tool_registry(seeded_session, tracker=tracker, read_only_only=True)
    ro_names = set(ro_registry.names())
    assert "schedule_lex_reminder" not in ro_names
    assert "propose_memory_note" not in ro_names
    assert "create_advisory_alert" not in ro_names

    expected_ro = {
        "list_parts_summary",
        "list_alerts",
        "get_planning_overview",
        "get_part_snapshot",
        "get_consumption_history",
        "get_po_details",
        "list_open_pos",
        "get_supplier_profile",
        "get_mailroom_thread",
        "run_sandbox_calc",
        "run_statistical_analysis",
        "render_chart",
        "render_diagram",
    }
    assert expected_ro == ro_names

    # ToolRegistry.read_only_view() also filters out any write tools from a full registry
    filtered_view = full_registry.read_only_view()
    assert "schedule_lex_reminder" not in set(filtered_view.names())
    assert "propose_memory_note" not in set(filtered_view.names())
    assert "create_advisory_alert" not in set(filtered_view.names())
    assert all(defn.read_only for defn in filtered_view.list_definitions())


def test_statistical_analysis_and_visual_tools_execute_cleanly(seeded_session) -> None:
    registry = build_lex_tool_registry(seeded_session, read_only_only=True)

    demand_stats = registry.execute(
        "run_statistical_analysis",
        {"mode": "part_demand_stats", "part_sku_or_id": "BRG-402", "days": 90},
    )
    assert demand_stats.ok
    assert demand_stats.data["sku"] == "BRG-402"
    assert demand_stats.data["count"] == 90
    assert demand_stats.data["mean"] > 0
    assert demand_stats.data["p90"] >= demand_stats.data["median"]
    assert "trend_r_squared" in demand_stats.data

    pareto = registry.execute(
        "run_statistical_analysis",
        {"mode": "inventory_abc_pareto"},
    )
    assert pareto.ok
    assert pareto.data["total_skus"] == 15
    assert pareto.data["total_daily_spend_inr"] > 0
    assert sum(pareto.data["abc_counts"].values()) == 15

    bench = registry.execute(
        "run_statistical_analysis",
        {"mode": "supplier_benchmark", "part_sku_or_id": "VLV-204"},
    )
    assert bench.ok
    assert len(bench.data["suppliers"]) == 3
    assert bench.data["part_comparison"]["sku"] == "VLV-204"
    assert "backup_lt_savings_days" in bench.data["part_comparison"]

    chart_res = registry.execute(
        "render_chart",
        {
            "chart_type": "grouped_bar",
            "title": "Primary vs Backup Effective Lead Time",
            "labels": ["VLV-204", "BRG-402"],
            "unit": "days",
            "series": [
                {"name": "Primary", "values": [22.1, 10.0]},
                {"name": "Backup", "values": [12.0, 15.0]},
            ],
            "reference_lines": [{"label": "Target Max", "value": 15.0}],
        },
    )
    assert chart_res.ok
    assert chart_res.data["rendered"] is True

    diagram_res = registry.execute(
        "render_diagram",
        {
            "diagram_type": "decision",
            "title": "Replenishment Decision Flow",
            "nodes": [
                {"id": "n1", "label": "Check Runway", "tone": "info"},
                {"id": "n2", "label": "Switch to Backup", "tone": "warning"},
            ],
            "edges": [{"source": "n1", "target": "n2", "label": "runway < LT"}],
        },
    )
    assert diagram_res.ok
    assert diagram_res.data["rendered"] is True

    visuals = registry.pop_visual_artifacts()
    assert len(visuals) == 2
    assert visuals[0]["kind"] == "chart"
    assert visuals[1]["kind"] == "diagram"

    # Invalid chart lengths fail gracefully as ToolResult(ok=False)
    bad_chart = registry.execute(
        "render_chart",
        {
            "chart_type": "bar",
            "title": "Mismatch",
            "labels": ["A", "B"],
            "series": [{"name": "S1", "values": [1.0]}],
        },
    )
    assert not bad_chart.ok
    assert "expected 2" in (bad_chart.error or "")


def test_copilot_graceful_degradation_when_api_key_missing(seeded_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    parts_before = {
        p.sku: (p.on_hand_stock, p.rop, p.replenishment_point)
        for p in seeded_session.scalars(select(Part)).all()
    }

    lex = LexAgent(llm_client=GeminiLLMClient())
    registry = build_lex_tool_registry(seeded_session, read_only_only=True)
    # Include a write tool in a raw registry to ensure answer_copilot_query strips it too
    raw_registry = ToolRegistry()
    for d in registry.list_definitions():
        entry = registry.get(d.name)
        assert entry is not None
        raw_registry.register(d, entry.handler)
    raw_registry.register_function(
        name="unsafe_write_tool",
        description="Must be stripped by answer_copilot_query",
        parameters_schema={"type": "object", "properties": {}},
        handler=lambda: {"mutated": True},
        read_only=False,
    )

    outcome = lex.answer_copilot_query(
        seeded_session,
        CopilotQueryInput(
            question="Which SKUs are CRITICAL right now?",
            current_date=clock.now(seeded_session).isoformat(),
        ),
        tool_registry=raw_registry,
        linked_session_id=7,
    )
    seeded_session.commit()

    assert outcome.degraded is True
    assert outcome.error_code == "MISSING_API_KEY"
    assert outcome.result.confidence == 0.0
    assert outcome.result.proposed_actions == []
    assert "GEMINI_API_KEY" in outcome.result.answer
    assert outcome.trace_id is not None

    trace = seeded_session.get(AgentTrace, outcome.trace_id)
    assert trace is not None
    assert trace.task_type == "copilot_query"
    assert trace.linked_entity_type == "copilot_session"
    assert trace.linked_entity_id == 7
    assert trace.output_summary.startswith("DEGRADED (MISSING_API_KEY)")

    parts_after = {
        p.sku: (p.on_hand_stock, p.rop, p.replenishment_point)
        for p in seeded_session.scalars(select(Part)).all()
    }
    assert parts_after == parts_before
