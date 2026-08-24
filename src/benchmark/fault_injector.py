from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from benchmark.evaluation import calculate_effect, validate_effect
from config.faults import (
    INDEPENDENT_METADATA_EVIDENCE_FAULT_IDS,
    EvidenceSourceType,
    GroundTruthCase,
    InjectionSpec,
    load_fault_catalog,
    load_ground_truth_cases,
    validate_ground_truth_case,
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
_F12_FREE_TREATMENT_CONVERSION_PROBABILITY = 0.20


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
    actual_effect: float | None = None
    ground_truth_case: GroundTruthCase | None = None


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
    *,
    timezone: str | None = None,
    date_grain: str | None = None,
) -> None:
    versions = tables["metric_versions"]
    existing = versions.loc[versions["metric_id"].eq(metric_id)]
    next_version = int(existing["version"].max()) + 1 if not existing.empty else 1
    previous = existing.iloc[-1] if not existing.empty else None
    inherited_timezone = (
        str(previous["timezone"])
        if previous is not None
        and "timezone" in existing
        and pd.notna(previous["timezone"])
        else "UTC"
    )
    inherited_date_grain = (
        str(previous["date_grain"])
        if previous is not None
        and "date_grain" in existing
        and pd.notna(previous["date_grain"])
        else "day"
    )
    row = pd.DataFrame(
        [
            {
                "metric_id": metric_id,
                "version": next_version,
                "definition_hash": compute_definition_hash(query),
                "query": query,
                "effective_at": pd.Timestamp(effective_at),
                "timezone": timezone or inherited_timezone,
                "date_grain": date_grain or inherited_date_grain,
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


def _append_f12_free_treatment_subscriptions(
    tables: dict[str, pd.DataFrame],
    *,
    user_ids: list[int],
    users: pd.DataFrame,
    metric_date: date,
    days: int,
) -> None:
    """Materialize F12's treatment-only onboarding conversions from latent data."""
    if not user_ids:
        return

    subscriptions = tables["subscriptions"]
    next_subscription_id = (
        int(subscriptions["subscription_id"].max()) + 1
        if not subscriptions.empty
        else 1
    )
    fee_map = {"basic": 19.0, "pro": 49.0, "enterprise": 199.0}
    rows: list[dict[str, object]] = []
    start_time = pd.Timestamp(metric_date)
    cancellation_window = max(1, min(30, days))
    for offset, user_id in enumerate(sorted(user_ids)):
        row = users.loc[user_id]
        plan_score = float(row.subscription_plan_score)
        plan_type = (
            "basic"
            if plan_score < 0.55
            else "pro"
            if plan_score < 0.90
            else "enterprise"
        )
        cancelled = float(row.subscription_cancel_score) < 0.15
        end_time = (
            start_time
            + pd.Timedelta(
                days=1
                + int(
                    np.floor(
                        float(row.subscription_cancel_score) * cancellation_window
                    )
                )
            )
            if cancelled
            else pd.NaT
        )
        rows.append(
            {
                "subscription_id": next_subscription_id + offset,
                "user_id": user_id,
                "plan_type": plan_type,
                "start_time": start_time,
                "end_time": end_time,
                "subscription_status": "cancelled" if cancelled else "active",
                "monthly_fee": fee_map[plan_type],
            }
        )
    tables["subscriptions"] = pd.concat(
        [subscriptions, pd.DataFrame(rows, columns=subscriptions.columns)],
        ignore_index=True,
    )


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
        target_timezone = spec.to_value or "Asia/Shanghai"
        region_users = tables["users"].loc[
            tables["users"]["region"].eq(region), "user_id"
        ]
        target = mask & events["user_id"].isin(region_users)
        boundary = target & (
            events["event_time"].dt.hour.le(3) | events["event_time"].dt.hour.ge(20)
        )
        selected = boundary if boundary.any() else target
        tables["events"].loc[selected, "event_time"] += pd.Timedelta(hours=shift_hours)
        _, current_metric_version = _version_rows(
            tables["metric_versions"], "daily_active_users"
        )
        _append_metric_version(
            tables,
            "daily_active_users",
            str(current_metric_version["query"]),
            metric_date,
            timezone=target_timezone,
        )
        _mark_pipeline(
            tables,
            metric_date=metric_date,
            status="warning",
            error_type="timezone_error",
            error_message=f"{region} boundary events shifted by {shift_hours} hours",
        )
        notes.append(
            f"{region} boundary events are shifted by {shift_hours} hours and the "
            f"metric timezone changes to {target_timezone}"
        )
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
        assignment_identity = assignments.drop(columns=["variant"]).copy(deep=True)
        original_variants = assignments.set_index("user_id")["variant"]
        treatment_count = round(len(assignments) * treatment_ratio)
        users = tables["users"].set_index("user_id")
        preserved_treatment = assignments.loc[
            assignments["variant"].eq("treatment"), "user_id"
        ].astype(int).tolist()
        if treatment_count < len(preserved_treatment):
            raise ValueError(
                "F12 target allocation cannot remove existing treatment exposure"
            )

        # Select users whose fixed latent score makes the allocation change causal
        # for the target date: control would not convert, treatment would convert.
        latest_event_dates = (
            events.assign(metric_day=events["event_time"].dt.date)
            .groupby("user_id")["metric_day"]
            .max()
        )
        active_users = set(events.loc[mask, "user_id"].dropna().astype(int))
        preferred: list[int] = []
        for row in users.loc[users.index.intersection(original_variants.index)].itertuples():
            user_id = int(row.Index)
            if (
                original_variants.loc[user_id] != "control"
                or user_id not in active_users
            ):
                continue
            if row.user_type == "free":
                if row.conversion_score < _F12_FREE_TREATMENT_CONVERSION_PROBABILITY:
                    preferred.append(user_id)
                continue
            if (
                row.user_type not in {"trial", "paid"}
                or latest_event_dates.get(user_id) != metric_date
            ):
                continue
            base_probability = 0.72 if row.user_type == "paid" else 0.32
            control_probability = min(base_probability + 0.02, 0.95)
            treatment_probability = min(base_probability + 0.20, 0.95)
            if control_probability <= row.conversion_score < treatment_probability:
                preferred.append(user_id)

        # A stable score/user-id order makes the causal selection independent of
        # dataframe order and Python hash randomization.
        preferred.sort(
            key=lambda user_id: (
                users.loc[user_id, "user_type"] == "free",
                -float(users.loc[user_id, "conversion_score"]),
                user_id,
            )
        )
        cohort_user_ids = set(assignments["user_id"])
        treatment_users = set(preserved_treatment)
        treatment_users.update(
            preferred[: treatment_count - len(preserved_treatment)]
        )
        remaining = assignments.loc[
            ~assignments["user_id"].isin(treatment_users), "user_id"
        ].to_numpy()
        fill_count = treatment_count - len(treatment_users)
        if fill_count:
            filler = rng.choice(remaining, size=fill_count, replace=False)
            treatment_users.update(int(user_id) for user_id in filler)
        assignments["variant"] = "control"
        treatment_indices = assignments.index[
            assignments["user_id"].isin(treatment_users)
        ]
        assignments.loc[treatment_indices, "variant"] = "treatment"
        if set(assignments["user_id"]) != cohort_user_ids or not assignments.drop(
            columns=["variant"]
        ).equals(assignment_identity):
            raise RuntimeError(
                "F12 injection must preserve assignment identity and change only variants"
            )
        tables["experiment_assignments"] = assignments
        # Recompute downstream subscription outcomes from the changed variants;
        # the experiment cohort and all assignment identity fields remain fixed.
        tables["subscriptions"] = generate_subscriptions(
            tables["users"], start_date, days, rng, assignments, tables["events"]
        )
        free_treatment_users = [
            user_id
            for user_id in preferred
            if user_id in treatment_users
            and users.loc[user_id, "user_type"] == "free"
        ]
        _append_f12_free_treatment_subscriptions(
            tables,
            user_ids=free_treatment_users,
            users=users,
            metric_date=metric_date,
            days=days,
        )
        _append_experiment_config(
            tables,
            control_ratio=control_ratio,
            treatment_ratio=treatment_ratio,
            effective_at=metric_date,
        )
        notes.append(
            "experiment allocation changed to the requested split while preserving "
            "the experiment cohort and prioritizing target-date treatment uplift users"
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
        "metric_versions": {
            "metric_id",
            "version",
            "definition_hash",
            "query",
            "effective_at",
            "timezone",
            "date_grain",
        },
        "experiment_configs": {"experiment_id", "version", "effective_at"},
    }
    for table_name, columns in required_columns.items():
        missing_columns = columns.difference(tables[table_name].columns)
        if missing_columns:
            raise ValueError(f"{table_name} missing columns: {sorted(missing_columns)}")


def _target_date(result: FaultInjectionResult, case: GroundTruthCase) -> date:
    return case.injection.metric_date or result.metric_date


def _target_events(
    tables: dict[str, pd.DataFrame], target_date: date, device_type: str | None = None
) -> pd.DataFrame:
    events = tables["events"]
    mask = events["event_time"].dt.date.eq(target_date)
    if device_type is not None:
        mask &= events["device_type"].eq(device_type)
    return events.loc[mask]


def _single_row(frame: pd.DataFrame, description: str) -> pd.Series:
    if len(frame) != 1:
        raise ValueError(f"expected one {description} row, found {len(frame)}")
    return frame.iloc[0]


def _same_date(value: object, target_date: date) -> bool:
    if value is None or pd.isna(value):
        return False
    return pd.Timestamp(value).date() == target_date


def _validate_business_evidence(
    result: FaultInjectionResult,
    baseline_tables: dict[str, pd.DataFrame],
    case: GroundTruthCase,
    asset: str,
) -> None:
    target_date = _target_date(result, case)
    spec = case.injection

    if asset not in result.tables or asset not in baseline_tables:
        raise ValueError(f"{case.case_id} business evidence asset is unavailable: {asset}")

    if case.fault_id in {"F01", "F04"} and asset == "events":
        device_type = spec.device_type or "android"
        baseline_events = _target_events(baseline_tables, target_date, device_type)
        fault_events = _target_events(result.tables, target_date, device_type)
        if len(baseline_events) == 0 or len(fault_events) >= len(baseline_events):
            raise ValueError(
                f"{case.case_id} target-date {device_type} business events did not decrease"
            )
        if case.fault_id == "F04":
            next_date = target_date + timedelta(days=1)
            baseline_next = _target_events(baseline_tables, next_date, device_type)
            fault_next = _target_events(result.tables, next_date, device_type)
            if len(fault_next) <= len(baseline_next):
                raise ValueError(
                    f"{case.case_id} delayed events did not rebound on the following date"
                )
        return

    if case.fault_id == "F05" and asset == "events":
        region = spec.region or "CN"
        region_users = set(
            result.tables["users"].loc[
                result.tables["users"]["region"].eq(region), "user_id"
            ]
        )
        baseline_events = _target_events(baseline_tables, target_date)
        fault_events = _target_events(result.tables, target_date)
        baseline_hours = (
            baseline_events.loc[baseline_events["user_id"].isin(region_users), "event_time"]
            .dt.hour.value_counts()
            .sort_index()
        )
        fault_hours = (
            fault_events.loc[fault_events["user_id"].isin(region_users), "event_time"]
            .dt.hour.value_counts()
            .sort_index()
        )
        if baseline_hours.empty or baseline_hours.equals(fault_hours):
            raise ValueError(f"{case.case_id} regional hourly business evidence did not change")
        return

    if case.fault_id == "F10" and asset in {"events", "daily_metrics"}:
        if asset == "events":
            baseline_count = len(_target_events(baseline_tables, target_date))
            fault_count = len(_target_events(result.tables, target_date))
            if baseline_count == 0 or fault_count >= baseline_count:
                raise ValueError(f"{case.case_id} target-day business events did not decrease")
        else:
            baseline_value = _metric_value(
                baseline_tables, target_date, case.affected_metric
            )
            fault_value = _metric_value(result.tables, target_date, case.affected_metric)
            if fault_value >= baseline_value:
                raise ValueError(f"{case.case_id} target-day metric did not decrease")
        return

    if case.fault_id == "F11" and asset == "events":
        baseline_count = len(_target_events(baseline_tables, target_date))
        fault_count = len(_target_events(result.tables, target_date))
        if fault_count != baseline_count:
            raise ValueError(f"{case.case_id} raw target-day event count is not stable")
        return

    if case.fault_id == "F12" and asset == "experiment_assignments":
        baseline_distribution = (
            baseline_tables[asset]["variant"].value_counts(normalize=True).sort_index()
        )
        fault_distribution = (
            result.tables[asset]["variant"].value_counts(normalize=True).sort_index()
        )
        if baseline_distribution.equals(fault_distribution):
            raise ValueError(f"{case.case_id} experiment assignment distribution did not change")
        return

    if case.fault_id == "F12" and asset == "subscriptions":
        if result.tables[asset].equals(baseline_tables[asset]):
            raise ValueError(f"{case.case_id} subscription outcome evidence did not change")
        return

    raise ValueError(
        f"{case.case_id} has unsupported business evidence contract: {asset}"
    )


def _version_rows(frame: pd.DataFrame, metric_id: str) -> tuple[pd.Series, pd.Series]:
    metric_rows = frame.loc[frame["metric_id"].eq(metric_id)].sort_values(
        ["version", "effective_at"]
    )
    if metric_rows.empty:
        raise ValueError(f"metric_versions has no row for {metric_id}")
    return metric_rows.iloc[0], metric_rows.iloc[-1]


def _validate_metric_version_evidence(
    result: FaultInjectionResult,
    baseline_tables: dict[str, pd.DataFrame],
    case: GroundTruthCase,
) -> None:
    target_date = _target_date(result, case)
    metric_id = case.affected_metric
    _, baseline_latest = _version_rows(baseline_tables["metric_versions"], metric_id)
    fault_rows = result.tables["metric_versions"].loc[
        result.tables["metric_versions"]["metric_id"].eq(metric_id)
    ]
    fault_candidates = fault_rows.loc[
        fault_rows["version"].astype(int).gt(int(baseline_latest["version"]))
        & fault_rows["effective_at"].map(lambda value: _same_date(value, target_date))
    ]
    fault_version = _single_row(fault_candidates, f"{metric_id} fault metric version")

    if case.fault_id == "F05":
        expected_timezone = case.injection.to_value
        if baseline_latest["timezone"] != "UTC":
            raise ValueError(f"{case.case_id} baseline timezone must be UTC")
        if not expected_timezone:
            raise ValueError(f"{case.case_id} injection.to_value must declare a timezone")
        if fault_version["timezone"] != expected_timezone:
            raise ValueError(
                f"{case.case_id} fault timezone must be {expected_timezone}"
            )
        if fault_version["query"] != baseline_latest["query"]:
            raise ValueError(f"{case.case_id} F05 metric query must remain unchanged")
        if fault_version["definition_hash"] != baseline_latest["definition_hash"]:
            raise ValueError(
                f"{case.case_id} F05 metric definition hash must remain unchanged"
            )
        return

    if case.fault_id == "F11":
        if fault_version["definition_hash"] == baseline_latest["definition_hash"]:
            raise ValueError(f"{case.case_id} metric definition hash did not change")
        if fault_version["query"] == baseline_latest["query"]:
            raise ValueError(f"{case.case_id} metric query did not change")
        return

    raise ValueError(f"{case.case_id} has unsupported metric version evidence")


def _validate_independent_evidence(
    result: FaultInjectionResult,
    baseline_tables: dict[str, pd.DataFrame],
    case: GroundTruthCase,
    source_type: EvidenceSourceType,
    asset: str,
) -> None:
    target_date = _target_date(result, case)

    if source_type == EvidenceSourceType.OPERATIONAL_METADATA and asset == "partition_metadata":
        if case.fault_id != "F01":
            raise ValueError(f"{case.case_id} has unsupported partition evidence")
        device_type = case.injection.device_type or "android"
        partition_value = f"{target_date}/{device_type}"
        rows = result.tables[asset].loc[
            result.tables[asset]["table_name"].eq("events")
            & result.tables[asset]["partition_value"].eq(partition_value)
        ]
        row = _single_row(rows, f"{partition_value} partition metadata")
        if int(row["row_count"]) != 0 and row["status"] not in {"missing", "stale"}:
            raise ValueError(f"{case.case_id} target partition is not missing or empty")
        return

    if source_type == EvidenceSourceType.OPERATIONAL_METADATA and asset == "pipeline_runs":
        if case.fault_id != "F04":
            raise ValueError(f"{case.case_id} has unsupported pipeline evidence")
        rows = result.tables[asset].loc[
            result.tables[asset]["target_table"].eq("events")
            & result.tables[asset]["target_partition"].eq(str(target_date))
        ]
        row = _single_row(rows, f"{target_date} events pipeline run")
        status = str(row["status"]).lower()
        error_type = str(row["error_type"]).lower()
        if status not in {"delayed", "late", "stale"} and error_type != "data_delay":
            raise ValueError(f"{case.case_id} target pipeline is not marked delayed")
        return

    if source_type == EvidenceSourceType.METRIC_VERSION and asset == "metric_versions":
        _validate_metric_version_evidence(result, baseline_tables, case)
        return

    if source_type == EvidenceSourceType.SCHEMA_METADATA and asset == "schema_snapshots":
        if case.fault_id != "F10":
            raise ValueError(f"{case.case_id} has unsupported schema evidence")
        baseline_rows = baseline_tables[asset].loc[
            baseline_tables[asset]["table_name"].eq("events")
        ].sort_values(["version", "effective_at"])
        fault_rows = result.tables[asset].loc[
            result.tables[asset]["table_name"].eq("events")
        ].sort_values(["version", "effective_at"])
        baseline_snapshot = _single_row(baseline_rows.tail(1), "baseline events schema")
        fault_candidates = fault_rows.loc[
            fault_rows["version"].astype(int).gt(int(baseline_snapshot["version"]))
            & fault_rows["effective_at"].map(lambda value: _same_date(value, target_date))
        ]
        fault_snapshot = _single_row(fault_candidates, "fault events schema")
        baseline_schema = json.loads(str(baseline_snapshot["schema_json"]))
        fault_schema = json.loads(str(fault_snapshot["schema_json"]))
        if baseline_schema.get("app_build_number") != "BIGINT":
            raise ValueError(f"{case.case_id} baseline app_build_number type is unexpected")
        if fault_schema.get("app_build_number") != "VARCHAR":
            raise ValueError(f"{case.case_id} fault app_build_number type is unexpected")
        return

    if source_type == EvidenceSourceType.EXPERIMENT_CONFIG and asset == "experiment_configs":
        if case.fault_id != "F12":
            raise ValueError(f"{case.case_id} has unsupported experiment evidence")
        experiment_id = str(baseline_tables[asset].iloc[0]["experiment_id"])
        baseline_rows = baseline_tables[asset].loc[
            baseline_tables[asset]["experiment_id"].eq(experiment_id)
        ].sort_values(["version", "effective_at"])
        fault_rows = result.tables[asset].loc[
            result.tables[asset]["experiment_id"].eq(experiment_id)
        ].sort_values(["version", "effective_at"])
        baseline_config = _single_row(baseline_rows.tail(1), "baseline experiment config")
        fault_candidates = fault_rows.loc[
            fault_rows["version"].astype(int).gt(int(baseline_config["version"]))
            & fault_rows["effective_at"].map(lambda value: _same_date(value, target_date))
        ]
        fault_config = _single_row(fault_candidates, "fault experiment config")
        control_ratio = case.injection.control_ratio
        treatment_ratio = case.injection.treatment_ratio
        if control_ratio is None or treatment_ratio is None:
            raise ValueError(f"{case.case_id} has no expected experiment allocation")
        if not np.isclose(fault_config["control_ratio"], control_ratio):
            raise ValueError(f"{case.case_id} control allocation is unexpected")
        if not np.isclose(fault_config["treatment_ratio"], treatment_ratio):
            raise ValueError(f"{case.case_id} treatment allocation is unexpected")
        if (
            baseline_config["control_ratio"] == fault_config["control_ratio"]
            and baseline_config["treatment_ratio"] == fault_config["treatment_ratio"]
        ):
            raise ValueError(f"{case.case_id} experiment allocation did not change")
        return

    raise ValueError(
        f"{case.case_id} has unsupported independent evidence contract: "
        f"{source_type.value}:{asset}"
    )


def _resolve_ground_truth_case(
    result: FaultInjectionResult, case: GroundTruthCase | None
) -> GroundTruthCase:
    resolved = case or result.ground_truth_case
    if resolved is None and result.case_id is not None:
        resolved = next(
            (
                candidate
                for candidate in load_ground_truth_cases(_GROUND_TRUTH_DIRECTORY)
                if candidate.case_id == result.case_id
            ),
            None,
        )
    if resolved is None:
        raise ValueError("validate_expected_evidence requires a Ground Truth case")
    if resolved.fault_id != result.fault_id:
        raise ValueError(
            f"Ground Truth fault {resolved.fault_id} does not match result {result.fault_id}"
        )
    return validate_ground_truth_case(resolved)


def _validate_legacy_expected_evidence(
    result: FaultInjectionResult,
    baseline_tables: dict[str, pd.DataFrame] | None,
) -> None:
    tables = result.tables
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
    }
    if not evidence_checks[result.fault_id]:
        raise ValueError(f"{result.fault_id} data evidence is missing")


def validate_expected_evidence(
    result: FaultInjectionResult,
    baseline_tables: dict[str, pd.DataFrame] | None = None,
    ground_truth_case: GroundTruthCase | None = None,
) -> None:
    """Verify metric and evidence-path observations for one Ground Truth case."""
    tables = result.tables
    validate_dataset_consistency(tables, expected_days=len(tables["daily_metrics"]))
    case = _resolve_ground_truth_case(result, ground_truth_case)
    if baseline_tables is not None:
        target_date = _target_date(result, case)
        baseline_value = _metric_value(
            baseline_tables, target_date, case.affected_metric
        )
        fault_value = _metric_value(tables, target_date, case.affected_metric)
        if not validate_effect(
            baseline_value,
            fault_value,
            expected_direction=case.expected_direction,
            effect_size_type=case.effect_size_type,
            minimum_effect_size=case.minimum_effect_size,
        ):
            raise ValueError(
                f"{case.case_id} business metric does not satisfy the effect contract"
            )
    if case.fault_id in INDEPENDENT_METADATA_EVIDENCE_FAULT_IDS:
        if baseline_tables is None:
            raise ValueError(
                f"{case.case_id} evidence validation requires baseline tables"
            )
        for path in case.evidence_paths:
            if path.asset not in tables:
                raise ValueError(
                    f"{case.case_id} evidence asset is unavailable: {path.asset}"
                )
            if path.source_type == EvidenceSourceType.BUSINESS_DATA:
                _validate_business_evidence(result, baseline_tables, case, path.asset)
            else:
                _validate_independent_evidence(
                    result,
                    baseline_tables,
                    case,
                    path.source_type,
                    path.asset,
                )
        return
    _validate_legacy_expected_evidence(result, baseline_tables)


def _inject(
    tables: dict[str, pd.DataFrame],
    *,
    fault_id: str,
    metric_date: date,
    spec: InjectionSpec,
    expected_direction: Literal["increase", "decrease"],
    affected_metric: str,
    effect_size_type: Literal["relative", "absolute"],
    rng: np.random.Generator,
    start_date: pd.Timestamp,
    days: int,
    case_id: str | None,
    ground_truth_case: GroundTruthCase | None = None,
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
        actual_effect=calculate_effect(
            baseline_value, fault_value, effect_size_type=effect_size_type
        ),
        ground_truth_case=ground_truth_case,
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
    validate_ground_truth_case(case)
    metric_date = case.injection.metric_date or start_date.date()
    return _inject(
        tables,
        fault_id=case.fault_id,
        metric_date=metric_date,
        spec=case.injection,
        expected_direction=case.expected_direction,
        affected_metric=case.affected_metric,
        effect_size_type=case.effect_size_type,
        rng=rng,
        start_date=start_date,
        days=days,
        case_id=case.case_id,
        ground_truth_case=case,
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
    bound_case = concrete.model_copy(update={"injection": spec})
    return _inject(
        tables,
        fault_id=fault_id,
        metric_date=metric_date,
        spec=spec,
        expected_direction=concrete.expected_direction,
        affected_metric=concrete.affected_metric,
        effect_size_type=concrete.effect_size_type,
        rng=random,
        start_date=active_start,
        days=active_days,
        case_id=bound_case.case_id,
        ground_truth_case=bound_case,
    )
