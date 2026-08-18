from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from benchmark.fault_injector import FaultInjectionResult, inject_fault
from config.faults import load_fault_catalog, load_ground_truth_cases
from data.generator import generate_daily_metrics, generate_dataset

TARGET_DATE = date(2026, 1, 16)
START_DATE = pd.Timestamp("2026-01-01")


@pytest.fixture(scope="module")
def baseline() -> dict[str, pd.DataFrame]:
    return generate_dataset(500, 30, 10_000, 42, START_DATE)


def _metrics(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    return generate_daily_metrics(
        tables["users"],
        tables["events"],
        tables["subscriptions"],
        START_DATE,
        30,
    ).set_index("metric_date")


def _run_query(tables: dict[str, pd.DataFrame], query: str) -> pd.DataFrame:
    with duckdb.connect(":memory:") as connection:
        for table_name in ("events", "subscriptions", "experiment_assignments"):
            if table_name in tables:
                connection.register(table_name, tables[table_name])
        return connection.execute(query).df()


def _inject(baseline: dict[str, pd.DataFrame], fault_id: str) -> FaultInjectionResult:
    return inject_fault(
        baseline,
        fault_id,
        TARGET_DATE,
        rng=np.random.default_rng(99),
        start_date=START_DATE,
        days=30,
    )


def test_fault_catalog_has_canonical_ids_and_machine_readable_cases() -> None:
    catalog = load_fault_catalog()
    cases = load_ground_truth_cases(Path("benchmark/ground_truth"), catalog)

    assert [fault.id for fault in catalog.faults] == [f"F{i:02d}" for i in range(1, 13)]
    assert {case.fault_id for case in cases} == {f"F{i:02d}" for i in range(1, 13)}
    assert all(len(fault.expected_evidence) >= 2 for fault in catalog.faults)


def test_f01_partition_missing_reduces_target_dau(
    baseline: dict[str, pd.DataFrame],
) -> None:
    result = _inject(baseline, "F01")
    assert (
        _metrics(result.tables).loc[TARGET_DATE, "daily_active_users"]
        < _metrics(baseline).loc[TARGET_DATE, "daily_active_users"]
    )
    assert (
        result.tables["partition_metadata"]
        .query("partition_value == '2026-01-16/android'")
        .iloc[0]["status"]
        == "missing"
    )


def test_f02_duplicate_batch_increases_task_rows(
    baseline: dict[str, pd.DataFrame],
) -> None:
    result = _inject(baseline, "F02")
    base_metrics = _metrics(baseline)
    fault_metrics = _metrics(result.tables)
    assert len(result.tables["events"]) > len(baseline["events"])
    assert (
        fault_metrics.loc[TARGET_DATE, "ai_task_count"]
        > base_metrics.loc[TARGET_DATE, "ai_task_count"]
    )


def test_f03_null_user_ids_reduce_valid_dau(baseline: dict[str, pd.DataFrame]) -> None:
    result = _inject(baseline, "F03")
    assert result.tables["events"]["user_id"].isna().sum() > 0
    assert (
        _metrics(result.tables).loc[TARGET_DATE, "daily_active_users"]
        < _metrics(baseline).loc[TARGET_DATE, "daily_active_users"]
    )


def test_f04_delay_moves_android_events_to_next_day(
    baseline: dict[str, pd.DataFrame],
) -> None:
    result = _inject(baseline, "F04")
    base_events = baseline["events"]
    fault_events = result.tables["events"]
    base_count = (
        (base_events["event_time"].dt.date == TARGET_DATE)
        & base_events["device_type"].eq("android")
    ).sum()
    fault_count = (
        (fault_events["event_time"].dt.date == TARGET_DATE)
        & fault_events["device_type"].eq("android")
    ).sum()
    assert fault_count < base_count
    assert (
        result.tables["partition_metadata"]
        .query("partition_value == '2026-01-16/android'")
        .iloc[0]["status"]
        == "delayed"
    )


def test_f05_timezone_shift_changes_target_day_distribution(
    baseline: dict[str, pd.DataFrame],
) -> None:
    result = _inject(baseline, "F05")
    assert not result.tables["events"].equals(baseline["events"])
    assert (
        _metrics(result.tables).loc[TARGET_DATE, "daily_active_users"]
        != _metrics(baseline).loc[TARGET_DATE, "daily_active_users"]
    )


def test_f06_unit_error_increases_average_duration(
    baseline: dict[str, pd.DataFrame],
) -> None:
    result = _inject(baseline, "F06")
    assert (
        _metrics(result.tables).loc[TARGET_DATE, "average_session_duration"]
        > _metrics(baseline).loc[TARGET_DATE, "average_session_duration"]
    )


def test_f07_join_filter_changes_dau_query_result(
    baseline: dict[str, pd.DataFrame],
) -> None:
    result = _inject(baseline, "F07")
    faulty = _run_query(result.tables, result.faulty_queries["daily_active_users"])
    observed = faulty.loc[
        faulty["metric_date"].dt.date.eq(TARGET_DATE), "daily_active_users"
    ].iloc[0]
    assert observed < _metrics(baseline).loc[TARGET_DATE, "daily_active_users"]


def test_f08_join_explosion_increases_joined_task_count(
    baseline: dict[str, pd.DataFrame],
) -> None:
    result = _inject(baseline, "F08")
    query = result.faulty_queries["ai_task_count"]
    base_result = _run_query(baseline, query)
    fault_result = _run_query(result.tables, query)
    base_value = base_result.loc[
        base_result["metric_date"].dt.date.eq(TARGET_DATE), "ai_task_count"
    ].iloc[0]
    fault_value = fault_result.loc[
        fault_result["metric_date"].dt.date.eq(TARGET_DATE), "ai_task_count"
    ].iloc[0]
    assert fault_value > base_value


def test_f09_field_drift_removes_known_task_events(
    baseline: dict[str, pd.DataFrame],
) -> None:
    result = _inject(baseline, "F09")
    assert "execute_ai_task" in set(result.tables["events"]["event_name"])
    assert (
        _metrics(result.tables).loc[TARGET_DATE, "ai_task_count"]
        < _metrics(baseline).loc[TARGET_DATE, "ai_task_count"]
    )


def test_f10_schema_change_marks_partition_failed_and_reduces_dau(
    baseline: dict[str, pd.DataFrame],
) -> None:
    result = _inject(baseline, "F10")
    assert (
        _metrics(result.tables).loc[TARGET_DATE, "daily_active_users"]
        < _metrics(baseline).loc[TARGET_DATE, "daily_active_users"]
    )
    assert (
        result.tables["pipeline_runs"]
        .query("target_table == 'events' and target_partition == '2026-01-16'")
        .iloc[0]["error_type"]
        == "schema_change"
    )


def test_f11_metric_definition_change_reduces_dau_query_result(
    baseline: dict[str, pd.DataFrame],
) -> None:
    result = _inject(baseline, "F11")
    faulty = _run_query(result.tables, result.faulty_queries["daily_active_users"])
    observed = faulty.loc[
        faulty["metric_date"].dt.date.eq(TARGET_DATE), "daily_active_users"
    ].iloc[0]
    assert observed < _metrics(baseline).loc[TARGET_DATE, "daily_active_users"]


def test_f12_ab_split_rebuild_changes_conversion_rate(
    baseline: dict[str, pd.DataFrame],
) -> None:
    result = _inject(baseline, "F12")
    assert result.tables["experiment_configs"].iloc[0]["control_ratio"] == 0.2
    assert (
        _metrics(result.tables).loc[TARGET_DATE, "conversion_rate"]
        != _metrics(baseline).loc[TARGET_DATE, "conversion_rate"]
    )
