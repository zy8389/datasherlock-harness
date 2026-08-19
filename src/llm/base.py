"""Provider-independent model client contracts and normalized errors."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from pydantic import BaseModel

from .models import ModelCallResult

T = TypeVar("T", bound=BaseModel)


class ModelClientError(RuntimeError):
    """Base error exposed to Planner code regardless of provider SDK.

    Provider adapters attach lightweight call metadata to terminal errors so a
    Planner fallback remains auditable even when no ``ModelCallResult`` exists.
    """

    def __init__(
        self,
        message: str,
        *,
        transport_retry_count: int = 0,
        provider: str | None = None,
        model: str | None = None,
        latency_ms: float | None = None,
    ) -> None:
        super().__init__(message)
        self.transport_retry_count = transport_retry_count
        self.provider = provider
        self.model = model
        self.latency_ms = latency_ms


class ModelConfigurationError(ModelClientError):
    """Raised when provider configuration is missing or unsupported."""


class ModelTimeoutError(ModelClientError):
    """Raised after a provider request exhausts its transport retries."""


class ModelRateLimitError(ModelClientError):
    """Raised after a provider rate-limit response exhausts its retries."""


class ModelTransportError(ModelClientError):
    """Raised for connection or retryable provider transport failures."""


class ModelAuthenticationError(ModelClientError):
    """Raised when the provider rejects credentials or authorization."""


class ModelRequestError(ModelClientError):
    """Raised when the provider rejects a non-retryable request."""


class ModelProviderError(ModelClientError):
    """Raised for a terminal provider-side 5xx failure."""


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
