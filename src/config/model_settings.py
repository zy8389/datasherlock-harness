"""Validated environment configuration for model clients."""

from __future__ import annotations

import os

from pydantic import BaseModel, Field, SecretStr, field_validator

try:  # The declared project dependency is used in normal installations.
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ModuleNotFoundError:  # pragma: no cover - only for minimal local runtimes
    SettingsConfigDict = dict  # type: ignore[misc,assignment]

    class BaseSettings(BaseModel):  # type: ignore[no-redef]
        """Small environment-reading fallback for environments without extras."""

        def __init__(self, **values: object) -> None:
            for field_name in self.model_fields:
                env_value = os.getenv(field_name.upper())
                if env_value is not None:
                    values.setdefault(field_name, env_value)
            super().__init__(**values)


class ModelSettings(BaseSettings):
    """Runtime settings shared by provider-specific model clients.

    The model name and API key intentionally have no application default. A
    real OpenAI client must receive them from the environment or an explicit
    test configuration.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        protected_namespaces=(),
    )

    model_provider: str = "openai"
    openai_api_key: SecretStr | None = None
    openai_model: str | None = None
    openai_base_url: str | None = None
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    llm_retry_base_delay_seconds: float = Field(default=0.5, ge=0)

    @field_validator("openai_api_key", "openai_model", "openai_base_url", mode="before")
    @classmethod
    def blank_values_are_unset(cls, value: object) -> object:
        """Allow intentionally blank optional values in ``.env`` files."""

        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("model_provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        """Normalize provider names before the factory selects an adapter."""

        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("model_provider must not be blank")
        return normalized
