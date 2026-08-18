from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from benchmark.evaluation import calculate_effect, validate_effect
from config.faults import (
    GroundTruthCase,
    InjectionSpec,
    load_fault_catalog,
    load_ground_truth_cases,
)
from data.generator import (
    compute_definition_hash,
    generate_subscriptions,
    logical_dtype,
    materialize_daily_metrics,
)

REQUIRED_TABLES = {
    "users",
    "events",
    "subscriptions",
    "experiment_assignments",
    "daily_metrics",
    "pipeline_runs",
    "partition_metadata",
    "schema_snapshots",
    "metric_versions",
    "experiment_configs",
}

_GROUND_TRUTH_DIRECTORY = Path(__file__).parents[2] / "benchmark" / "ground_truth"


@dataclass
class FaultInjectionResult:
    """A complete, self-consistent fault dataset and its evaluation context."""

    fault_id: str
    metric_date: date
    tables: dict[str, pd.DataFrame]
    expected_direction: Literal["increase", "decrease"]
    faulty_queries: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    case_id: str | None = None
    affected_metric: str | None = None
    effect_size_type: Literal["relative", "absolute"] = "relative"
    minimum_effect_size: float | None = None
    actual_effect: float | None = None


def _copy_tables(tables: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Copy every input table before applying an injection strategy."""
    return {name: frame.copy(deep=True) for name, frame in tables.items()}


def _day_mask(events: pd.DataFrame, metric_date: date) -> pd.Series:
    return events["event_time"].dt.date.eq(metric_date)


def _infer_window(
    tables: dict[str, pd.DataFrame], metric_date: date
) -> tuple[pd.Timestamp, int]:
    events = tables["events"]
    start = pd.Timestamp(events["event_time"].min()).normalize()
    end = pd.Timestamp(events["event_time"].max()).normalize()
    if pd.isna(start) or pd.isna(end):
        return pd.Timestamp(metric_date), 1
    return start, max(1, int((end - start).days + 1))


def _required_ratio(spec: InjectionSpec) -> float:
    if spec.ratio is None:
        raise ValueError(f"{spec.strategy} requires injection.ratio")
    return spec.ratio


def _sample_indices(
    frame: pd.DataFrame,
    mask: pd.Series,
    ratio: float,
    rng: np.random.Generator,
) -> pd.Index:
    candidates = frame.index[mask]
    count = round(len(candidates) * ratio)
    if count == 0:
        return candidates[:0]
    return pd.Index(rng.choice(candidates.to_numpy(), size=count, replace=False))


def _sample_complete_user_events(
    events: pd.DataFrame,
    mask: pd.Series,
    ratio: float,
    rng: np.random.Generator,
) -> pd.Index:
    """Select an exact row ratio while preferentially moving whole user groups."""
    candidates = events.loc[mask]
    target_count = round(len(candidates) * ratio)
    if target_count == 0:
        return candidates.index[:0]
    grouped = list(candidates.groupby("user_id", dropna=False).groups.values())
    order = rng.permutation(len(grouped))
    selected: list[int] = []
    remaining = target_count
    for group_position in order:
        group = list(grouped[group_position])
        if len(group) <= remaining:
            selected.extend(group)
            remaining -= len(group)
    if remaining:
        remaining_indices = candidates.index.difference(pd.Index(selected))
        selected.extend(rng.choice(remaining_indices.to_numpy(), size=remaining, replace=False))
    return pd.Index(selected)


def _append_metric_version(
    tables: dict[str, pd.DataFrame],
    metric_id: str,
    query: str,
    effective_at: date,
) -> None:
    versions = tables["metric_versions"]
    existing = versions.loc[versions["metric_id"].eq(metric_id)]
    next_version = int(existing["version"].max()) + 1 if not existing.empty else 1
    row = pd.DataFrame(
        [
            {
                "metric_id": metric_id,
                "version": next_version,
                "definition_hash": compute_definition_hash(query),
                "query": query,
                "effective_at": pd.Timestamp(effective_at),
            }
        ]
    )
    tables["metric_versions"] = pd.concat([versions, row], ignore_index=True)


def _append_schema_snapshot(
    tables: dict[str, pd.DataFrame],
    table_name: str,
    schema: dict[str, str],
    effective_at: date,
) -> None:
    snapshots = tables["schema_snapshots"]
    existing = snapshots.loc[snapshots["table_name"].eq(table_name)]
    next_version = int(existing["version"].max()) + 1 if not existing.empty else 1
    row = pd.DataFrame(
        [
            {
                "table_name": table_name,
                "version": next_version,
                "schema_json": json.dumps(schema, sort_keys=True),
                "effective_at": pd.Timestamp(effective_at),
            }
        ]
    )
    tables["schema_snapshots"] = pd.concat([snapshots, row], ignore_index=True)


def _append_experiment_config(
    tables: dict[str, pd.DataFrame],
    *,
    control_ratio: float,
    treatment_ratio: float,
    effective_at: date,
) -> None:
    configs = tables["experiment_configs"]
    experiment_id = "exp_onboarding_v1"
    existing = configs.loc[configs["experiment_id"].eq(experiment_id)]
    next_version = int(existing["version"].max()) + 1 if not existing.empty else 1
    row = pd.DataFrame(
        [
            {
                "experiment_id": experiment_id,
                "version": next_version,
                "control_ratio": control_ratio,
                "treatment_ratio": treatment_ratio,
                "hash_key": "user_id",
                "effective_at": pd.Timestamp(effective_at),
                "status": "active",
            }
        ]
    )
    tables["experiment_configs"] = pd.concat([configs, row], ignore_index=True)


def _upsert_partition(
    tables: dict[str, pd.DataFrame],
    *,
    partition_date: date,
    device_type: str,
    status: str | None = None,
) -> None:
    partitions = tables["partition_metadata"]
    value = f"{partition_date}/{device_type}"
    row_mask = partitions["partition_value"].eq(value)
    events = tables["events"]
    row_count = int(
        (_day_mask(events, partition_date) & events["device_type"].eq(device_type)).sum()
    )
    updated_at = pd.Timestamp(partition_date) + pd.Timedelta(minutes=10)
    if row_mask.any():
        partitions.loc[row_mask, "row_count"] = row_count
        partitions.loc[row_mask, "updated_at"] = updated_at
        if status is not None:
            partitions.loc[row_mask, "status"] = status
        return
    row = pd.DataFrame(
        [
            {
                "table_name": "events",
                "partition_key": "metric_date/device_type",
                "partition_value": value,
                "row_count": row_count,
                "updated_at": updated_at,
                "status": status or "ready",
                "source_job_id": f"job_events_{partition_date:%Y%m%d}",
            }
        ]
    )
    tables["partition_metadata"] = pd.concat([partitions, row], ignore_index=True)


def _mark_pipeline(
    tables: dict[str, pd.DataFrame],
    *,
    metric_date: date,
    status: str,
    error_type: str | None,
    error_message: str | None,
) -> None:
    pipeline = tables["pipeline_runs"]
    row = pipeline["target_table"].eq("events") & pipeline["target_partition"].eq(
        str(metric_date)
    )
    pipeline.loc[row, ["status", "error_type", "error_message"]] = [
        status,
        error_type,
        error_message,
    ]


def _schema_from_frame(
    frame: pd.DataFrame, overrides: dict[str, str] | None = None
) -> dict[str, str]:
    schema = {column: logical_dtype(dtype) for column, dtype in frame.dtypes.items()}
    schema.update(overrides or {})
    return schema


def _faulty_dau_subscription_join() -> str:
    return """
        SELECT CAST(e.event_time AS DATE) AS metric_date,
               COUNT(DISTINCT e.user_id) AS daily_active_users
        FROM events e
        INNER JOIN subscriptions s ON e.user_id = s.user_id
        GROUP BY metric_date
    """


def _faulty_ai_task_assignment_join() -> str:
    return """
        SELECT CAST(e.event_time AS DATE) AS metric_date,
               COUNT(e.event_id) AS ai_task_count
        FROM events e
        LEFT JOIN experiment_assignments a ON e.user_id = a.user_id
        WHERE e.event_name = 'run_ai_task'
        GROUP BY metric_date
    """


def _faulty_dau_core_task_filter() -> str:
    return """
        SELECT CAST(event_time AS DATE) AS metric_date,
               COUNT(DISTINCT user_id) AS daily_active_users
        FROM events
        WHERE event_name = 'run_ai_task'
        GROUP BY metric_date
    """


def _apply_strategy(
    tables: dict[str, pd.DataFrame],
    *,
    fault_id: str,
    metric_date: date,
    spec: InjectionSpec,
    rng: np.random.Generator,
    start_date: pd.Timestamp,
    days: int,
) -> tuple[dict[str, str], list[str]]:
    """Apply a strategy; concrete magnitudes are read from ``spec`` only."""
    events = tables["events"]
    mask = _day_mask(events, metric_date)
    faulty_queries: dict[str, str] = {}
    notes: list[str] = []

    if fault_id == "F01":
        device_type = spec.device_type or "android"
        target = mask & events["device_type"].eq(device_type)
        tables["events"] = events.loc[~target].reset_index(drop=True)
        _upsert_partition(
            tables, partition_date=metric_date, device_type=device_type, status="missing"
        )
        _mark_pipeline(
            tables,
            metric_date=metric_date,
            status="failed",
            error_type="missing_partition",
            error_message=f"{device_type} partition was not written",
        )
        notes.append(f"{device_type} events on the target partition were removed")
    elif fault_id == "F02":
        ratio = _required_ratio(spec)
        candidates = events.loc[mask & events["event_name"].eq("run_ai_task")]
        if candidates.empty:
            candidates = events.loc[mask]
        selected = _sample_indices(events, events.index.isin(candidates.index), ratio, rng)
        tables["events"] = pd.concat([events, events.loc[selected]], ignore_index=True)
        _mark_pipeline(
            tables,
            metric_date=metric_date,
            status="warning",
            error_type="duplicate_batch",
            error_message="duplicate batch rows were loaded",
        )
        notes.append(f"{len(selected)} source rows were duplicated")
    elif fault_id == "F03":
        ratio = _required_ratio(spec)
        device_type = spec.device_type or "mobile"
        mobile = (
            events["device_type"].isin(["ios", "android"])
            if device_type == "mobile"
            else events["device_type"].eq(device_type)
        )
        selected = _sample_complete_user_events(events, mask & mobile, ratio, rng)
        tables["events"].loc[selected, "user_id"] = pd.NA
        _mark_pipeline(
            tables,
            metric_date=metric_date,
            status="warning",
            error_type="null_value_anomaly",
            error_message="mobile user_id values are null",
        )
        notes.append(f"{len(selected)} target-date {device_type} user_id values are null")
    elif fault_id == "F04":
        ratio = _required_ratio(spec)
        device_type = spec.device_type or "android"
        shift_days = spec.shift_days if spec.shift_days is not None else 1
        selected = _sample_complete_user_events(
            events, mask & events["device_type"].eq(device_type), ratio, rng
        )
        tables["events"].loc[selected, "event_time"] += pd.Timedelta(days=shift_days)
        _upsert_partition(
            tables, partition_date=metric_date, device_type=device_type, status="delayed"
        )
        _upsert_partition(
            tables,
            partition_date=metric_date + pd.Timedelta(days=shift_days),
            device_type=device_type,
        )
        _mark_pipeline(
            tables,
            metric_date=metric_date,
            status="delayed",
            error_type="data_delay",
            error_message=f"{len(selected)} {device_type} events arrived late",
        )
        notes.append(
            f"{len(selected)} {device_type} events were delayed by {shift_days} day(s)"
        )
    elif fault_id == "F05":
        region = spec.region or "CN"
        shift_hours = spec.shift_hours if spec.shift_hours is not None else 8
        region_users = tables["users"].loc[
            tables["users"]["region"].eq(region), "user_id"
        ]
        target = mask & events["user_id"].isin(region_users)
        boundary = target & (
            events["event_time"].dt.hour.le(3) | events["event_time"].dt.hour.ge(20)
        )
        selected = boundary if boundary.any() else target
        tables["events"].loc[selected, "event_time"] += pd.Timedelta(hours=shift_hours)
        _mark_pipeline(
            tables,
            metric_date=metric_date,
            status="warning",
            error_type="timezone_error",
            error_message=f"{region} boundary events shifted by {shift_hours} hours",
        )
        notes.append(f"{region} boundary events are shifted by {shift_hours} hours")
    elif fault_id == "F06":
        ratio = _required_ratio(spec)
        multiplier = spec.multiplier
        if multiplier is None:
            raise ValueError(f"{spec.strategy} requires injection.multiplier")
        selected = _sample_indices(events, mask, ratio, rng)
        tables["events"].loc[selected, "duration_seconds"] *= multiplier
        notes.append(f"{len(selected)} event durations were multiplied by {multiplier}")
    elif fault_id == "F07":
        query = _faulty_dau_subscription_join()
        faulty_queries["daily_active_users"] = query
        _append_metric_version(tables, "daily_active_users", query, metric_date)
        notes.append("the metric SQL introduces an erroneous subscription inner join")
    elif fault_id == "F08":
        ratio = _required_ratio(spec)
        assignments = tables["experiment_assignments"]
        selected = _sample_indices(
            assignments,
            pd.Series(True, index=assignments.index),
            ratio,
            rng,
        )
        tables["experiment_assignments"] = pd.concat(
            [assignments, assignments.loc[selected]], ignore_index=True
        )
        query = _faulty_ai_task_assignment_join()
        faulty_queries["ai_task_count"] = query
        _append_metric_version(tables, "ai_task_count", query, metric_date)
        notes.append(f"{len(selected)} assignment rows were duplicated before joining")
    elif fault_id == "F09":
        ratio = _required_ratio(spec)
        from_value = spec.from_value
        to_value = spec.to_value
        if from_value is None or to_value is None:
            raise ValueError(f"{spec.strategy} requires from_value and to_value")
        selected = _sample_indices(events, mask & events["event_name"].eq(from_value), ratio, rng)
        tables["events"].loc[selected, "event_name"] = to_value
        notes.append(f"{len(selected)} {from_value} events were renamed to {to_value}")
    elif fault_id == "F10":
        tables["events"] = events.loc[~mask].reset_index(drop=True)
        incoming_type = spec.to_value or "VARCHAR"
        _append_schema_snapshot(
            tables,
            "events",
            _schema_from_frame(tables["events"], {"app_build_number": incoming_type}),
            metric_date,
        )
        _mark_pipeline(
            tables,
            metric_date=metric_date,
            status="failed",
            error_type="schema_change",
            error_message="incoming app_build_number VARCHAR failed strict compatibility validation",
        )
        for device_type in ("web", "ios", "android"):
            _upsert_partition(
                tables, partition_date=metric_date, device_type=device_type, status="failed"
            )
        notes.append(
            "incoming batch schema reports app_build_number VARCHAR; strict "
            "compatibility validation fails before the target partition is materialized"
        )
    elif fault_id == "F11":
        query = _faulty_dau_core_task_filter()
        faulty_queries["daily_active_users"] = query
        _append_metric_version(tables, "daily_active_users", query, metric_date)
        notes.append("the active-user definition is narrowed to core task events")
    elif fault_id == "F12":
        control_ratio = spec.control_ratio
        treatment_ratio = spec.treatment_ratio
        if control_ratio is None or treatment_ratio is None:
            raise ValueError(f"{spec.strategy} requires control_ratio and treatment_ratio")
        assignments = tables["experiment_assignments"].copy()
        original_variants = assignments.set_index("user_id")["variant"]
        assignments["variant"] = "control"
        treatment_count = round(len(assignments) * treatment_ratio)
        users = tables["users"].set_index("user_id")
        active_users = set(events.loc[mask, "user_id"].dropna().astype(int))
        preferred: list[int] = []
        for row in users.loc[users.index.intersection(original_variants.index)].itertuples():
            base_probability = 0.72 if row.user_type == "paid" else 0.32
            if (
                int(row.Index) in active_users
                and original_variants.loc[row.Index] == "control"
                and base_probability <= row.conversion_score < base_probability + 0.20
            ):
                preferred.append(int(row.Index))
        preferred = preferred[:treatment_count]
        remaining = assignments.loc[~assignments["user_id"].isin(preferred), "user_id"].to_numpy()
        fill_count = treatment_count - len(preferred)
        filler = rng.choice(remaining, size=fill_count, replace=False) if fill_count else []
        treatment_indices = assignments.index[assignments["user_id"].isin([*preferred, *filler])]
        assignments.loc[treatment_indices, "variant"] = "treatment"
        tables["experiment_assignments"] = assignments
        # User latent variables make outcomes deterministic across paired scenarios.
        tables["subscriptions"] = generate_subscriptions(
            tables["users"], start_date, days, rng, assignments, tables["events"]
        )
        _append_experiment_config(
            tables,
            control_ratio=control_ratio,
            treatment_ratio=treatment_ratio,
            effective_at=metric_date,
        )
        notes.append(
            "only experiment allocation changed; subscription outcomes reuse fixed user latents"
        )
    else:
        raise ValueError(f"unknown fault id: {fault_id}")

    return faulty_queries, notes


def _metric_value(tables: dict[str, pd.DataFrame], metric_date: date, metric: str) -> float:
    rows = tables["daily_metrics"].loc[
        pd.to_datetime(tables["daily_metrics"]["metric_date"]).dt.date.eq(metric_date), metric
    ]
    if rows.empty:
        raise ValueError(f"daily_metrics is missing {metric_date} for {metric}")
    return float(rows.iloc[0])


def validate_dataset_consistency(
    tables: dict[str, pd.DataFrame],
    *,
    expected_days: int,
) -> None:
    """Raise when an injected dataset is incomplete or not query-ready."""
    missing_tables = REQUIRED_TABLES.difference(tables)
    if missing_tables:
        raise ValueError(f"missing required tables: {sorted(missing_tables)}")
    daily_metrics = tables["daily_metrics"]
    if len(daily_metrics) != expected_days:
        raise ValueError("daily_metrics row count does not match expected_days")
    if "metric_date" not in daily_metrics or daily_metrics["metric_date"].duplicated().any():
        raise ValueError("daily_metrics must have unique metric_date values")
    required_columns = {
        "events": {"event_id", "user_id", "event_time", "event_name"},
        "pipeline_runs": {"target_table", "target_partition", "status"},
        "partition_metadata": {"partition_value", "row_count", "status"},
        "schema_snapshots": {"table_name", "version", "schema_json", "effective_at"},
        "metric_versions": {"metric_id", "version", "definition_hash", "query", "effective_at"},
        "experiment_configs": {"experiment_id", "version", "effective_at"},
    }
    for table_name, columns in required_columns.items():
        missing_columns = columns.difference(tables[table_name].columns)
        if missing_columns:
            raise ValueError(f"{table_name} missing columns: {sorted(missing_columns)}")


def validate_expected_evidence(
    result: FaultInjectionResult,
    baseline_tables: dict[str, pd.DataFrame] | None = None,
) -> None:
    """Verify a metric observation and an independent metadata observation."""
    tables = result.tables
    validate_dataset_consistency(tables, expected_days=len(tables["daily_metrics"]))
    if baseline_tables is not None and result.affected_metric is not None:
        baseline_value = _metric_value(
            baseline_tables, result.metric_date, result.affected_metric
        )
        fault_value = _metric_value(tables, result.metric_date, result.affected_metric)
        if result.minimum_effect_size is not None and not validate_effect(
            baseline_value,
            fault_value,
            expected_direction=result.expected_direction,
            effect_size_type=result.effect_size_type,
            minimum_effect_size=result.minimum_effect_size,
        ):
            raise ValueError("fault metric does not satisfy the effect contract")
    metadata_evidence = {
        "F01": tables["partition_metadata"]["status"].eq("missing").any(),
        "F04": tables["pipeline_runs"]["status"].eq("delayed").any(),
        "F10": tables["pipeline_runs"]["error_type"].eq("schema_change").any(),
        "F11": tables["metric_versions"]["version"].astype(int).ge(2).any(),
        "F12": tables["experiment_configs"]["version"].astype(int).ge(2).any(),
    }
    if result.fault_id in metadata_evidence and not metadata_evidence[result.fault_id]:
        raise ValueError(f"{result.fault_id} metadata evidence is missing")
    if baseline_tables is None:
        return
    base_events = baseline_tables["events"]
    fault_events = tables["events"]
    metric_date = result.metric_date
    base_day = _day_mask(base_events, metric_date)
    fault_day = _day_mask(fault_events, metric_date)
    evidence_checks = {
        "F01": len(fault_events) < len(base_events),
        "F02": len(fault_events) > len(base_events)
        and fault_events["event_id"].duplicated().any(),
        "F03": len(fault_events) == len(base_events)
        and fault_events["user_id"].isna().sum() > base_events["user_id"].isna().sum(),
        "F04": fault_day.sum() < base_day.sum(),
        "F05": not fault_events.equals(base_events),
        "F06": fault_events["duration_seconds"].max() > base_events["duration_seconds"].max(),
        "F07": "daily_active_users" in result.faulty_queries,
        "F08": tables["experiment_assignments"]["user_id"].duplicated().any()
        and "ai_task_count" in result.faulty_queries,
        "F09": fault_events["event_name"].eq("execute_ai_task").sum() > 0
        and fault_events["event_name"].eq("run_ai_task").sum()
        < base_events["event_name"].eq("run_ai_task").sum(),
        "F10": not fault_day.any()
        and tables["schema_snapshots"].query("table_name == 'events'")["version"].max() >= 2,
        "F11": len(fault_events) == len(base_events)
        and "daily_active_users" in result.faulty_queries,
        "F12": len(tables["experiment_assignments"]) == len(
            baseline_tables["experiment_assignments"]
        )
        and tables["experiment_assignments"]["variant"].value_counts().get("treatment", 0)
        > baseline_tables["experiment_assignments"]["variant"].value_counts().get(
            "treatment", 0
        ),
    }
    if not evidence_checks[result.fault_id]:
        raise ValueError(f"{result.fault_id} data evidence is missing")


def _inject(
    tables: dict[str, pd.DataFrame],
    *,
    fault_id: str,
    metric_date: date,
    spec: InjectionSpec,
    expected_direction: Literal["increase", "decrease"],
    affected_metric: str,
    effect_size_type: Literal["relative", "absolute"],
    minimum_effect_size: float,
    rng: np.random.Generator,
    start_date: pd.Timestamp,
    days: int,
    case_id: str | None,
) -> FaultInjectionResult:
    baseline_value = _metric_value(tables, metric_date, affected_metric)
    mutated = _copy_tables(tables)
    faulty_queries, notes = _apply_strategy(
        mutated,
        fault_id=fault_id,
        metric_date=metric_date,
        spec=spec,
        rng=rng,
        start_date=start_date,
        days=days,
    )
    mutated["daily_metrics"] = materialize_daily_metrics(
        mutated,
        start_date=start_date,
        days=days,
        query_overrides=faulty_queries,
    )
    fault_value = _metric_value(mutated, metric_date, affected_metric)
    result = FaultInjectionResult(
        fault_id=fault_id,
        metric_date=metric_date,
        tables=mutated,
        expected_direction=expected_direction,
        faulty_queries=faulty_queries,
        notes=notes,
        case_id=case_id,
        affected_metric=affected_metric,
        effect_size_type=effect_size_type,
        minimum_effect_size=minimum_effect_size,
        actual_effect=calculate_effect(
            baseline_value, fault_value, effect_size_type=effect_size_type
        ),
    )
    validate_dataset_consistency(mutated, expected_days=days)
    return result


def inject_case(
    tables: dict[str, pd.DataFrame],
    case: GroundTruthCase,
    *,
    rng: np.random.Generator,
    start_date: pd.Timestamp,
    days: int,
) -> FaultInjectionResult:
    """Inject a concrete Ground Truth case into a materialized fault dataset."""
    metric_date = case.injection.metric_date or start_date.date()
    return _inject(
        tables,
        fault_id=case.fault_id,
        metric_date=metric_date,
        spec=case.injection,
        expected_direction=case.expected_direction,
        affected_metric=case.affected_metric,
        effect_size_type=case.effect_size_type,
        minimum_effect_size=case.minimum_effect_size,
        rng=rng,
        start_date=start_date,
        days=days,
        case_id=case.case_id,
    )


def inject_fault(
    tables: dict[str, pd.DataFrame],
    fault_id: str,
    metric_date: date,
    *,
    rng: np.random.Generator | None = None,
    start_date: pd.Timestamp | None = None,
    days: int | None = None,
) -> FaultInjectionResult:
    """Compatibility wrapper; benchmark callers should use :func:`inject_case`."""
    random = rng or np.random.default_rng(42)
    inferred_start, inferred_days = _infer_window(tables, metric_date)
    active_start = start_date if start_date is not None else inferred_start
    active_days = days if days is not None else inferred_days
    catalog = load_fault_catalog()
    matching_cases = [
        case
        for case in load_ground_truth_cases(_GROUND_TRUTH_DIRECTORY, catalog)
        if case.fault_id == fault_id
    ]
    if not matching_cases:
        raise ValueError(f"no ground-truth case exists for {fault_id}")
    concrete = matching_cases[0]
    spec = concrete.injection.model_copy(update={"metric_date": metric_date})
    return _inject(
        tables,
        fault_id=fault_id,
        metric_date=metric_date,
        spec=spec,
        expected_direction=concrete.expected_direction,
        affected_metric=concrete.affected_metric,
        effect_size_type=concrete.effect_size_type,
        minimum_effect_size=concrete.minimum_effect_size,
        rng=random,
        start_date=active_start,
        days=active_days,
        case_id=concrete.case_id,
    )
