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
    ) -> LLMResult[T]: ...


class GeminiLLMClient:
    """Thin wrapper over official google-genai SDK using gemini-3.5-flash-lite."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str | None = None,
    ) -> None:
        self._api_key = api_key or settings.gemini_api_key or os.environ.get("GEMINI_API_KEY")
        self.model_name = model_name or settings.gemini_model
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google import genai

            if not self._api_key:
                raise RuntimeError(
                    "GEMINI_API_KEY is not configured. Export GEMINI_API_KEY in your environment."
                )
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def generate_structured(
        self,
        *,
        prompt: str,
        response_schema: type[T],
        system_instruction: str | None = None,
        temperature: float = 0.0,
        pdf_bytes: bytes | None = None,
    ) -> LLMResult[T]:
        from google.genai import types

        client = self._get_client()
        contents: list[object] = []
        if pdf_bytes is not None:
            contents.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
        contents.append(prompt)

        config = types.GenerateContentConfig(
            temperature=temperature,
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=response_schema,
        )

        start = time.perf_counter()
        response = client.models.generate_content(
            model=self.model_name,
            contents=contents,
            config=config,
        )
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
