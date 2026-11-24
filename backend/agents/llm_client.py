import os
import time
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

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
    return False


class GeminiLLMClient:
    """Thin wrapper over official google-genai SDK using gemini-3.5-flash-lite."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str | None = None,
        timeout_seconds: float = 30.0,
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
        contents: list[object] = []
        if pdf_bytes is not None:
            contents.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
        for mime_type, img_bytes in image_attachments or []:
            if img_bytes:
                contents.append(
                    types.Part.from_bytes(data=img_bytes, mime_type=mime_type or "image/jpeg")
                )
        contents.append(prompt)

        config = types.GenerateContentConfig(
            temperature=temperature,
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=response_schema,
        )

        start = time.perf_counter()
        response = None
        max_attempts = max(0, self.max_retries) + 1
        for attempt in range(max_attempts):
            try:
                response = client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=config,
                )
                break
            except Exception as exc:
                if attempt >= max_attempts - 1 or not _is_transient_error(exc):
                    raise
                delay = self.backoff_base_seconds * (2**attempt)
                if delay > 0:
                    time.sleep(delay)
        elapsed_ms = int(round((time.perf_counter() - start) * 1000))

        raw_text = response.text or ""
        parsed_raw = getattr(response, "parsed", None)
        if isinstance(parsed_raw, response_schema):
            parsed = parsed_raw
        elif isinstance(parsed_raw, dict):
            parsed = response_schema.model_validate(parsed_raw)
        else:
            parsed = response_schema.model_validate_json(raw_text)

        usage = getattr(response, "usage_metadata", None)
        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)

        return LLMResult(
            parsed=parsed,
            raw_text=raw_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=elapsed_ms,
            cost_inr=estimate_cost_inr(input_tokens, output_tokens),
            model_name=self.model_name,
        )
