"""Deterministic generation and validation for the standard 60-case set.

Canonical semantic fields are always loaded from the fault catalog and the
12 Ground Truth seed YAML files.  ``benchmark/cases/variants.yaml`` contains
only concrete seeds, dates, and typed injection parameter overrides.

Run ``python -m benchmark.case_generator`` to regenerate manifests, or
``python -m benchmark.case_generator --check`` to detect drift without writing
the repository.  ``--materialize CASE_ID`` writes runtime data only when an
explicit output directory is supplied.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import tempfile
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from agents.planner import Alert
from benchmark.cases import (
    CASE_ID_RE,
    BaselineConfig,
    CaseManifest,
    CaseVariant,
    VariantConfig,
    concrete_case_from_manifest,
    manifest_payload,
)
from benchmark.evaluation import calculate_effect, validate_effect
from benchmark.fault_injector import (
    FaultInjectionResult,
    inject_case,
    validate_expected_evidence,
)
from config.faults import (
    FaultCatalog,
    GroundTruthCase,
    InjectionSpec,
    load_fault_catalog,
    load_ground_truth_cases,
    validate_ground_truth_case,
)
from data.generator import generate_dataset, write_outputs

ROOT = Path(__file__).parents[2]
GROUND_TRUTH_DIRECTORY = ROOT / "benchmark" / "ground_truth"
CASES_DIRECTORY = ROOT / "benchmark" / "cases"
VARIANTS_PATH = CASES_DIRECTORY / "variants.yaml"
MANIFEST_PATTERN = re.compile(r"^F(?:0[1-9]|1[0-2])-00[1-5]\.yaml$")
CASE_SCHEMA_VERSION = 1

# Tables are compared by their stable logical identity.  This makes the
# affected-row contract work for both row mutations and metadata-only faults.
ROW_ID_COLUMNS: dict[str, tuple[str, ...]] = {
    "users": ("user_id",),
    "events": ("event_id",),
    "subscriptions": ("subscription_id",),
    "experiment_assignments": ("experiment_id", "user_id"),
    "daily_metrics": ("metric_date",),
    "pipeline_runs": ("job_id",),
    "partition_metadata": ("table_name", "partition_value"),
    "schema_snapshots": ("table_name", "version"),
    "metric_versions": ("metric_id", "version"),
    "experiment_configs": ("experiment_id", "version"),
}


def load_variant_config(path: str | Path = VARIANTS_PATH) -> VariantConfig:
    """Load and validate the versioned parameter-only variant configuration."""

    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    config = VariantConfig.model_validate(payload)
    if config.schema_version != CASE_SCHEMA_VERSION:
        raise ValueError(f"unsupported variants schema_version: {config.schema_version}")
    return config


def _seed_cases(
    directory: str | Path = GROUND_TRUTH_DIRECTORY,
    catalog: FaultCatalog | None = None,
) -> dict[str, GroundTruthCase]:
    active_catalog = catalog or load_fault_catalog()
    cases = load_ground_truth_cases(directory, active_catalog)
    by_fault: dict[str, GroundTruthCase] = {}
    for case in cases:
        if not case.case_id.endswith("-001"):
            raise ValueError(f"canonical seed case must end in -001: {case.case_id}")
        if case.fault_id in by_fault:
            raise ValueError(f"multiple canonical seeds found for {case.fault_id}")
        by_fault[case.fault_id] = case
    expected = {f"F{number:02d}" for number in range(1, 13)}
    if set(by_fault) != expected:
        raise ValueError("canonical Ground Truth seeds must cover F01-F12")
    return by_fault


def _concrete_case(
    seed_case: GroundTruthCase,
    variant: CaseVariant,
    catalog: FaultCatalog,
) -> GroundTruthCase:
    if variant.source_seed_case_id != seed_case.case_id:
        raise ValueError(
            f"{variant.case_id} source seed does not match {seed_case.case_id}"
        )
    if variant.variant_index == 1:
        if variant.metric_date != seed_case.injection.metric_date:
            raise ValueError(f"{variant.case_id} must retain the canonical seed date")
        if variant.injection_overrides.model_dump(
            mode="python", exclude_unset=True, exclude_none=True
        ):
            raise ValueError(f"{variant.case_id} must retain canonical injection parameters")

    injection_values = seed_case.injection.model_dump(mode="python")
    overrides = variant.injection_overrides.model_dump(
        mode="python", exclude_unset=True, exclude_none=True
    )
    seed_parameters = {
        name
        for name, value in injection_values.items()
        if name != "metric_date" and value is not None
    }
    invalid_overrides = set(overrides).difference(seed_parameters)
    if invalid_overrides:
        raise ValueError(
            f"{variant.case_id} overrides parameters not present in its seed: "
            + ", ".join(sorted(invalid_overrides))
        )
    injection_values.update(overrides)
    injection_values["metric_date"] = variant.metric_date
    injection = InjectionSpec.model_validate(injection_values)
    concrete = seed_case.model_copy(
        update={"case_id": variant.case_id, "injection": injection}
    )
    return validate_ground_truth_case(concrete, catalog)


def _baseline_from_config(config: BaselineConfig) -> dict[str, pd.DataFrame]:
    return generate_dataset(
        config.user_count,
        config.days,
        config.event_count,
        config.seed,
        pd.Timestamp(config.start_date),
    )


def severity_for_effect(effect: float) -> str:
    """Map signed effect magnitude to a stable alert severity."""

    magnitude = abs(float(effect))
    if magnitude >= 0.50:
        return "critical"
    if magnitude >= 0.20:
        return "high"
    if magnitude >= 0.05:
        return "medium"
    return "low"


def _stable_float(value: float) -> float:
    """Normalize derived numeric observations before writing YAML."""

    return round(float(value), 8)


def _json_value(value: object) -> object:
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, (pd.Timestamp, date)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if pd.isna(value):
        return None
    return value


def _row_key(table_name: str, row: pd.Series, fallback: int) -> tuple[object, ...]:
    columns = ROW_ID_COLUMNS.get(table_name)
    if columns and all(column in row.index for column in columns):
        return tuple(_json_value(row[column]) for column in columns)
    return (fallback,)


def _row_fingerprint(row: pd.Series) -> str:
    payload = {
        str(column): _json_value(row[column])
        for column in sorted(row.index, key=str)
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _changed_row_count(
    baseline_tables: dict[str, pd.DataFrame],
    fault_tables: dict[str, pd.DataFrame],
    assets: list[str],
) -> int:
    """Count changed logical rows once per affected asset.

    A row is counted once when its stable key is added, removed, or its
    payload changes.  Duplicate rows are counted by occurrence, which captures
    duplicate-batch and join-explosion additions.  Summing across affected
    assets gives metadata/config-only faults the same explicit semantics as
    source-data faults without inventing a row count.
    """

    total = 0
    for table_name in assets:
        baseline = baseline_tables[table_name].reset_index(drop=True)
        faulty = fault_tables[table_name].reset_index(drop=True)
        baseline_rows: dict[tuple[object, ...], Counter[str]] = {}
        faulty_rows: dict[tuple[object, ...], Counter[str]] = {}
        for index, row in baseline.iterrows():
            key = _row_key(table_name, row, index)
            baseline_rows.setdefault(key, Counter())[_row_fingerprint(row)] += 1
        for index, row in faulty.iterrows():
            key = _row_key(table_name, row, index)
            faulty_rows.setdefault(key, Counter())[_row_fingerprint(row)] += 1

        for key in set(baseline_rows) | set(faulty_rows):
            before = baseline_rows.get(key, Counter())
            after = faulty_rows.get(key, Counter())
            if len(before) == 1 and len(after) == 1:
                total += int(before != after)
                continue
            total += sum((before - after).values()) + sum((after - before).values())
    return total


def _metric_value(
    tables: dict[str, pd.DataFrame], metric_date: date, metric: str
) -> float:
    rows = tables["daily_metrics"].loc[
        pd.to_datetime(tables["daily_metrics"]["metric_date"]).dt.date.eq(metric_date),
        metric,
    ]
    if len(rows) != 1:
        raise ValueError(f"expected one {metric} value on {metric_date}, found {len(rows)}")
    return float(rows.iloc[0])


def _metric_date_is_in_window(metric_date: date, config: BaselineConfig) -> bool:
    return config.start_date <= metric_date <= config.start_date + timedelta(
        days=config.days - 1
    )


def _validate_variant_strategy_context(
    case: GroundTruthCase, config: BaselineConfig
) -> None:
    metric_date = case.injection.metric_date
    if metric_date is None or not _metric_date_is_in_window(metric_date, config):
        raise ValueError(f"{case.case_id} metric_date is outside the baseline window")
    if case.injection.strategy == "delay_android_events_one_day":
        shift_days = case.injection.shift_days or 1
        if not _metric_date_is_in_window(
            metric_date + timedelta(days=shift_days), config
        ):
            raise ValueError(
                f"{case.case_id} delayed-event follow-up date is outside the baseline window"
            )


def generate_case_manifest(
    seed_case: GroundTruthCase,
    variant: CaseVariant,
    *,
    baseline_config: BaselineConfig,
    baseline_tables: dict[str, pd.DataFrame] | None = None,
    catalog: FaultCatalog | None = None,
) -> CaseManifest:
    """Materialize one variant and derive its alert and observed contract."""

    active_catalog = catalog or load_fault_catalog()
    case = _concrete_case(seed_case, variant, active_catalog)
    _validate_variant_strategy_context(case, baseline_config)
    baseline = baseline_tables or _baseline_from_config(baseline_config)
    result = inject_case(
        baseline,
        case,
        rng=np.random.default_rng(variant.seed),
        start_date=pd.Timestamp(baseline_config.start_date),
        days=baseline_config.days,
    )
    validate_expected_evidence(result, baseline, case)

    expected_value = _stable_float(
        _metric_value(baseline, case.injection.metric_date, case.affected_metric)
    )
    observed_value = _stable_float(
        _metric_value(result.tables, case.injection.metric_date, case.affected_metric)
    )
    actual_effect = _stable_float(calculate_effect(
        expected_value,
        observed_value,
        effect_size_type=case.effect_size_type,
    ))
    if not validate_effect(
        expected_value,
        observed_value,
        expected_direction=case.expected_direction,
        effect_size_type=case.effect_size_type,
        minimum_effect_size=case.minimum_effect_size,
    ):
        raise ValueError(f"{case.case_id} effect contract failed during generation")
    severity = severity_for_effect(actual_effect)
    alert = Alert(
        incident_id=f"INC-{case.case_id}",
        metric=case.affected_metric,
        observed_at=case.injection.metric_date,
        expected_value=expected_value,
        observed_value=observed_value,
        change_rate=actual_effect,
        severity=severity,
    )
    return CaseManifest(
        schema_version=CASE_SCHEMA_VERSION,
        case_id=case.case_id,
        fault_id=case.fault_id,
        source_seed_case_id=seed_case.case_id,
        variant_index=variant.variant_index,
        seed=variant.seed,
        baseline_seed=baseline_config.seed,
        baseline_start_date=baseline_config.start_date,
        baseline_days=baseline_config.days,
        baseline_user_count=baseline_config.user_count,
        baseline_event_count=baseline_config.event_count,
        metric_date=case.injection.metric_date,
        root_cause_type=case.root_cause_type,
        affected_metric=case.affected_metric,
        affected_assets=case.affected_assets,
        injection=case.injection,
        original_alert=alert,
        expected_direction=case.expected_direction,
        effect_size_type=case.effect_size_type,
        minimum_effect_size=case.minimum_effect_size,
        actual_effect=actual_effect,
        affected_row_count=_changed_row_count(
            baseline, result.tables, case.affected_assets
        ),
        severity=severity,
        expected_evidence=case.expected_evidence,
        evidence_paths=case.evidence_paths,
    )


def generate_case_manifests(
    variants_path: str | Path = VARIANTS_PATH,
    *,
    ground_truth_directory: str | Path = GROUND_TRUTH_DIRECTORY,
    baseline_tables: dict[str, pd.DataFrame] | None = None,
) -> list[CaseManifest]:
    """Generate all 60 manifests from the canonical seeds and variant file."""

    config = load_variant_config(variants_path)
    catalog = load_fault_catalog()
    seeds = _seed_cases(ground_truth_directory, catalog)
    baseline = baseline_tables or _baseline_from_config(config.baseline)
    manifests = [
        generate_case_manifest(
            seeds[variant.source_seed_case_id[:3]],
            variant,
            baseline_config=config.baseline,
            baseline_tables=baseline,
            catalog=catalog,
        )
        for variant in sorted(config.variants, key=lambda item: item.case_id)
    ]
    return sorted(manifests, key=lambda manifest: manifest.case_id)


def validate_case_manifest(
    manifest: CaseManifest, *, catalog: FaultCatalog | None = None
) -> CaseManifest:
    """Validate a generated manifest against the canonical Ground Truth contract."""

    if manifest.schema_version != CASE_SCHEMA_VERSION:
        raise ValueError(f"unsupported manifest schema_version: {manifest.schema_version}")
    active_catalog = catalog or load_fault_catalog()
    seeds = _seed_cases(catalog=active_catalog)
    seed = seeds[manifest.source_seed_case_id[:3]]
    if seed.case_id != manifest.source_seed_case_id:
        raise ValueError(f"{manifest.case_id} source seed is not canonical")
    concrete = concrete_case_from_manifest(manifest)
    validate_ground_truth_case(concrete, active_catalog)
    if concrete.root_cause_type != seed.root_cause_type:
        raise ValueError(f"{manifest.case_id} root cause drifted from its seed")
    if concrete.affected_metric != seed.affected_metric:
        raise ValueError(f"{manifest.case_id} metric drifted from its seed")
    if concrete.affected_assets != seed.affected_assets:
        raise ValueError(f"{manifest.case_id} affected assets drifted from its seed")
    return manifest


def _manifest_yaml(manifest: CaseManifest) -> str:
    return yaml.safe_dump(
        manifest_payload(manifest),
        allow_unicode=False,
        default_flow_style=False,
        sort_keys=False,
    )


def _manifest_path(case_id: str, directory: Path) -> Path:
    if not CASE_ID_RE.fullmatch(case_id):
        raise ValueError(f"invalid case id: {case_id!r}")
    path = (directory / f"{case_id}.yaml").resolve()
    root = directory.resolve()
    if path.parent != root:
        raise ValueError("case path escapes the manifest directory")
    return path


def load_case_manifest(
    case: str | Path,
    directory: str | Path = CASES_DIRECTORY,
) -> CaseManifest:
    """Load one manifest by safe case ID or a path inside the case directory."""

    root = Path(directory).resolve()
    if isinstance(case, Path) or (isinstance(case, str) and case.endswith(".yaml")):
        path = Path(case).resolve()
        if path.parent != root or not MANIFEST_PATTERN.fullmatch(path.name):
            raise ValueError("manifest path must be a case YAML in the case directory")
    else:
        path = _manifest_path(str(case), root)
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    manifest = CaseManifest.model_validate(payload)
    return validate_case_manifest(manifest)


def load_case_manifests(
    directory: str | Path = CASES_DIRECTORY,
) -> list[CaseManifest]:
    """Load all generated manifests in stable case-ID order."""

    root = Path(directory)
    paths = sorted(
        path for path in root.glob("F??-???.yaml") if MANIFEST_PATTERN.fullmatch(path.name)
    )
    if len(paths) != 60:
        raise ValueError(f"expected 60 case manifests, found {len(paths)}")
    manifests = [load_case_manifest(path, root) for path in paths]
    if len({manifest.case_id for manifest in manifests}) != 60:
        raise ValueError("case manifest IDs must be unique")
    return manifests


def _materialize_from_manifest(
    manifest: CaseManifest,
    *,
    baseline_tables: dict[str, pd.DataFrame] | None = None,
) -> tuple[dict[str, pd.DataFrame], FaultInjectionResult]:
    baseline = baseline_tables
    if baseline is None:
        baseline = generate_dataset(
            manifest.baseline_user_count,
            manifest.baseline_days,
            manifest.baseline_event_count,
            manifest.baseline_seed,
            pd.Timestamp(manifest.baseline_start_date),
        )
    case = concrete_case_from_manifest(manifest)
    result = inject_case(
        baseline,
        case,
        rng=np.random.default_rng(manifest.seed),
        start_date=pd.Timestamp(manifest.baseline_start_date),
        days=manifest.baseline_days,
    )
    validate_expected_evidence(result, baseline, case)
    expected_value = _stable_float(
        _metric_value(baseline, manifest.metric_date, manifest.affected_metric)
    )
    observed_value = _stable_float(
        _metric_value(result.tables, manifest.metric_date, manifest.affected_metric)
    )
    actual_effect = _stable_float(
        calculate_effect(
            expected_value, observed_value, effect_size_type=manifest.effect_size_type
        )
    )
    if not math.isclose(actual_effect, manifest.actual_effect, abs_tol=1e-10):
        raise ValueError(f"{manifest.case_id} actual_effect is not reproducible")
    actual_row_count = _changed_row_count(
        baseline, result.tables, manifest.affected_assets
    )
    if actual_row_count != manifest.affected_row_count:
        raise ValueError(f"{manifest.case_id} affected_row_count is not reproducible")
    return baseline, result


def materialize_case(
    case: str | Path | CaseManifest,
    *,
    directory: str | Path = CASES_DIRECTORY,
    baseline_tables: dict[str, pd.DataFrame] | None = None,
) -> FaultInjectionResult:
    """Generate a fixed baseline and apply one manifest through ``inject_case``."""

    manifest = (
        case
        if isinstance(case, CaseManifest)
        else load_case_manifest(case, directory)
    )
    validate_case_manifest(manifest)
    _, result = _materialize_from_manifest(
        manifest,
        baseline_tables=baseline_tables,
    )
    return result


def _write_manifests(directory: Path, manifests: list[CaseManifest]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for manifest in manifests:
        (directory / f"{manifest.case_id}.yaml").write_text(
            _manifest_yaml(manifest), encoding="utf-8", newline="\n"
        )


def _check_manifests(
    manifests: list[CaseManifest], directory: Path = CASES_DIRECTORY
) -> list[str]:
    """Compare regenerated files with committed files without touching them."""

    with tempfile.TemporaryDirectory() as temp_dir:
        generated_dir = Path(temp_dir)
        _write_manifests(generated_dir, manifests)
        expected_names = {f"{manifest.case_id}.yaml" for manifest in manifests}
        actual_names = {
            path.name
            for path in directory.glob("F??-???.yaml")
            if MANIFEST_PATTERN.fullmatch(path.name)
        }
        drift: list[str] = []
        if actual_names != expected_names:
            drift.append("manifest file set differs")
        for name in sorted(expected_names & actual_names):
            if (generated_dir / name).read_text(encoding="utf-8") != (
                directory / name
            ).read_text(encoding="utf-8"):
                drift.append(name)
        return drift


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail when manifests drift")
    mode.add_argument("--materialize", metavar="CASE_ID", help="materialize one case")
    parser.add_argument(
        "--output",
        type=Path,
        help="output directory for --materialize (required for runtime data)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifests = generate_case_manifests()
    if args.check:
        drift = _check_manifests(manifests)
        if drift:
            print("manifest drift detected: " + ", ".join(drift))
            return 1
        print("60 cases checked; no manifest drift")
        return 0
    if args.materialize:
        result = materialize_case(args.materialize)
        if args.output is not None:
            write_outputs(args.output, result.tables)
            print(f"materialized {args.materialize} at {args.output}")
        else:
            print(
                f"{args.materialize} materialized in memory; use --output to write tables"
            )
        return 0
    _write_manifests(CASES_DIRECTORY, manifests)
    print("60 cases generated")
    print("12 fault families")
    print("5 variants each")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
