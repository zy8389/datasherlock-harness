"""Provider-independent model client contracts and normalized errors."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from pydantic import BaseModel

from .models import ModelCallResult

T = TypeVar("T", bound=BaseModel)


class ModelClientError(RuntimeError):
    """Base error exposed to Planner code regardless of provider SDK."""


class ModelConfigurationError(ModelClientError):
    """Raised when provider configuration is missing or unsupported."""


class ModelTimeoutError(ModelClientError):
    """Raised after a provider request exhausts its transport retries."""


class ModelRateLimitError(ModelClientError):
    """Raised after a provider rate-limit response exhausts its retries."""


class ModelTransportError(ModelClientError):
    """Raised for connection or retryable provider transport failures."""


class ModelResponseError(ModelClientError):
    """Raised when a provider response cannot produce the requested schema."""


class ModelClient(Protocol):
    """Minimal async interface consumed by Planner."""

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
    ) -> ModelCallResult[T]:
        """Generate and validate one response against ``response_model``."""


def is_model_client(value: object) -> bool:
    """Return whether an object exposes the required async client method."""

    return callable(getattr(value, "generate_structured", None))


CallableGenerator = Callable[[str], str]
