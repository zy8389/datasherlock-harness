from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _resolve_config_path(filename: str) -> Path:
    candidates = (
        Path.cwd() / "config" / filename,
        Path(__file__).parents[2] / "config" / filename,
        Path("/workspace/config") / filename,
    )
    return next(
        (candidate for candidate in candidates if candidate.is_file()), candidates[0]
    )


DEFAULT_FAULT_CATALOG_PATH = _resolve_config_path("fault_catalog.yaml")
EXPECTED_FAULT_IDS = {f"F{number:02d}" for number in range(1, 13)}
INDEPENDENT_METADATA_EVIDENCE_FAULT_IDS = frozenset(
    {"F01", "F04", "F05", "F10", "F11", "F12"}
)


class EvidenceSourceType(StrEnum):
    """Machine-readable categories for independent benchmark evidence."""

    BUSINESS_DATA = "business_data"
    OPERATIONAL_METADATA = "operational_metadata"
    SCHEMA_METADATA = "schema_metadata"
    METRIC_VERSION = "metric_version"
    EXPERIMENT_CONFIG = "experiment_config"


# This is the single runtime provenance policy for benchmark-owned assets.
# Unknown assets intentionally remain unclassified instead of being treated as
# business data.
EVIDENCE_SOURCE_BY_ASSET: Final[Mapping[str, EvidenceSourceType]] = MappingProxyType(
    {
        "events": EvidenceSourceType.BUSINESS_DATA,
        "users": EvidenceSourceType.BUSINESS_DATA,
        "subscriptions": EvidenceSourceType.BUSINESS_DATA,
        "experiment_assignments": EvidenceSourceType.BUSINESS_DATA,
        "daily_metrics": EvidenceSourceType.BUSINESS_DATA,
        "partition_metadata": EvidenceSourceType.OPERATIONAL_METADATA,
        "pipeline_runs": EvidenceSourceType.OPERATIONAL_METADATA,
        "schema_snapshots": EvidenceSourceType.SCHEMA_METADATA,
        "metric_versions": EvidenceSourceType.METRIC_VERSION,
        "experiment_configs": EvidenceSourceType.EXPERIMENT_CONFIG,
    }
)


def evidence_source_for_asset(asset: str) -> EvidenceSourceType | None:
    """Return canonical provenance for one physical asset, failing closed."""

    normalized = asset.strip().lower().rsplit(".", maxsplit=1)[-1].strip('"')
    return EVIDENCE_SOURCE_BY_ASSET.get(normalized)


def evidence_source_for_data_quality_result(
    tool_name: str,
    table: str,
) -> EvidenceSourceType | None:
    """Classify a DQ result using the same policy used for planned steps."""

    if tool_name == "detect_schema_drift":
        return EvidenceSourceType.SCHEMA_METADATA
    return evidence_source_for_asset(table)


def evidence_assets_by_source() -> dict[str, list[str]]:
    """Render the canonical source-to-assets view used in Planner prompts."""

    grouped = {source.value: [] for source in EvidenceSourceType}
    for asset, source in EVIDENCE_SOURCE_BY_ASSET.items():
        grouped[source.value].append(asset)
    return grouped


class EvidencePath(BaseModel):
    """One machine-readable source, asset, and signal contract."""

    model_config = ConfigDict(extra="forbid")

    source_type: EvidenceSourceType
    asset: str = Field(min_length=1)
    signal: str = Field(min_length=1)

    @field_validator("asset", "signal")
    @classmethod
    def reject_blank_values(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence path values must not be blank")
        return value


class FaultDefinition(BaseModel):
    """Canonical, machine-readable definition of one fault family."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^F(?:0[1-9]|1[0-2])$")
    root_cause_type: str = Field(min_length=1)
    affected_metrics: list[str] = Field(min_length=1)
    affected_assets: list[str] = Field(min_length=1)
    injection_strategy: str = Field(min_length=1)
    expected_evidence: list[str] = Field(min_length=2)
    evidence_source_types: list[EvidenceSourceType] = Field(default_factory=list)
    expected_direction: Literal["increase", "decrease"]
    effect_size_type: Literal["relative", "absolute"] = "relative"
    minimum_effect_size: float = Field(gt=0)
    aliases: list[str] = Field(default_factory=list)
    verification_fields: list[str] = Field(min_length=1)
    diagnostic_tools: list[str] = Field(min_length=1)


class FaultCatalog(BaseModel):
    """The one canonical taxonomy used by injection and evaluation."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(gt=0)
    faults: list[FaultDefinition] = Field(min_length=12, max_length=12)

    @model_validator(mode="after")
    def validate_catalog(self) -> FaultCatalog:
        ids = {fault.id for fault in self.faults}
        root_causes = [fault.root_cause_type for fault in self.faults]
        if ids != EXPECTED_FAULT_IDS:
            raise ValueError("fault catalog must contain exactly F01-F12")
        if len(root_causes) != len(set(root_causes)):
            raise ValueError("root_cause_type values must be unique")
        for fault in self.faults:
            for field_name in ("verification_fields", "diagnostic_tools"):
                values = getattr(fault, field_name)
                if any(not value.strip() for value in values):
                    raise ValueError(f"{fault.id} {field_name} must not contain blanks")
                if len(values) != len(set(values)):
                    raise ValueError(f"{fault.id} {field_name} must be unique")
            source_types = set(fault.evidence_source_types)
            if len(source_types) != len(fault.evidence_source_types):
                raise ValueError(
                    f"{fault.id} evidence_source_types must be unique"
                )
            if fault.id in INDEPENDENT_METADATA_EVIDENCE_FAULT_IDS and (
                EvidenceSourceType.BUSINESS_DATA not in source_types
                or len(source_types) < 2
            ):
                raise ValueError(
                    f"{fault.id} must declare business and independent evidence sources"
                )
        return self

    def by_id(self, fault_id: str) -> FaultDefinition:
        for fault in self.faults:
            if fault.id == fault_id:
                return fault
        raise KeyError(fault_id)


def load_fault_catalog(path: str | Path = DEFAULT_FAULT_CATALOG_PATH) -> FaultCatalog:
    """Load and validate the canonical fault taxonomy."""
    with Path(path).open(encoding="utf-8") as file:
        payload = yaml.safe_load(file)
    return FaultCatalog.model_validate(payload)


class GroundTruthCase(BaseModel):
    """Machine-readable expected result for one benchmark case."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    fault_id: str = Field(pattern=r"^F(?:0[1-9]|1[0-2])$")
    root_cause_type: str = Field(min_length=1)
    affected_metric: str = Field(min_length=1)
    affected_assets: list[str] = Field(min_length=1)
    injection: InjectionSpec
    expected_evidence: list[str] = Field(min_length=2)
    evidence_paths: list[EvidencePath] = Field(default_factory=list)
    expected_direction: Literal["increase", "decrease"]
    effect_size_type: Literal["relative", "absolute"] = "relative"
    minimum_effect_size: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_evidence_paths(self) -> GroundTruthCase:
        path_keys = [
            (path.source_type, path.asset, path.signal)
            for path in self.evidence_paths
        ]
        if len(path_keys) != len(set(path_keys)):
            raise ValueError(
                f"{self.case_id} evidence paths must be unique"
            )
        undeclared_assets = {
            path.asset for path in self.evidence_paths
        }.difference(self.affected_assets)
        if undeclared_assets:
            raise ValueError(
                f"{self.case_id} evidence assets are not affected assets: "
                + ", ".join(sorted(undeclared_assets))
            )
        return self


class InjectionSpec(BaseModel):
    """Strongly typed parameters for one concrete fault case."""

    model_config = ConfigDict(extra="forbid")

    strategy: str = Field(min_length=1)
    metric_date: date | None = None
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
    def validate_split(self) -> InjectionSpec:
        if (
            self.control_ratio is not None
            and self.treatment_ratio is not None
            and abs(self.control_ratio + self.treatment_ratio - 1.0) > 1e-9
        ):
            raise ValueError("control_ratio and treatment_ratio must sum to 1")
        return self


def load_ground_truth_cases(
    directory: str | Path, catalog: FaultCatalog | None = None
) -> list[GroundTruthCase]:
    """Load YAML cases and ensure every case uses the catalog's canonical label."""
    active_catalog = catalog or load_fault_catalog()
    cases = [
        GroundTruthCase.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in sorted(Path(directory).glob("*.yaml"))
    ]
    if not cases:
        raise ValueError(f"no ground-truth YAML files found in {directory}")
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("ground-truth case ids must be unique")
    for case in cases:
        validate_ground_truth_case(case, active_catalog)
    return cases


def validate_ground_truth_case(
    case: GroundTruthCase, catalog: FaultCatalog | None = None
) -> GroundTruthCase:
    """Apply the complete catalog-aware contract validation to one case.

    ``GroundTruthCase`` owns field-level and same-case checks.  This function
    is the single entry point for rules that need the canonical fault catalog,
    including evidence source declarations and independent-path requirements.
    """

    active_catalog = catalog or load_fault_catalog()
    fault = active_catalog.by_id(case.fault_id)
    if case.root_cause_type != fault.root_cause_type:
        raise ValueError(f"{case.case_id} root cause does not match {case.fault_id}")
    if case.affected_metric not in fault.affected_metrics:
        raise ValueError(f"{case.case_id} metric is not valid for {case.fault_id}")
    if set(case.affected_assets) != set(fault.affected_assets):
        raise ValueError(
            f"{case.case_id} affected_assets do not match {case.fault_id}"
        )
    if case.injection.strategy != fault.injection_strategy:
        raise ValueError(f"{case.case_id} strategy does not match {case.fault_id}")
    if case.expected_direction != fault.expected_direction:
        raise ValueError(f"{case.case_id} direction does not match {case.fault_id}")
    if case.effect_size_type != fault.effect_size_type:
        raise ValueError(f"{case.case_id} effect type does not match {case.fault_id}")
    if not math.isclose(
        case.minimum_effect_size,
        fault.minimum_effect_size,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError(
            f"{case.case_id} minimum_effect_size does not match {case.fault_id}"
        )

    declared_sources = set(fault.evidence_source_types)
    case_sources = {path.source_type for path in case.evidence_paths}
    undeclared_sources = case_sources.difference(declared_sources)
    if undeclared_sources:
        values = ", ".join(sorted(source.value for source in undeclared_sources))
        raise ValueError(
            f"{case.case_id} evidence source types are not declared by "
            f"{case.fault_id}: {values}"
        )
    missing_sources = declared_sources.difference(case_sources)
    if missing_sources:
        values = ", ".join(sorted(source.value for source in missing_sources))
        raise ValueError(
            f"{case.case_id} is missing required evidence sources: {values}"
        )

    if case.fault_id in INDEPENDENT_METADATA_EVIDENCE_FAULT_IDS:
        source_types = case_sources
        if len(case.evidence_paths) < 2:
            raise ValueError(f"{case.case_id} requires at least two evidence paths")
        if EvidenceSourceType.BUSINESS_DATA not in source_types:
            raise ValueError(f"{case.case_id} requires a business_data evidence path")
        if not source_types.difference({EvidenceSourceType.BUSINESS_DATA}):
            raise ValueError(
                f"{case.case_id} requires an independent non-business evidence path"
            )
    return case
