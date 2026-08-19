"""Deterministic in-memory ModelClient for unit tests."""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import Any, TypeVar

from pydantic import BaseModel

from .models import ModelCallResult, ModelUsage

T = TypeVar("T", bound=BaseModel)
MockResponseFactory = Callable[[type[T], str, str], T]


class MockModelClient:
    """Return a caller-supplied Pydantic response without network access."""

    provider = "mock"

    def __init__(
        self,
        response: BaseModel | MockResponseFactory[Any],
        *,
        model: str = "mock-model",
        usage: ModelUsage | None = None,
    ) -> None:
        self.response = response
        self.model = model
        self.usage = usage or ModelUsage()
        self.calls: list[dict[str, str]] = []

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
    ) -> ModelCallResult[T]:
        """Record the call and return a validated copy of the configured response."""

        started = perf_counter()
        self.calls.append(
            {"system_prompt": system_prompt, "user_prompt": user_prompt}
        )
        if callable(self.response):
            parsed = self.response(response_model, system_prompt, user_prompt)
        else:
            parsed = response_model.model_validate(self.response.model_dump(mode="json"))
        validated = response_model.model_validate(parsed)
        return ModelCallResult(
            provider=self.provider,
            model=self.model,
            parsed=validated,
            raw_text=validated.model_dump_json(),
            request_id=f"mock-{len(self.calls)}",
            usage=self.usage,
            latency_ms=(perf_counter() - started) * 1000,
            retry_count=0,
            transport_retry_count=0,
        )


__all__ = ["MockModelClient"]
