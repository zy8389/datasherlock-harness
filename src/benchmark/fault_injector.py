from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from data.generator import generate_subscriptions


@dataclass
class FaultInjectionResult:
    """Non-destructive fault mutation plus the evidence needed to evaluate it."""

    fault_id: str
    metric_date: date
    tables: dict[str, pd.DataFrame]
    expected_direction: str
    faulty_queries: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _copy_tables(tables: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    return {name: frame.copy(deep=True) for name, frame in tables.items()}


def _day_mask(events: pd.DataFrame, metric_date: date) -> pd.Series:
    return events["event_time"].dt.date == metric_date


def _update_metric_version(
    tables: dict[str, pd.DataFrame], metric_id: str, query: str
) -> None:
    versions = tables["metric_versions"]
    row = versions["metric_id"] == metric_id
    versions.loc[row, "version"] = versions.loc[row, "version"].astype(int) + 1
    versions.loc[row, "query"] = query
    versions.loc[row, "definition_hash"] = pd.util.hash_pandas_object(
        pd.Series([query]), index=False
    ).astype(str).iloc[0]


def _infer_window(tables: dict[str, pd.DataFrame], metric_date: date) -> tuple[pd.Timestamp, int]:
    events = tables["events"]
    start = pd.Timestamp(events["event_time"].min()).normalize()
    end = pd.Timestamp(events["event_time"].max()).normalize()
    return start, max(1, int((end - start).days + 1))


def inject_fault(
    tables: dict[str, pd.DataFrame],
    fault_id: str,
    metric_date: date,
    *,
    rng: np.random.Generator | None = None,
    start_date: pd.Timestamp | None = None,
    days: int | None = None,
) -> FaultInjectionResult:
    """Inject one canonical fault without mutating the healthy input tables."""
    random = rng or np.random.default_rng(42)
    mutated = _copy_tables(tables)
    events = mutated["events"]
    mask = _day_mask(events, metric_date)
    expected_direction = "decrease"
    faulty_queries: dict[str, str] = {}
    notes: list[str] = []

    if fault_id == "F01":
        target = mask & events["device_type"].eq("android")
        mutated["events"] = events.loc[~target].reset_index(drop=True)
        partitions = mutated["partition_metadata"]
        target_partition = partitions["partition_value"].eq(f"{metric_date}/android")
        partitions.loc[target_partition, ["row_count", "status"]] = [0, "missing"]
        pipeline = mutated["pipeline_runs"]
        pipeline.loc[
            pipeline["target_partition"].eq(str(metric_date))
            & pipeline["target_table"].eq("events"),
            ["status", "error_type", "error_message"],
        ] = ["failed", "missing_partition", "android partition was not written"]
        notes.append("android events on the target partition were removed completely")
    elif fault_id == "F02":
        candidates = events.loc[mask & events["event_name"].eq("run_ai_task")]
        if candidates.empty:
            candidates = events.loc[mask]
        duplicated = candidates.sample(
            n=max(1, int(len(candidates) * 0.4)), random_state=int(random.integers(0, 2**31))
        )
        mutated["events"] = pd.concat([events, duplicated], ignore_index=True)
        expected_direction = "increase"
        notes.append("duplicated rows retain event_id, batch_id, user_id, and event_name")
    elif fault_id == "F03":
        mobile = events["device_type"].isin(["ios", "android"])
        target = mask & mobile
        selected = events.loc[target].sample(
            frac=0.25, random_state=int(random.integers(0, 2**31))
        )
        mutated["events"].loc[selected.index, "user_id"] = pd.NA
        notes.append("25% of target-date mobile user_id values are null")
    elif fault_id == "F04":
        target = mask & events["device_type"].eq("android")
        mutated["events"].loc[target, "event_time"] += pd.Timedelta(days=1)
        partitions = mutated["partition_metadata"]
        partitions.loc[
            partitions["partition_value"].eq(f"{metric_date}/android"),
            "status",
        ] = "delayed"
        notes.append("Android target-day events arrive on the following date")
    elif fault_id == "F05":
        users = mutated["users"]
        cn_users = users.loc[users["region"].eq("CN"), "user_id"]
        target = mask & events["user_id"].isin(cn_users)
        boundary = target & (
            events["event_time"].dt.hour.le(3) | events["event_time"].dt.hour.ge(20)
        )
        if not boundary.any():
            boundary = target
        mutated["events"].loc[boundary, "event_time"] += pd.Timedelta(hours=8)
        notes.append("CN boundary events are shifted by UTC+8")
    elif fault_id == "F06":
        selected = events.loc[mask].sample(
            frac=0.20, random_state=int(random.integers(0, 2**31))
        )
        mutated["events"].loc[selected.index, "duration_seconds"] *= 1000
        expected_direction = "increase"
        notes.append("20% of target events contain millisecond values in seconds")
    elif fault_id == "F07":
        faulty_queries["daily_active_users"] = """
            SELECT CAST(e.event_time AS DATE) AS metric_date,
                   COUNT(DISTINCT e.user_id) AS daily_active_users
            FROM events e
            INNER JOIN subscriptions s ON e.user_id = s.user_id
            GROUP BY metric_date
        """
        _update_metric_version(mutated, "daily_active_users", faulty_queries["daily_active_users"])
        notes.append("the metric SQL introduces an erroneous subscription inner join")
    elif fault_id == "F08":
        assignments = mutated["experiment_assignments"]
        if not assignments.empty:
            duplicate_users = assignments.sample(
                frac=0.5, random_state=int(random.integers(0, 2**31))
            )
            mutated["experiment_assignments"] = pd.concat(
                [assignments, duplicate_users], ignore_index=True
            )
        faulty_queries["ai_task_count"] = """
            SELECT CAST(e.event_time AS DATE) AS metric_date,
                   COUNT(e.event_id) AS ai_task_count
            FROM events e
            INNER JOIN experiment_assignments a ON e.user_id = a.user_id
            WHERE e.event_name = 'run_ai_task'
            GROUP BY metric_date
        """
        _update_metric_version(mutated, "ai_task_count", faulty_queries["ai_task_count"])
        expected_direction = "increase"
        notes.append("duplicate assignments are joined into task-event counting")
    elif fault_id == "F09":
        target = mask & events["event_name"].eq("run_ai_task")
        mutated["events"].loc[target, "event_name"] = "execute_ai_task"
        notes.append("new event name is not recognized by the existing metric filter")
    elif fault_id == "F10":
        target = mask
        mutated["events"] = mutated["events"].loc[~target].reset_index(drop=True)
        snapshots = mutated["schema_snapshots"]
        row = snapshots["table_name"].eq("events")
        snapshots.loc[row, "version"] = 2
        snapshots.loc[row, "schema_json"] = snapshots.loc[row, "schema_json"].str.replace(
            '"app_build_number": "int64"', '"app_build_number": "object"'
        )
        pipeline = mutated["pipeline_runs"]
        pipeline.loc[
            pipeline["target_partition"].eq(str(metric_date))
            & pipeline["target_table"].eq("events"),
            ["status", "error_type", "error_message"],
        ] = ["failed", "schema_change", "app_build_number type mismatch"]
        partitions = mutated["partition_metadata"]
        partitions.loc[
            partitions["partition_value"].str.startswith(f"{metric_date}/"), "status"
        ] = "failed"
        notes.append("target partition carries VARCHAR app_build_number values")
    elif fault_id == "F11":
        faulty_queries["daily_active_users"] = """
            SELECT CAST(event_time AS DATE) AS metric_date,
                   COUNT(DISTINCT user_id) AS daily_active_users
            FROM events
            WHERE event_name = 'run_ai_task'
            GROUP BY metric_date
        """
        _update_metric_version(mutated, "daily_active_users", faulty_queries["daily_active_users"])
        notes.append("the active-user definition is narrowed to core task events")
    elif fault_id == "F12":
        assignments = mutated["experiment_assignments"].copy()
        if not assignments.empty:
            assignments["variant"] = "control"
            treatment_count = round(len(assignments) * 0.8)
            treatment_indices = random.choice(
                assignments.index.to_numpy(), size=treatment_count, replace=False
            )
            assignments.loc[treatment_indices, "variant"] = "treatment"
            mutated["experiment_assignments"] = assignments
            if start_date is None or days is None:
                inferred_start, inferred_days = _infer_window(mutated, metric_date)
                start_date = inferred_start if start_date is None else start_date
                days = inferred_days if days is None else days
            mutated["subscriptions"] = generate_subscriptions(
                mutated["users"], start_date, days, random, assignments
            )
        configs = mutated["experiment_configs"]
        configs.loc[configs["experiment_id"].eq("exp_onboarding_v1"), [
            "version", "control_ratio", "treatment_ratio"
        ]] = [2, 0.2, 0.8]
        expected_direction = "increase"
        notes.append("subscription generation is rerun from the anomalous 20/80 allocation")
    else:
        raise ValueError(f"unknown fault id: {fault_id}")

    return FaultInjectionResult(
        fault_id=fault_id,
        metric_date=metric_date,
        tables=mutated,
        expected_direction=expected_direction,
        faulty_queries=faulty_queries,
        notes=notes,
    )
