import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Protocol, TypeVar

from pydantic import BaseModel

from backend.agents.tools import ToolRegistry
from backend.core.config import settings
from backend.observability.tracer import estimate_cost_inr

T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMResult(Generic[T]):
    parsed: T
    raw_text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_inr: float
    model_name: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class LLMClientProtocol(Protocol):
    def generate_structured(
        self,
        *,
        prompt: str,
        response_schema: type[T],
        system_instruction: str | None = None,
        temperature: float = 0.0,
        pdf_bytes: bytes | None = None,
        image_attachments: list[tuple[str, bytes]] | None = None,
    ) -> LLMResult[T]: ...

    def generate_with_tools(
        self,
        *,
        prompt: str,
        response_schema: type[T],
        tool_registry: ToolRegistry,
        system_instruction: str | None = None,
        temperature: float = 0.0,
        pdf_bytes: bytes | None = None,
        image_attachments: list[tuple[str, bytes]] | None = None,
        max_steps: int = 8,
    ) -> LLMResult[T]: ...


_TRANSIENT_HTTP_CODES = {408, 429, 500, 502, 503, 504}


def _is_transient_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int) and code in _TRANSIENT_HTTP_CODES:
        return True
    cls_name = type(exc).__name__.lower()
    if "timeout" in cls_name or "connection" in cls_name or "transport" in cls_name:
        return True
    msg_lower = str(exc).lower()
    if any(
        tok in msg_lower
        for tok in ("429", "503", "504", "resource_exhausted", "unavailable", "timed out")
    ):
        return True
    return False


def _extract_token_counts(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage_metadata", None)
    input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
    return input_tokens, output_tokens


def _parse_schema_response(response: Any, response_schema: type[T]) -> tuple[T, str]:
    raw_text = getattr(response, "text", None) or ""
    parsed_raw = getattr(response, "parsed", None)
    if isinstance(parsed_raw, response_schema):
        parsed = parsed_raw
    elif isinstance(parsed_raw, dict):
        parsed = response_schema.model_validate(parsed_raw)
    else:
        parsed = response_schema.model_validate_json(raw_text)
    if not raw_text:
        raw_text = parsed.model_dump_json()
    return parsed, raw_text


class GeminiLLMClient:
    """Thin wrapper over official google-genai SDK using gemini-3.5-flash-lite."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str | None = None,
        timeout_seconds: float = 45.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.5,
    ) -> None:
        self._api_key = api_key or settings.gemini_api_key or os.environ.get("GEMINI_API_KEY")
        self.model_name = model_name or settings.gemini_model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google import genai
            from google.genai import types

            if not self._api_key:
                raise RuntimeError(
                    "GEMINI_API_KEY is not configured. Export GEMINI_API_KEY in your environment."
                )
            self._client = genai.Client(
                api_key=self._api_key,
                http_options=types.HttpOptions(
                    timeout=max(1000, int(self.timeout_seconds * 1000))
                ),
            )
        return self._client

    def _call_with_retry(self, fn: Callable[[], Any]) -> Any:
        max_attempts = max(0, self.max_retries) + 1
        for attempt in range(max_attempts):
            try:
                return fn()
            except Exception as exc:
                if attempt >= max_attempts - 1 or not _is_transient_error(exc):
                    raise
                delay = self.backoff_base_seconds * (2**attempt)
                if delay > 0:
                    time.sleep(delay)
        raise RuntimeError("Unreachable retry loop exit")

    @staticmethod
    def _build_initial_parts(
        *,
        prompt: str,
        pdf_bytes: bytes | None = None,
        image_attachments: list[tuple[str, bytes]] | None = None,
    ) -> list[Any]:
        from google.genai import types

        parts: list[Any] = []
        if pdf_bytes is not None:
            parts.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
        for mime_type, img_bytes in image_attachments or []:
            if img_bytes:
                parts.append(
                    types.Part.from_bytes(data=img_bytes, mime_type=mime_type or "image/jpeg")
                )
        parts.append(prompt)
        return parts

    def generate_structured(
        self,
        *,
        prompt: str,
        response_schema: type[T],
        system_instruction: str | None = None,
        temperature: float = 0.0,
        pdf_bytes: bytes | None = None,
        image_attachments: list[tuple[str, bytes]] | None = None,
    ) -> LLMResult[T]:
        from google.genai import types

        client = self._get_client()
        contents = self._build_initial_parts(
            prompt=prompt,
            pdf_bytes=pdf_bytes,
            image_attachments=image_attachments,
        )
        config = types.GenerateContentConfig(
            temperature=temperature,
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=response_schema,
        )

        start = time.perf_counter()
        response = self._call_with_retry(
            lambda: client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=config,
            )
        )
        elapsed_ms = int(round((time.perf_counter() - start) * 1000))

        parsed, raw_text = _parse_schema_response(response, response_schema)
        input_tokens, output_tokens = _extract_token_counts(response)

        return LLMResult(
            parsed=parsed,
            raw_text=raw_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=elapsed_ms,
            cost_inr=estimate_cost_inr(input_tokens, output_tokens),
            model_name=self.model_name,
        )

    def generate_with_tools(
        self,
        *,
        prompt: str,
        response_schema: type[T],
        tool_registry: ToolRegistry,
        system_instruction: str | None = None,
        temperature: float = 0.0,
        pdf_bytes: bytes | None = None,
        image_attachments: list[tuple[str, bytes]] | None = None,
        max_steps: int = 8,
    ) -> LLMResult[T]:
        from google.genai import types

        defs = tool_registry.list_definitions()
        if not defs:
            return self.generate_structured(
                prompt=prompt,
                response_schema=response_schema,
                system_instruction=system_instruction,
                temperature=temperature,
                pdf_bytes=pdf_bytes,
                image_attachments=image_attachments,
            )

        client = self._get_client()
        declarations = [
            types.FunctionDeclaration(
                name=d.name,
                description=d.description,
                parameters_json_schema=d.parameters_schema,
            )
            for d in defs
        ]
        gemini_tool = types.Tool(function_declarations=declarations)
        tool_config = types.GenerateContentConfig(
            temperature=temperature,
            system_instruction=system_instruction,
            tools=[gemini_tool],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        raw_parts = self._build_initial_parts(
            prompt=prompt,
            pdf_bytes=pdf_bytes,
            image_attachments=image_attachments,
        )
        typed_parts = [
            types.Part.from_text(text=p) if isinstance(p, str) else p for p in raw_parts
        ]
        conversation: list[Any] = [types.UserContent(parts=typed_parts)]

        start = time.perf_counter()
        total_in = 0
        total_out = 0
        tool_call_logs: list[dict[str, Any]] = []
        last_response: Any = None

        for _ in range(max(1, max_steps)):
            response = self._call_with_retry(
                lambda: client.models.generate_content(
                    model=self.model_name,
                    contents=conversation,
                    config=tool_config,
                )
            )
            last_response = response
            in_tok, out_tok = _extract_token_counts(response)
            total_in += in_tok
            total_out += out_tok

            fcs = list(getattr(response, "function_calls", None) or [])
            if not fcs:
                try:
                    parsed, raw_text = _parse_schema_response(response, response_schema)
                    elapsed_ms = int(round((time.perf_counter() - start) * 1000))
                    return LLMResult(
                        parsed=parsed,
                        raw_text=raw_text,
                        input_tokens=total_in,
                        output_tokens=total_out,
                        latency_ms=elapsed_ms,
                        cost_inr=estimate_cost_inr(total_in, total_out),
                        model_name=self.model_name,
                        tool_calls=tool_call_logs,
                    )
                except Exception:
                    # Model returned prose after tool calls; synthesize strict schema below.
                    candidates = getattr(response, "candidates", None) or []
                    if candidates and getattr(candidates[0], "content", None) is not None:
                        conversation.append(candidates[0].content)
                    elif getattr(response, "text", None):
                        conversation.append(
                            types.ModelContent(
                                parts=[types.Part.from_text(text=str(response.text))]
                            )
                        )
                    break

            candidates = getattr(response, "candidates", None) or []
            if candidates and getattr(candidates[0], "content", None) is not None:
                conversation.append(candidates[0].content)
            else:
                conversation.append(
                    types.ModelContent(
                        parts=[
                            types.Part.from_function_call(
                                name=str(fc.name),
                                args=dict(fc.args or {}),
                            )
                            for fc in fcs
                        ]
                    )
                )

            response_parts: list[Any] = []
            for fc in fcs:
                fn_name = str(fc.name)
                fn_args = dict(fc.args or {})
                tool_res = tool_registry.execute(fn_name, fn_args)
                tool_call_logs.append(ToolRegistry.format_trace_record(tool_res, fn_args))
                response_parts.append(
                    types.Part.from_function_response(
                        name=fn_name,
                        response={
                            "ok": tool_res.ok,
                            "data": tool_res.data,
                            "error": tool_res.error,
                        },
                    )
                )
            conversation.append(types.UserContent(parts=response_parts))
        else:
            # Hit max_steps while still requesting tools; append model turn if not already appended
            _ = last_response

        # Final schema-enforced synthesis step (no tools)
        conversation.append(
            types.UserContent(
                parts=[
                    types.Part.from_text(
                        text=(
                            "Based on the original request and all tool outputs above, "
                            "output ONLY the final JSON object conforming strictly to the "
                            "required response schema."
                        )
                    )
                ]
            )
        )
        final_config = types.GenerateContentConfig(
            temperature=temperature,
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=response_schema,
        )
        final_response = self._call_with_retry(
            lambda: client.models.generate_content(
                model=self.model_name,
                contents=conversation,
                config=final_config,
            )
        )
        in_tok, out_tok = _extract_token_counts(final_response)
        total_in += in_tok
        total_out += out_tok

        parsed, raw_text = _parse_schema_response(final_response, response_schema)
        elapsed_ms = int(round((time.perf_counter() - start) * 1000))
        return LLMResult(
            parsed=parsed,
            raw_text=raw_text,
            input_tokens=total_in,
            output_tokens=total_out,
            latency_ms=elapsed_ms,
            cost_inr=estimate_cost_inr(total_in, total_out),
            model_name=self.model_name,
            tool_calls=tool_call_logs,
        )
