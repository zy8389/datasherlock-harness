"""Typed contracts for generated benchmark case variants and manifests.

Canonical meaning remains in ``benchmark/ground_truth`` and the fault catalog.
This module only describes concrete generation parameters and materialized
observations that are safe to consume as benchmark input.
"""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.planner import Alert
from config.faults import EvidencePath, GroundTruthCase, InjectionSpec

CASE_ID_PATTERN = r"^F(?:0[1-9]|1[0-2])-(?:00[1-5])$"
SEED_CASE_ID_PATTERN = r"^F(?:0[1-9]|1[0-2])-001$"
CASE_ID_RE = re.compile(CASE_ID_PATTERN)
SEED_CASE_ID_RE = re.compile(SEED_CASE_ID_PATTERN)


class BaselineConfig(BaseModel):
    """Small, reproducible source dataset configuration for all cases."""

    model_config = ConfigDict(extra="forbid")

    seed: int
    start_date: date
    days: int = Field(gt=0)
    user_count: int = Field(gt=0)
    event_count: int = Field(gt=0)


class InjectionOverrides(BaseModel):
    """Allowed typed overrides for a canonical ``InjectionSpec``.

    The strategy is inherited from the Ground Truth seed and cannot be
    overridden.  ``None`` means that a field is absent from the variant file;
    the generator merges only explicitly provided values.
    """

    model_config = ConfigDict(extra="forbid")

    ratio: float | None = Field(default=None, ge=0, le=1)
    device_type: str | None = None
    region: str | None = None
    shift_hours: int | None = None
    shift_days: int | None = None
    multiplier: float | None = Field(default=None, gt=0)
    from_value: str | None = None
    to_value: str | None = None
    control_ratio: float | None = Field(default=None, ge=0, le=1)
    treatment_ratio: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_split(self) -> InjectionOverrides:
        if (
            self.control_ratio is not None
            and self.treatment_ratio is not None
            and not math.isclose(
                self.control_ratio + self.treatment_ratio, 1.0, abs_tol=1e-9
            )
        ):
            raise ValueError("variant split overrides must sum to 1")
        return self


class CaseVariant(BaseModel):
    """One deterministic parameter set derived from a canonical seed case."""

    model_config = ConfigDict(extra="forbid")

    source_seed_case_id: str = Field(pattern=SEED_CASE_ID_PATTERN)
    variant_index: int = Field(ge=1, le=5)
    seed: int
    metric_date: date
    injection_overrides: InjectionOverrides = Field(default_factory=InjectionOverrides)

    @property
    def case_id(self) -> str:
        return f"{self.source_seed_case_id[:3]}-{self.variant_index:03d}"


class VariantConfig(BaseModel):
    """Versioned variant file containing shared baseline and 60 parameters."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(gt=0)
    baseline: BaselineConfig
    variants: list[CaseVariant] = Field(min_length=60, max_length=60)

    @model_validator(mode="after")
    def validate_variant_set(self) -> VariantConfig:
        expected_ids = {
            f"F{fault_number:02d}-{variant_index:03d}"
            for fault_number in range(1, 13)
            for variant_index in range(1, 6)
        }
        actual_ids = {variant.case_id for variant in self.variants}
        if actual_ids != expected_ids:
            raise ValueError("variants must contain exactly F01-001 through F12-005")
        if len(actual_ids) != len(self.variants):
            raise ValueError("variant case ids must be unique")

        start = self.baseline.start_date
        end = start.fromordinal(start.toordinal() + self.baseline.days - 1)
        for variant in self.variants:
            if not start <= variant.metric_date <= end:
                raise ValueError(
                    f"{variant.case_id} metric_date must be inside the baseline window"
                )
        return self


class CaseManifest(BaseModel):
    """Concrete, generated case contract plus observed materialization facts."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(gt=0)
    case_id: str = Field(pattern=CASE_ID_PATTERN)
    fault_id: str = Field(pattern=r"^F(?:0[1-9]|1[0-2])$")
    source_seed_case_id: str = Field(pattern=SEED_CASE_ID_PATTERN)
    variant_index: int = Field(ge=1, le=5)
    seed: int
    baseline_seed: int
    baseline_start_date: date
    baseline_days: int = Field(gt=0)
    baseline_user_count: int = Field(gt=0)
    baseline_event_count: int = Field(gt=0)
    metric_date: date

    root_cause_type: str = Field(min_length=1)
    affected_metric: str = Field(min_length=1)
    affected_assets: list[str] = Field(min_length=1)
    injection: InjectionSpec

    original_alert: Alert
    expected_direction: Literal["increase", "decrease"]
    effect_size_type: Literal["relative", "absolute"]
    minimum_effect_size: float = Field(gt=0)
    actual_effect: float
    affected_row_count: int = Field(gt=0)
    severity: str = Field(min_length=1)
    expected_evidence: list[str] = Field(min_length=2)
    evidence_paths: list[EvidencePath] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_identity_and_alert(self) -> CaseManifest:
        expected_case_id = f"{self.source_seed_case_id[:3]}-{self.variant_index:03d}"
        if self.case_id != expected_case_id:
            raise ValueError("case_id does not match source seed and variant_index")
        if self.fault_id != self.source_seed_case_id[:3]:
            raise ValueError("fault_id does not match source seed case")
        if self.injection.metric_date != self.metric_date:
            raise ValueError("injection.metric_date must match manifest.metric_date")
        if self.original_alert.metric != self.affected_metric:
            raise ValueError("original_alert.metric must match affected_metric")
        if self.original_alert.observed_at != self.metric_date.isoformat():
            raise ValueError("original_alert.observed_at must match metric_date")
        if not math.isclose(
            self.original_alert.change_rate, self.actual_effect, abs_tol=1e-10
        ):
            raise ValueError("original_alert.change_rate must match actual_effect")
        if self.original_alert.severity != self.severity:
            raise ValueError("original_alert.severity must match severity")
        return self


def concrete_case_from_manifest(manifest: CaseManifest) -> GroundTruthCase:
    """Return the Ground Truth-shaped view used by the existing validators."""

    return GroundTruthCase(
        case_id=manifest.case_id,
        fault_id=manifest.fault_id,
        root_cause_type=manifest.root_cause_type,
        affected_metric=manifest.affected_metric,
        affected_assets=manifest.affected_assets,
        injection=manifest.injection,
        expected_evidence=manifest.expected_evidence,
        evidence_paths=manifest.evidence_paths,
        expected_direction=manifest.expected_direction,
        effect_size_type=manifest.effect_size_type,
        minimum_effect_size=manifest.minimum_effect_size,
    )


def manifest_payload(manifest: CaseManifest) -> dict[str, Any]:
    """Serialize a manifest using portable JSON-compatible scalar values."""

    return manifest.model_dump(mode="json", exclude_none=True)
