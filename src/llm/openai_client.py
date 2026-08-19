"""Async OpenAI Responses API adapter with Pydantic Structured Outputs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

try:  # Imported lazily in minimal test environments without the extra.
    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AsyncOpenAI,
        RateLimitError,
    )
except ModuleNotFoundError:  # pragma: no cover - normal installs have openai
    class _OpenAISDKUnavailable(Exception):
        pass

    APIConnectionError = APIStatusError = APITimeoutError = RateLimitError = (
        _OpenAISDKUnavailable
    )
    AsyncOpenAI = None  # type: ignore[assignment,misc]

from config.model_settings import ModelSettings

from .base import (
    ModelAuthenticationError,
    ModelClientError,
    ModelConfigurationError,
    ModelProviderError,
    ModelRateLimitError,
    ModelRequestError,
    ModelResponseError,
    ModelTimeoutError,
    ModelTransportError,
)
from .models import ModelCallResult, ModelUsage

T = TypeVar("T", bound=BaseModel)
SleepFunc = Callable[[float], Awaitable[None]]


class OpenAIModelClient:
    """Call OpenAI asynchronously and return only provider-neutral results."""

    provider = "openai"

    def __init__(
        self,
        settings: ModelSettings | None = None,
        *,
        client: AsyncOpenAI | Any | None = None,
        sleep_func: SleepFunc | None = None,
    ) -> None:
        self.settings = settings or ModelSettings()
        self._sleep_func = sleep_func or asyncio.sleep
        if self.settings.model_provider != self.provider:
            raise ModelConfigurationError(
                f"OpenAIModelClient cannot handle provider {self.settings.model_provider!r}"
            )
        if not self.settings.openai_model:
            raise ModelConfigurationError(
                "OPENAI_MODEL is required before creating an OpenAIModelClient"
            )

        self.model = self.settings.openai_model
        if client is not None:
            self._client = client
            return

        if AsyncOpenAI is None:
            raise ModelConfigurationError(
                "openai package is required to create an OpenAIModelClient"
            )

        if self.settings.openai_api_key is None:
            raise ModelConfigurationError(
                "OPENAI_API_KEY is required before creating an OpenAIModelClient"
            )

        # Transport retries are implemented here so the count in
        # ModelCallResult is accurate. Setting the SDK retry budget to zero
        # prevents hidden SDK retries from being mixed with Planner repair
        # retries.
        client_kwargs: dict[str, Any] = {
            "api_key": self.settings.openai_api_key.get_secret_value(),
            "timeout": self.settings.llm_timeout_seconds,
            "max_retries": 0,
        }
        if self.settings.openai_base_url:
            client_kwargs["base_url"] = self.settings.openai_base_url
        self._client = AsyncOpenAI(**client_kwargs)

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
    ) -> ModelCallResult[T]:
        """Run Responses Structured Outputs and validate the parsed model again."""

        started = perf_counter()
        transport_retries = 0
        for attempt in range(self.settings.llm_max_retries + 1):
            try:
                response = await self._client.responses.parse(
                    model=self.model,
                    instructions=system_prompt,
                    input=[{"role": "user", "content": user_prompt}],
                    text_format=response_model,
                )
            except ValidationError as exc:
                raise ModelResponseError(
                    f"OpenAI Structured Output parsing failed: {exc}",
                    transport_retry_count=transport_retries,
                    provider=self.provider,
                    model=self.model,
                    latency_ms=(perf_counter() - started) * 1000,
                ) from exc
            except Exception as exc:
                normalized = self._normalize_error(
                    exc,
                    transport_retry_count=transport_retries,
                    provider=self.provider,
                    model=self.model,
                    latency_ms=(perf_counter() - started) * 1000,
                )
                if self._is_retryable(exc) and attempt < self.settings.llm_max_retries:
                    delay = self.settings.llm_retry_base_delay_seconds * (2**transport_retries)
                    transport_retries += 1
                    await self._sleep_func(delay)
                    continue
                raise normalized from exc

            try:
                parsed_value = getattr(response, "output_parsed", None)
                if parsed_value is None:
                    raise ModelResponseError(
                        "OpenAI response did not contain a parsed Structured Output",
                        transport_retry_count=transport_retries,
                        provider=self.provider,
                        model=self.model,
                        latency_ms=(perf_counter() - started) * 1000,
                    )
                parsed = response_model.model_validate(parsed_value)
            except (ValidationError, ModelClientError) as exc:
                if isinstance(exc, ModelClientError):
                    raise
                raise ModelResponseError(
                    f"OpenAI Structured Output failed Pydantic validation: {exc}",
                    transport_retry_count=transport_retries,
                    provider=self.provider,
                    model=self.model,
                    latency_ms=(perf_counter() - started) * 1000,
                ) from exc
            except Exception as exc:
                raise ModelResponseError(
                    f"OpenAI response could not be parsed: {exc}",
                    transport_retry_count=transport_retries,
                    provider=self.provider,
                    model=self.model,
                    latency_ms=(perf_counter() - started) * 1000,
                ) from exc

            usage = getattr(response, "usage", None)
            request_id = getattr(response, "_request_id", None) or getattr(
                response, "id", None
            )
            raw_text = getattr(response, "output_text", None)
            return ModelCallResult(
                provider=self.provider,
                model=self.model,
                parsed=parsed,
                raw_text=raw_text if isinstance(raw_text, str) else None,
                request_id=request_id if isinstance(request_id, str) else None,
                usage=ModelUsage(
                    input_tokens=getattr(usage, "input_tokens", None),
                    output_tokens=getattr(usage, "output_tokens", None),
                    total_tokens=getattr(usage, "total_tokens", None),
                ),
                latency_ms=(perf_counter() - started) * 1000,
                retry_count=transport_retries,
                transport_retry_count=transport_retries,
            )

        raise ModelTransportError(
            "OpenAI request ended without a response",
            transport_retry_count=transport_retries,
            provider=self.provider,
            model=self.model,
            latency_ms=(perf_counter() - started) * 1000,
        )

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        """Classify only transport, rate-limit and transient server failures."""

        if isinstance(error, (APITimeoutError, APIConnectionError, RateLimitError)):
            return True
        if isinstance(error, APIStatusError):
            status_code = getattr(error, "status_code", None)
            return status_code in {408, 429} or (
                isinstance(status_code, int) and status_code >= 500
            )
        return isinstance(error, (TimeoutError, asyncio.TimeoutError, ConnectionError))

    @staticmethod
    def _normalize_error(
        error: Exception,
        *,
        transport_retry_count: int = 0,
        provider: str | None = None,
        model: str | None = None,
        latency_ms: float | None = None,
    ) -> ModelClientError:
        """Convert OpenAI SDK-specific exceptions to stable application errors."""

        if isinstance(error, ModelClientError):
            normalized = error
        elif isinstance(error, (APITimeoutError, TimeoutError, asyncio.TimeoutError)):
            normalized = ModelTimeoutError("OpenAI request timed out")
        elif isinstance(error, RateLimitError):
            normalized = ModelRateLimitError("OpenAI request was rate limited")
        elif isinstance(error, APIStatusError):
            status_code = getattr(error, "status_code", None)
            if status_code in {401, 403}:
                normalized = ModelAuthenticationError(
                    f"OpenAI authentication failed (status={status_code})"
                )
            elif status_code in {400, 404, 422}:
                normalized = ModelRequestError(
                    f"OpenAI request was rejected (status={status_code})"
                )
            elif status_code == 408:
                normalized = ModelTimeoutError("OpenAI request timed out")
            elif status_code == 429:
                normalized = ModelRateLimitError("OpenAI request was rate limited")
            elif isinstance(status_code, int) and status_code >= 500:
                normalized = ModelProviderError(
                    f"OpenAI provider failed (status={status_code})"
                )
            else:
                normalized = ModelRequestError(
                    f"OpenAI API returned an unsupported status (status={status_code})"
                )
        elif isinstance(error, (APIConnectionError, ConnectionError)):
            normalized = ModelTransportError("OpenAI connection failed")
        else:
            normalized = ModelTransportError(f"OpenAI provider call failed: {error}")

        normalized.transport_retry_count = max(
            normalized.transport_retry_count, transport_retry_count
        )
        normalized.provider = provider or normalized.provider
        normalized.model = model or normalized.model
        normalized.latency_ms = latency_ms
        return normalized


__all__ = ["OpenAIModelClient"]
