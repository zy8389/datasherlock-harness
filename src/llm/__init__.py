"""Provider-neutral model client layer."""

from .base import (
    ModelAuthenticationError,
    ModelClient,
    ModelClientError,
    ModelConfigurationError,
    ModelProviderError,
    ModelRateLimitError,
    ModelRequestError,
    ModelResponseError,
    ModelTimeoutError,
    ModelTransportError,
)
from .mock_client import MockModelClient
from .models import ModelCallResult, ModelUsage


def create_model_client(*args: object, **kwargs: object):
    """Lazily create a configured provider client."""

    from .factory import create_model_client as _create_model_client

    return _create_model_client(*args, **kwargs)


def __getattr__(name: str):
    """Keep provider SDK imports lazy for offline Planner and Mock usage."""

    if name == "OpenAIModelClient":
        from .openai_client import OpenAIModelClient

        return OpenAIModelClient
    raise AttributeError(name)

__all__ = [
    "MockModelClient",
    "ModelAuthenticationError",
    "ModelCallResult",
    "ModelClient",
    "ModelClientError",
    "ModelConfigurationError",
    "ModelProviderError",
    "ModelRateLimitError",
    "ModelRequestError",
    "ModelResponseError",
    "ModelTimeoutError",
    "ModelTransportError",
    "ModelUsage",
    "OpenAIModelClient",
    "create_model_client",
]
