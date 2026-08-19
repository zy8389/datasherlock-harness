"""Provider-neutral models returned by the model client layer."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T", bound=BaseModel)


class ModelUsage(BaseModel):
    """Token accounting copied from a provider response when available."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class ModelCallResult(BaseModel, Generic[T]):
    """Stable result envelope shared by every model provider."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    parsed: T
    raw_text: str | None = None
    request_id: str | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    latency_ms: float = Field(ge=0)

    # retry_count is the total observable retry count for this call. The two
    # components make transport retry and Planner repair retry auditable rather
    # than silently combining them in one counter.
    retry_count: int = Field(default=0, ge=0)
    transport_retry_count: int = Field(default=0, ge=0)
    planner_repair_count: int = Field(default=0, ge=0)
