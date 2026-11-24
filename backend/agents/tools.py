import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel, Field


def _to_json_safe(value: Any) -> Any:
    """Recursively convert values into JSON-serializable primitives."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(v) for v in value]
    return str(value)


def _summarize_payload(data: Any, *, max_chars: int = 240) -> str:
    """Create a concise single-line summary of a tool result for trace logs."""
    if data is None:
        return "null"
    try:
        serialized = json.dumps(_to_json_safe(data), ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        serialized = str(data)
    if len(serialized) <= max_chars:
        return serialized
    return f"{serialized[: max_chars - 3]}..."


class ToolDefinition(BaseModel):
    """Domain-agnostic tool metadata and JSON Schema for LLM function calling."""

    name: str
    description: str
    parameters_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    read_only: bool = True


ToolSpec = ToolDefinition


class ToolResult(BaseModel):
    """Structured outcome returned by ToolRegistry.execute()."""

    tool_name: str
    ok: bool = True
    data: Any = None
    error: str | None = None
    duration_ms: int = 0
    summary: str = ""


class ToolHandler(Protocol):
    def __call__(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolDefinition
    handler: ToolHandler


class ToolRegistry:
    """Session-scoped registry binding ToolDefinitions to concrete Python handlers."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, spec: ToolDefinition, handler: ToolHandler) -> None:
        clean_name = spec.name.strip()
        if not clean_name:
            raise ValueError("Tool name must not be empty")
        if clean_name in self._tools:
            raise ValueError(f"Tool '{clean_name}' is already registered")
        normalized_spec = spec.model_copy(update={"name": clean_name})
        self._tools[clean_name] = RegisteredTool(spec=normalized_spec, handler=handler)

    def register_function(
        self,
        *,
        name: str,
        description: str,
        parameters_schema: dict[str, Any],
        handler: Callable[..., Any],
        read_only: bool = True,
    ) -> None:
        self.register(
            ToolDefinition(
                name=name,
                description=description,
                parameters_schema=parameters_schema,
                read_only=read_only,
            ),
            handler,
        )

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def list_definitions(self) -> list[ToolDefinition]:
        return [entry.spec for entry in self._tools.values()]

    def execute(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        start = time.perf_counter()
        entry = self._tools.get(name)
        if entry is None:
            elapsed_ms = int(round((time.perf_counter() - start) * 1000))
            err = f"Unknown tool '{name}'"
            return ToolResult(
                tool_name=name,
                ok=False,
                data=None,
                error=err,
                duration_ms=elapsed_ms,
                summary=f"ERROR: {err}",
            )

        call_args = dict(args or {})
        try:
            raw_output = entry.handler(**call_args)
            elapsed_ms = int(round((time.perf_counter() - start) * 1000))
            if isinstance(raw_output, ToolResult):
                return raw_output.model_copy(
                    update={
                        "tool_name": name,
                        "duration_ms": raw_output.duration_ms or elapsed_ms,
                    }
                )
            safe_data = _to_json_safe(raw_output)
            return ToolResult(
                tool_name=name,
                ok=True,
                data=safe_data,
                error=None,
                duration_ms=elapsed_ms,
                summary=_summarize_payload(safe_data),
            )
        except Exception as exc:
            elapsed_ms = int(round((time.perf_counter() - start) * 1000))
            err = f"{type(exc).__name__}: {exc}"
            return ToolResult(
                tool_name=name,
                ok=False,
                data=None,
                error=err,
                duration_ms=elapsed_ms,
                summary=f"ERROR: {err}",
            )

    @staticmethod
    def format_trace_record(
        result: ToolResult,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "tool": result.tool_name,
            "args": _to_json_safe(args or {}),
            "result_summary": result.summary,
            "duration_ms": result.duration_ms,
            "error": result.error,
        }
