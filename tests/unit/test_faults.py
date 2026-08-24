import inspect
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from benchmark import fault_injector
from benchmark.evaluation import validate_effect
from benchmark.fault_injector import (
    FaultInjectionResult,
    inject_case,
    inject_fault,
    validate_dataset_consistency,
    validate_expected_evidence,
)
from config.faults import load_fault_catalog, load_ground_truth_cases
from data.generator import generate_dataset

TARGET_DATE = date(2026, 1, 16)
START_DATE = pd.Timestamp("2026-01-01")
LATER_FAULT_IDS = ("F07", "F08", "F09", "F10", "F11", "F12")


@pytest.fixture(scope="module")
def baseline() -> dict[str, pd.DataFrame]:
    return generate_dataset(500, 30, 10_000, 42, START_DATE)


def _metrics(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    return tables["daily_metrics"].set_index("metric_date")


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
    base_next_count = (
        (base_events["event_time"].dt.date == TARGET_DATE + pd.Timedelta(days=1))
        & base_events["device_type"].eq("android")
    ).sum()
    fault_next_count = (
        (fault_events["event_time"].dt.date == TARGET_DATE + pd.Timedelta(days=1))
        & fault_events["device_type"].eq("android")
    ).sum()
    assert fault_next_count > base_next_count
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
    observed = _metrics(result.tables).loc[TARGET_DATE, "daily_active_users"]
    assert observed < _metrics(baseline).loc[TARGET_DATE, "daily_active_users"]


def test_f08_join_explosion_increases_joined_task_count(
    baseline: dict[str, pd.DataFrame],
) -> None:
    result = _inject(baseline, "F08")
    base_value = _metrics(baseline).loc[TARGET_DATE, "ai_task_count"]
    fault_value = _metrics(result.tables).loc[TARGET_DATE, "ai_task_count"]
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
    snapshots = result.tables["schema_snapshots"].query("table_name == 'events'")
    assert set(snapshots["version"]) == {1, 2}
    assert '"app_build_number": "BIGINT"' in snapshots.iloc[0]["schema_json"]
    assert '"app_build_number": "VARCHAR"' in snapshots.iloc[-1]["schema_json"]
    assert (
        result.tables["partition_metadata"]
        .query("partition_value == '2026-01-16/android'")
        .iloc[0]["status"]
        == "failed"
    )


def test_f11_metric_definition_change_reduces_dau_query_result(
    baseline: dict[str, pd.DataFrame],
) -> None:
    result = _inject(baseline, "F11")
    observed = _metrics(result.tables).loc[TARGET_DATE, "daily_active_users"]
    assert observed < _metrics(baseline).loc[TARGET_DATE, "daily_active_users"]
    base_count = (
        baseline["events"]["event_time"].dt.date == TARGET_DATE
    ).sum()
    fault_count = (
        result.tables["events"]["event_time"].dt.date == TARGET_DATE
    ).sum()
    assert fault_count == base_count
    versions = result.tables["metric_versions"].query(
        "metric_id == 'daily_active_users'"
    ).sort_values(["version", "effective_at"])
    assert versions.iloc[-1]["version"] > versions.iloc[-2]["version"]
    assert versions.iloc[-1]["definition_hash"] != versions.iloc[-2]["definition_hash"]
    assert versions.iloc[-1]["query"] != versions.iloc[-2]["query"]


def test_f12_ab_split_rebuild_changes_conversion_rate(
    baseline: dict[str, pd.DataFrame],
) -> None:
    case = next(
        case for case in load_ground_truth_cases(Path("benchmark/ground_truth"))
        if case.fault_id == "F12"
    )
    result = inject_case(
        baseline,
        case,
        rng=np.random.default_rng(99),
        start_date=START_DATE,
        days=30,
    )
    assignments = result.tables["experiment_assignments"]
    assert assignments["user_id"].is_unique
    assert baseline["experiment_assignments"]["variant"].value_counts(normalize=True).to_dict() == {
        "control": pytest.approx(0.50),
        "treatment": pytest.approx(0.50),
    }
    assert set(assignments["user_id"]) == set(
        baseline["experiment_assignments"]["user_id"]
    )
    assert_frame_equal(
        assignments.drop(columns=["variant"]),
        baseline["experiment_assignments"].drop(columns=["variant"]),
    )
    assert assignments["variant"].value_counts(normalize=True).to_dict() == {
        "treatment": pytest.approx(0.80),
        "control": pytest.approx(0.20),
    }
    assert result.tables["experiment_configs"].iloc[-1]["control_ratio"] == 0.2
    assert result.tables["experiment_configs"].iloc[0]["control_ratio"] == 0.5
    assert not result.tables["subscriptions"].equals(baseline["subscriptions"])


def _effect_contract_cases() -> list[object]:
    return [
        pytest.param(case, id=case.case_id)
        for case in load_ground_truth_cases(Path("benchmark/ground_truth"))
    ]


@pytest.mark.parametrize("case", _effect_contract_cases())
def test_fault_case_effect_contract(
    case, baseline: dict[str, pd.DataFrame]
) -> None:
    result = inject_case(
        baseline,
        case,
        rng=np.random.default_rng(99),
        start_date=START_DATE,
        days=30,
    )
    baseline_value = baseline["daily_metrics"].set_index("metric_date").loc[
        case.injection.metric_date, case.affected_metric
    ]
    fault_value = result.tables["daily_metrics"].set_index("metric_date").loc[
        case.injection.metric_date, case.affected_metric
    ]
    assert result.fault_id == case.fault_id
    assert validate_effect(
        baseline_value,
        fault_value,
        expected_direction=case.expected_direction,
        effect_size_type=case.effect_size_type,
        minimum_effect_size=case.minimum_effect_size,
    )
    validate_dataset_consistency(result.tables, expected_days=30)
    validate_expected_evidence(result, baseline)


def test_f12_injector_does_not_accept_or_store_minimum_effect_size() -> None:
    assert "minimum_effect_size" not in inspect.signature(
        fault_injector._apply_strategy
    ).parameters
    assert "minimum_effect_size" not in inspect.signature(fault_injector._inject).parameters
    assert "minimum_effect_size" not in FaultInjectionResult.__dataclass_fields__


def test_f04_and_f09_use_requested_ratios(
    baseline: dict[str, pd.DataFrame],
) -> None:
    f04 = _inject(baseline, "F04")
    base_android = baseline["events"].loc[
        (baseline["events"]["event_time"].dt.date == TARGET_DATE)
        & baseline["events"]["device_type"].eq("android")
    ]
    moved_android = len(base_android) - len(
        f04.tables["events"].loc[
            (f04.tables["events"]["event_time"].dt.date == TARGET_DATE)
            & f04.tables["events"]["device_type"].eq("android")
        ]
    )
    assert moved_android == round(len(base_android) * 0.60)

    f09 = _inject(baseline, "F09")
    base_tasks = baseline["events"].loc[
        (baseline["events"]["event_time"].dt.date == TARGET_DATE)
        & baseline["events"]["event_name"].eq("run_ai_task")
    ]
    renamed = f09.tables["events"]["event_name"].eq("execute_ai_task").sum()
    assert renamed == round(len(base_tasks) * 0.70)
    assert len(f09.tables["events"]) == len(baseline["events"])


def test_append_only_history_and_definition_hashes(
    baseline: dict[str, pd.DataFrame],
) -> None:
    f10 = _inject(baseline, "F10")
    event_versions = f10.tables["schema_snapshots"].query("table_name == 'events'")
    assert set(event_versions["version"]) == {1, 2}
    assert '"app_build_number": "BIGINT"' in event_versions.iloc[0]["schema_json"]
    assert '"app_build_number": "VARCHAR"' in event_versions.iloc[-1]["schema_json"]

    f11 = _inject(baseline, "F11")
    metric_versions = f11.tables["metric_versions"].query(
        "metric_id == 'daily_active_users'"
    )
    assert set(metric_versions["version"]) == {1, 2}
    assert metric_versions.iloc[0]["query"] != metric_versions.iloc[-1]["query"]

    f12 = _inject(baseline, "F12")
    configs = f12.tables["experiment_configs"]
    assert set(configs["version"]) == {1, 2}
    assert configs.iloc[-1]["effective_at"] == pd.Timestamp(TARGET_DATE)

    for value in f11.tables["metric_versions"]["definition_hash"]:
        assert len(value) == 64
        int(value, 16)


def test_f03_preserves_nullable_integer_schema_and_row_count(
    baseline: dict[str, pd.DataFrame],
) -> None:
    result = _inject(baseline, "F03")
    assert baseline["events"]["user_id"].dtype == result.tables["events"]["user_id"].dtype
    assert len(baseline["events"]) == len(result.tables["events"])
    assert result.tables["events"]["user_id"].isna().sum() > baseline["events"]["user_id"].isna().sum()


def test_f12_is_paired_deterministic_and_causally_ordered(
    baseline: dict[str, pd.DataFrame],
) -> None:
    before_users = baseline["users"][
        ["user_id", "conversion_score", "subscription_timing_score"]
    ].copy(deep=True)
    first = _inject(baseline, "F12")
    second = _inject(baseline, "F12")
    for table_name in (
        "experiment_assignments",
        "experiment_configs",
        "subscriptions",
        "daily_metrics",
    ):
        assert_frame_equal(first.tables[table_name], second.tables[table_name])
    assert_frame_equal(
        before_users,
        baseline["users"][
            ["user_id", "conversion_score", "subscription_timing_score"]
        ],
    )
    assert (
        baseline["experiment_assignments"]["assigned_time"]
        == baseline["experiment_assignments"]["user_id"].map(
            baseline["users"].set_index("user_id")["register_time"]
        )
    ).all()


@pytest.mark.parametrize("fault_id", LATER_FAULT_IDS)
def test_f07_to_f12_are_exactly_reproducible_for_same_seed(
    fault_id: str,
    baseline: dict[str, pd.DataFrame],
) -> None:
    case = next(
        case
        for case in load_ground_truth_cases(Path("benchmark/ground_truth"))
        if case.fault_id == fault_id
    )
    first = inject_case(
        baseline,
        case,
        rng=np.random.default_rng(99),
        start_date=START_DATE,
        days=30,
    )
    second = inject_case(
        baseline,
        case,
        rng=np.random.default_rng(99),
        start_date=START_DATE,
        days=30,
    )

    for table_name in first.tables:
        assert_frame_equal(
            first.tables[table_name],
            second.tables[table_name],
            check_exact=True,
        )


def test_fault_injection_does_not_mutate_healthy_baseline(
    baseline: dict[str, pd.DataFrame],
) -> None:
    before = {name: frame.copy(deep=True) for name, frame in baseline.items()}
    for fault_id in [f"F{i:02d}" for i in range(1, 13)]:
        inject_fault(
            baseline,
            fault_id,
            TARGET_DATE,
            rng=np.random.default_rng(99),
            start_date=START_DATE,
            days=30,
        )
    for table_name, frame in baseline.items():
        assert_frame_equal(frame, before[table_name])
