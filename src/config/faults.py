from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class FaultDefinition(BaseModel):
    """Canonical, machine-readable definition of one fault family."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^F(?:0[1-9]|1[0-2])$")
    root_cause_type: str = Field(min_length=1)
    affected_metrics: list[str] = Field(min_length=1)
    affected_assets: list[str] = Field(min_length=1)
    injection_strategy: str = Field(min_length=1)
    expected_evidence: list[str] = Field(min_length=2)
    expected_direction: Literal["increase", "decrease"]
    effect_size_type: Literal["relative", "absolute"] = "relative"
    minimum_effect_size: float = Field(gt=0)
    aliases: list[str] = Field(default_factory=list)


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
    expected_direction: Literal["increase", "decrease"]
    effect_size_type: Literal["relative", "absolute"] = "relative"
    minimum_effect_size: float = Field(gt=0)


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
        fault = active_catalog.by_id(case.fault_id)
        if case.root_cause_type != fault.root_cause_type:
            raise ValueError(
                f"{case.case_id} root cause does not match {case.fault_id}"
            )
        if case.affected_metric not in fault.affected_metrics:
            raise ValueError(f"{case.case_id} metric is not valid for {case.fault_id}")
        if case.injection.strategy != fault.injection_strategy:
            raise ValueError(f"{case.case_id} strategy does not match {case.fault_id}")
        if case.expected_direction != fault.expected_direction:
            raise ValueError(f"{case.case_id} direction does not match {case.fault_id}")
        if case.effect_size_type != fault.effect_size_type:
            raise ValueError(f"{case.case_id} effect type does not match {case.fault_id}")
    return cases
