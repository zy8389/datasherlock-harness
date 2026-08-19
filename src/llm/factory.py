"""Provider selection kept outside Planner."""

from __future__ import annotations

from config.model_settings import ModelSettings

from .base import ModelClient, ModelConfigurationError
from .openai_client import OpenAIModelClient


def create_model_client(settings: ModelSettings | None = None) -> ModelClient:
    """Create the configured provider adapter without branching in Planner."""

    active_settings = settings or ModelSettings()
    if active_settings.model_provider == "openai":
        return OpenAIModelClient(settings=active_settings)
    raise ModelConfigurationError(
        f"unsupported model provider: {active_settings.model_provider!r}"
    )


__all__ = ["create_model_client"]
