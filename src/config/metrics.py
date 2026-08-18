from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DATE_GRAINS = {"day", "week", "month", "quarter"}


def _resolve_config_path(filename: str) -> Path:
    candidates = (
        Path.cwd() / "config" / filename,
        Path(__file__).parents[2] / "config" / filename,
        Path("/workspace/config") / filename,
    )
    return next(
        (candidate for candidate in candidates if candidate.is_file()), candidates[0]
    )


DEFAULT_METRICS_PATH = _resolve_config_path("metrics.yaml")


class MetricDefinition(BaseModel):
    """Validated executable definition for one metric."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    aggregation: str = Field(min_length=1)
    query: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    formula: str | None = None
    validity: dict[str, Any] = Field(default_factory=dict)
    zero_denominator: float | int | None = None
    source_table: str | None = None
    source_tables: list[str] = Field(default_factory=list)
    time_column: str | None = None
    entity_column: str | None = None
    group_by: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    numerator: str | None = None
    denominator: str | None = None

    @field_validator("id", "name", "description", "aggregation", "query", "unit")
    @classmethod
    def reject_blank_strings(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class MetricsConfig(BaseModel):
    """Top-level metric configuration and its global time semantics."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(gt=0)
    timezone: str = Field(min_length=1)
    date_grain: Literal["day", "week", "month", "quarter"]
    metrics: list[MetricDefinition] = Field(min_length=1)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"invalid IANA timezone: {value}") from exc
        return value

    @field_validator("date_grain")
    @classmethod
    def validate_date_grain(cls, value: str) -> str:
        if value not in DATE_GRAINS:
            raise ValueError(f"unsupported date grain: {value}")
        return value

    @model_validator(mode="after")
    def validate_metric_ids(self) -> MetricsConfig:
        ids = [metric.id for metric in self.metrics]
        if len(ids) != len(set(ids)):
            raise ValueError("metric ids must be unique")
        return self


def load_metrics_config(path: str | Path = DEFAULT_METRICS_PATH) -> MetricsConfig:
    """Load and validate the canonical metrics YAML file."""
    config_path = Path(path)
    with config_path.open(encoding="utf-8") as file:
        payload = yaml.safe_load(file)
    return MetricsConfig.model_validate(payload)
