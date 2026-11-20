import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import trace as otel_trace
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.config import settings
from backend.core.models import AgentTrace

# Approximate INR pricing for gemini-3.5-flash-lite (~₹84/USD * $0.075 in / $0.30 out per 1M tokens)
COST_PER_1M_INPUT_INR = 6.30
COST_PER_1M_OUTPUT_INR = 25.20

_phoenix_initialized = False


def estimate_cost_inr(input_tokens: int, output_tokens: int) -> float:
    in_cost = (max(0, input_tokens) / 1_000_000) * COST_PER_1M_INPUT_INR
    out_cost = (max(0, output_tokens) / 1_000_000) * COST_PER_1M_OUTPUT_INR
    return round(in_cost + out_cost, 6)


def get_phoenix_status() -> dict[str, Any]:
    """Report Phoenix OpenTelemetry configuration and availability without importing heavy deps."""
    import importlib.util

    try:
        available = (
            importlib.util.find_spec("phoenix") is not None
            and importlib.util.find_spec("phoenix.otel") is not None
        )
    except ModuleNotFoundError:
        available = False
    return {
        "enable_phoenix": bool(settings.enable_phoenix),
        "phoenix_installed": available,
        "phoenix_initialized": _phoenix_initialized,
        "mode": (
            "active"
            if (settings.enable_phoenix and available)
            else ("fallback_sqlite" if settings.enable_phoenix else "disabled")
        ),
    }


def _maybe_init_phoenix() -> None:
    global _phoenix_initialized
    if _phoenix_initialized or not settings.enable_phoenix:
        return
    try:
        from phoenix.otel import register  # type: ignore[import-not-found]

        register(project_name="inv-plan")
    except Exception:
        # Optional dependency or Phoenix server offline — degrade gracefully to SQLite traces.
        pass
    finally:
        _phoenix_initialized = True


@dataclass
class TraceSpanContext:
    agent_name: str
    task_type: str
    trigger_summary: str
    linked_entity_type: str | None = None
    linked_entity_id: int | None = None
    input_summary: str = ""
    output_summary: str = ""
    confidence: float | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw_prompt: str = ""
    raw_response: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_inr: float | None = None
    trace_id: int | None = None


class AgentTracer:
    """Agent-agnostic observability tracer writing SQLite rows + optional OpenTelemetry spans."""

    @staticmethod
    @contextmanager
    def span(
        session: Session,
        *,
        agent_name: str,
        task_type: str,
        trigger_summary: str,
        linked_entity_type: str | None = None,
        linked_entity_id: int | None = None,
    ) -> Generator[TraceSpanContext, None, None]:
        _maybe_init_phoenix()
        ctx = TraceSpanContext(
            agent_name=agent_name,
            task_type=task_type,
            trigger_summary=trigger_summary,
            linked_entity_type=linked_entity_type,
            linked_entity_id=linked_entity_id,
        )
        tracer = otel_trace.get_tracer("inv_plan")
        start = time.perf_counter()
        with tracer.start_as_current_span(f"{agent_name}.{task_type}") as otel_span:
            otel_span.set_attribute("agent.name", agent_name)
            otel_span.set_attribute("agent.task_type", task_type)
            try:
                yield ctx
            finally:
                elapsed_ms = int(round((time.perf_counter() - start) * 1000))
                computed_cost = (
                    ctx.cost_inr
                    if ctx.cost_inr is not None
                    else estimate_cost_inr(ctx.input_tokens, ctx.output_tokens)
                )
                otel_span.set_attribute("llm.input_tokens", ctx.input_tokens)
                otel_span.set_attribute("llm.output_tokens", ctx.output_tokens)
                otel_span.set_attribute("llm.cost_inr", computed_cost)
                if ctx.confidence is not None:
                    otel_span.set_attribute("agent.confidence", ctx.confidence)

                row = AgentTrace(
                    agent_name=ctx.agent_name,
                    task_type=ctx.task_type,
                    trigger_summary=ctx.trigger_summary[:300],
                    linked_entity_type=ctx.linked_entity_type,
                    linked_entity_id=ctx.linked_entity_id,
                    input_summary=ctx.input_summary,
                    output_summary=ctx.output_summary,
                    confidence=ctx.confidence,
                    tool_calls_json=ctx.tool_calls,
                    raw_prompt=ctx.raw_prompt,
                    raw_response=ctx.raw_response,
                    latency_ms=elapsed_ms,
                    input_tokens=ctx.input_tokens,
                    output_tokens=ctx.output_tokens,
                    cost_inr=computed_cost,
                    timestamp=clock.now(session),
                )
                session.add(row)
                session.flush()
                ctx.trace_id = row.id


def list_traces(
    session: Session,
    *,
    agent_name: str | None = None,
    task_type: str | None = None,
    limit: int = 100,
) -> list[AgentTrace]:
    stmt = select(AgentTrace)
    if agent_name:
        stmt = stmt.where(AgentTrace.agent_name == agent_name)
    if task_type:
        stmt = stmt.where(AgentTrace.task_type == task_type)
    stmt = stmt.order_by(AgentTrace.timestamp.desc(), AgentTrace.id.desc()).limit(limit)
    return list(session.scalars(stmt).all())
