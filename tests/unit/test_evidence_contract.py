import json
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml
from pydantic import ValidationError

from benchmark.fault_injector import inject_case, validate_expected_evidence
from config.faults import (
    EvidenceSourceType,
    GroundTruthCase,
    load_fault_catalog,
    load_ground_truth_cases,
    validate_ground_truth_case,
)
from data.generator import generate_dataset, write_outputs
from tools.sql_runner import run_readonly_sql

START_DATE = pd.Timestamp("2026-01-01")
FOCUSED_CASE_IDS = (
    "F01-001",
    "F04-001",
    "F05-001",
    "F10-001",
    "F11-001",
    "F12-001",
)
POSITIVE_RUNTIME_EVIDENCE_CASE_IDS = (
    "F01-001",
    "F04-001",
    "F05-001",
    "F10-001",
    "F11-001",
    "F12-001",
)
SQL_EVIDENCE_CASE_IDS = ("F07-001", "F08-001", "F09-001")


@pytest.fixture(scope="module")
def baseline() -> dict[str, pd.DataFrame]:
    return generate_dataset(500, 30, 10_000, 42, START_DATE)


@pytest.fixture(scope="module")
def focused_cases() -> dict[str, GroundTruthCase]:
    cases = load_ground_truth_cases(Path("benchmark/ground_truth"))
    return {case.case_id: case for case in cases if case.case_id in FOCUSED_CASE_IDS}


@pytest.fixture(scope="module")
def all_cases() -> dict[str, GroundTruthCase]:
    return {
        case.case_id: case
        for case in load_ground_truth_cases(Path("benchmark/ground_truth"))
    }


def _inject(
    baseline: dict[str, pd.DataFrame], case: GroundTruthCase
):
    return inject_case(
        baseline,
        case,
        rng=np.random.default_rng(99),
        start_date=START_DATE,
        days=30,
    )


@pytest.mark.parametrize("case_id", FOCUSED_CASE_IDS)
def test_focused_case_has_independent_evidence_contract(
    case_id: str, focused_cases: dict[str, GroundTruthCase]
) -> None:
    case = focused_cases[case_id]
    source_types = {path.source_type for path in case.evidence_paths}

    assert len(case.evidence_paths) >= 2
    assert EvidenceSourceType.BUSINESS_DATA in source_types
    assert source_types.difference({EvidenceSourceType.BUSINESS_DATA})
    assert {path.asset for path in case.evidence_paths}.issubset(case.affected_assets)


@pytest.mark.parametrize("case_id", POSITIVE_RUNTIME_EVIDENCE_CASE_IDS)
def test_evidence_validator_uses_the_bound_case_contract(
    case_id: str,
    baseline: dict[str, pd.DataFrame],
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    result = _inject(baseline, focused_cases[case_id])

    validate_expected_evidence(result, baseline)


@pytest.mark.parametrize("case_id", POSITIVE_RUNTIME_EVIDENCE_CASE_IDS)
def test_missing_case_specific_metadata_fails_validation(
    case_id: str,
    baseline: dict[str, pd.DataFrame],
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    case = focused_cases[case_id]
    result = _inject(baseline, case)
    independent_path = next(
        path
        for path in case.evidence_paths
        if path.source_type != EvidenceSourceType.BUSINESS_DATA
    )
    result.tables[independent_path.asset] = baseline[independent_path.asset].copy(
        deep=True
    )

    with pytest.raises(ValueError, match="target|evidence|metadata|schema|version|config"):
        validate_expected_evidence(result, baseline)


@pytest.mark.parametrize(
    ("asset", "message"),
    [
        ("experiment_configs", "fault experiment config"),
        ("experiment_assignments", "assignment distribution did not change"),
    ],
)
def test_f12_evidence_requires_changed_assignment_and_config(
    asset: str,
    message: str,
    baseline: dict[str, pd.DataFrame],
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    case = focused_cases["F12-001"]
    result = _inject(baseline, case)
    result.tables[asset] = baseline[asset].copy(deep=True)

    with pytest.raises(ValueError, match=message):
        validate_expected_evidence(result, baseline)


def test_f12_evidence_requires_business_metric_effect(
    baseline: dict[str, pd.DataFrame],
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    case = focused_cases["F12-001"]
    result = _inject(baseline, case)
    result.tables["daily_metrics"] = baseline["daily_metrics"].copy(deep=True)

    with pytest.raises(ValueError, match="effect contract"):
        validate_expected_evidence(result, baseline)


def test_f05_injector_emits_independent_metric_version_evidence(
    baseline: dict[str, pd.DataFrame],
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    case = focused_cases["F05-001"]
    result = _inject(baseline, case)
    target_date = case.injection.metric_date
    assert target_date is not None

    region_users = set(
        baseline["users"].loc[
            baseline["users"]["region"].eq(case.injection.region), "user_id"
        ]
    )
    baseline_hours = (
        baseline["events"]
        .loc[
            baseline["events"]["event_time"].dt.date.eq(target_date)
            & baseline["events"]["user_id"].isin(region_users),
            "event_time",
        ]
        .dt.hour.value_counts()
        .sort_index()
    )
    fault_hours = (
        result.tables["events"]
        .loc[
            result.tables["events"]["event_time"].dt.date.eq(target_date)
            & result.tables["events"]["user_id"].isin(region_users),
            "event_time",
        ]
        .dt.hour.value_counts()
        .sort_index()
    )
    assert not baseline_hours.equals(fault_hours)

    baseline_versions = baseline["metric_versions"].loc[
        baseline["metric_versions"]["metric_id"].eq(case.affected_metric)
    ].sort_values(["version", "effective_at"])
    assert baseline_versions.iloc[-1]["timezone"] == "UTC"
    fault_versions = result.tables["metric_versions"].loc[
        result.tables["metric_versions"]["metric_id"].eq(case.affected_metric)
    ]
    fault_specific_versions = fault_versions.loc[
        fault_versions["version"].astype(int).gt(int(baseline_versions.iloc[-1]["version"]))
        & fault_versions["effective_at"].map(
            lambda value: pd.Timestamp(value).date() == target_date
        )
    ]
    assert len(fault_specific_versions) == 1
    fault_version = fault_specific_versions.iloc[0]
    assert fault_version["timezone"] == case.injection.to_value == "Asia/Shanghai"
    assert fault_version["query"] == baseline_versions.iloc[-1]["query"]
    assert fault_version["definition_hash"] == baseline_versions.iloc[-1]["definition_hash"]
    validate_expected_evidence(result, baseline)


def test_f05_validator_requires_utc_baseline_timezone(
    baseline: dict[str, pd.DataFrame],
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    case = focused_cases["F05-001"]
    result = _inject(baseline, case)
    invalid_baseline = {name: frame.copy(deep=True) for name, frame in baseline.items()}
    invalid_baseline["metric_versions"].loc[
        invalid_baseline["metric_versions"]["metric_id"].eq(case.affected_metric),
        "timezone",
    ] = "Asia/Shanghai"

    with pytest.raises(ValueError, match="baseline timezone must be UTC"):
        validate_expected_evidence(result, invalid_baseline)


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("query", "SELECT changed", "query must remain unchanged"),
        ("definition_hash", "changed", "definition hash must remain unchanged"),
    ],
)
def test_f05_validator_requires_unchanged_metric_definition(
    column: str,
    value: str,
    message: str,
    baseline: dict[str, pd.DataFrame],
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    case = focused_cases["F05-001"]
    result = _inject(baseline, case)
    fault_versions = result.tables["metric_versions"]
    fault_index = fault_versions.index[
        fault_versions["metric_id"].eq(case.affected_metric)
        & fault_versions["version"].astype(int).gt(1)
    ][0]
    fault_versions.loc[fault_index, column] = value

    with pytest.raises(ValueError, match=message):
        validate_expected_evidence(result, baseline)


def test_single_evidence_path_fails_complete_ground_truth_validation(
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    case = focused_cases["F01-001"]
    invalid_case = case.model_copy(update={"evidence_paths": case.evidence_paths[:1]})

    with pytest.raises(ValueError, match="missing required evidence sources"):
        validate_ground_truth_case(invalid_case)


def test_two_business_evidence_paths_fail_independence_validation(
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    case = focused_cases["F01-001"]
    business_paths = [
        path for path in case.evidence_paths if path.source_type == EvidenceSourceType.BUSINESS_DATA
    ]
    second_business_path = case.evidence_paths[0].model_copy(
        update={"asset": "events", "signal": "a second business signal"}
    )
    invalid_case = case.model_copy(
        update={"evidence_paths": business_paths + [second_business_path]}
    )

    with pytest.raises(ValueError, match="missing required evidence sources"):
        validate_ground_truth_case(invalid_case)


def test_evidence_asset_outside_affected_assets_fails_model_validation(
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    payload = focused_cases["F01-001"].model_dump(mode="json")
    payload["affected_assets"] = ["events"]
    payload["evidence_paths"][1]["asset"] = "partition_metadata"

    with pytest.raises(ValidationError, match="not affected assets"):
        GroundTruthCase.model_validate(payload)


def test_invalid_evidence_source_type_fails_model_validation(
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    payload = focused_cases["F01-001"].model_dump(mode="json")
    payload["evidence_paths"][0]["source_type"] = "not_a_source"

    with pytest.raises(ValidationError):
        GroundTruthCase.model_validate(payload)


def test_duplicate_evidence_path_fails_model_validation(
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    payload = focused_cases["F01-001"].model_dump(mode="json")
    payload["evidence_paths"].append(dict(payload["evidence_paths"][0]))

    with pytest.raises(ValidationError, match="unique"):
        GroundTruthCase.model_validate(payload)


def test_ground_truth_missing_catalog_affected_asset_fails_validation(
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    case = focused_cases["F01-001"]
    invalid_case = case.model_copy(
        update={"affected_assets": ["events", "partition_metadata"]}
    )

    with pytest.raises(ValueError, match="affected_assets do not match"):
        validate_ground_truth_case(invalid_case)


def test_ground_truth_extra_catalog_affected_asset_fails_validation(
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    case = focused_cases["F01-001"]
    invalid_case = case.model_copy(
        update={"affected_assets": [*case.affected_assets, "unexpected_asset"]}
    )

    with pytest.raises(ValueError, match="affected_assets do not match"):
        validate_ground_truth_case(invalid_case)


def test_ground_truth_effect_threshold_mismatch_fails_validation(
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    case = focused_cases["F12-001"]
    invalid_case = case.model_copy(update={"minimum_effect_size": 0.04})

    with pytest.raises(ValueError, match="minimum_effect_size does not match"):
        validate_ground_truth_case(invalid_case)


def test_ground_truth_missing_catalog_required_source_fails_validation(
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    case = focused_cases["F01-001"]
    invalid_case = case.model_copy(update={"evidence_paths": case.evidence_paths[:1]})

    with pytest.raises(ValueError, match="missing required evidence sources"):
        validate_ground_truth_case(invalid_case)


def test_loader_is_the_catalog_aware_ground_truth_entry_point(
    tmp_path: Path, focused_cases: dict[str, GroundTruthCase]
) -> None:
    payload = yaml.safe_load(Path("benchmark/ground_truth/F01-001.yaml").read_text())
    payload["evidence_paths"] = payload["evidence_paths"][:1]
    (tmp_path / "F01-001.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="missing required evidence sources"):
        load_ground_truth_cases(tmp_path, load_fault_catalog())


METADATA_TABLES = (
    "pipeline_runs",
    "partition_metadata",
    "schema_snapshots",
    "metric_versions",
    "experiment_configs",
)


def test_all_metadata_tables_are_queryable_through_sql_runner(
    tmp_path: Path, baseline: dict[str, pd.DataFrame]
) -> None:
    write_outputs(tmp_path, baseline)
    database_path = tmp_path / "datasherlock.duckdb"

    for table_name in METADATA_TABLES:
        result = run_readonly_sql(
            database_path, f"SELECT * FROM {table_name} LIMIT 1"
        )
        assert result.row_count == 1
        assert result.columns


def _evidence_sql(case: GroundTruthCase) -> tuple[str, str]:
    target_date = case.injection.metric_date
    assert target_date is not None
    date_literal = target_date.isoformat()
    next_date_literal = (target_date + timedelta(days=1)).isoformat()

    queries = {
        "F07": (
            (
                "SELECT COUNT(DISTINCT e.user_id) AS event_users, "
                "COUNT(DISTINCT s.user_id) AS matched_users, "
                "COUNT(DISTINCT e.user_id) - COUNT(DISTINCT s.user_id) "
                "AS unmatched_users FROM events e "
                "LEFT JOIN subscriptions s ON e.user_id = s.user_id "
                f"WHERE CAST(e.event_time AS DATE) = DATE '{date_literal}'"
            ),
            (
                "SELECT metric_id, version, definition_hash, query, effective_at "
                "FROM metric_versions WHERE metric_id = 'daily_active_users' "
                "ORDER BY version"
            ),
        ),
        "F08": (
            (
                "WITH joined AS ("
                "SELECT e.event_id FROM events e "
                "INNER JOIN experiment_assignments a ON e.user_id = a.user_id "
                f"WHERE CAST(e.event_time AS DATE) = DATE '{date_literal}' "
                "AND e.event_name = 'run_ai_task') "
                "SELECT (SELECT COUNT(*) FROM experiment_assignments) AS assignment_rows, "
                "(SELECT COUNT(DISTINCT user_id) FROM experiment_assignments) AS assignment_users, "
                "COUNT(*) AS joined_rows, COUNT(DISTINCT event_id) AS joined_events "
                "FROM joined"
            ),
            (
                "SELECT metric_id, version, definition_hash, query, effective_at "
                "FROM metric_versions WHERE metric_id = 'ai_task_count' "
                "ORDER BY version"
            ),
        ),
        "F09": (
            (
                "SELECT COUNT(*) AS total_events, "
                "COUNT(*) FILTER (WHERE event_name = 'run_ai_task') AS run_ai_task_rows, "
                "COUNT(*) FILTER (WHERE event_name = 'execute_ai_task') AS execute_ai_task_rows "
                "FROM events "
                f"WHERE CAST(event_time AS DATE) = DATE '{date_literal}'"
            ),
            (
                "SELECT table_name, version, schema_json, effective_at "
                "FROM schema_snapshots WHERE table_name = 'events' ORDER BY version"
            ),
        ),
        "F01": (
            (
                f"SELECT device_type, COUNT(*) AS event_count FROM events "
                f"WHERE CAST(event_time AS DATE) = DATE '{date_literal}' "
                "GROUP BY device_type"
            ),
            (
                f"SELECT table_name, partition_value, row_count, status FROM partition_metadata "
                f"WHERE table_name = 'events' AND partition_value = '{date_literal}/android'"
            ),
        ),
        "F04": (
            (
                f"SELECT CAST(event_time AS DATE) AS event_date, device_type, COUNT(*) AS event_count "
                f"FROM events WHERE CAST(event_time AS DATE) BETWEEN DATE '{date_literal}' "
                f"AND DATE '{next_date_literal}' GROUP BY event_date, device_type"
            ),
            (
                f"SELECT target_table, target_partition, status, error_type FROM pipeline_runs "
                f"WHERE target_table = 'events' AND target_partition = '{date_literal}'"
            ),
        ),
        "F05": (
            (
                f"SELECT u.region, EXTRACT(HOUR FROM e.event_time) AS event_hour, COUNT(*) AS event_count "
                f"FROM events e JOIN users u ON e.user_id = u.user_id "
                f"WHERE CAST(e.event_time AS DATE) = DATE '{date_literal}' "
                "GROUP BY u.region, event_hour"
            ),
            (
                "SELECT metric_id, version, timezone, date_grain, effective_at "
                "FROM metric_versions WHERE metric_id = 'daily_active_users' "
                "ORDER BY effective_at"
            ),
        ),
        "F10": (
            (
                f"SELECT COUNT(*) AS event_count FROM events "
                f"WHERE CAST(event_time AS DATE) = DATE '{date_literal}'"
            ),
            (
                "SELECT table_name, version, schema_json, effective_at FROM schema_snapshots "
                "WHERE table_name = 'events' ORDER BY effective_at"
            ),
        ),
        "F11": (
            (
                f"SELECT COUNT(*) AS event_count FROM events "
                f"WHERE CAST(event_time AS DATE) = DATE '{date_literal}'"
            ),
            (
                "SELECT metric_id, version, definition_hash, query, effective_at "
                "FROM metric_versions WHERE metric_id = 'daily_active_users' "
                "ORDER BY effective_at"
            ),
        ),
        "F12": (
            "SELECT variant, COUNT(*) AS users FROM experiment_assignments GROUP BY variant",
            (
                "SELECT experiment_id, version, control_ratio, treatment_ratio, effective_at "
                "FROM experiment_configs ORDER BY effective_at"
            ),
        ),
    }
    return queries[case.fault_id]


@pytest.mark.parametrize("case_id", FOCUSED_CASE_IDS)
def test_each_focused_fault_has_business_and_independent_sql_evidence(
    case_id: str,
    tmp_path: Path,
    baseline: dict[str, pd.DataFrame],
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    case = focused_cases[case_id]
    result = _inject(baseline, case)
    output_dir = tmp_path / case_id
    write_outputs(output_dir, result.tables)
    database_path = output_dir / "datasherlock.duckdb"

    business_sql, metadata_sql = _evidence_sql(case)
    business_result = run_readonly_sql(database_path, business_sql)
    metadata_result = run_readonly_sql(database_path, metadata_sql)
    assert business_result.row_count >= 1
    assert business_result.columns
    assert metadata_result.row_count >= 1
    assert metadata_result.columns

    metadata_rows = [
        dict(zip(metadata_result.columns, row, strict=True))
        for row in metadata_result.rows
    ]
    if case.fault_id == "F01":
        target = metadata_rows[0]
        assert int(target["row_count"]) == 0
        assert target["status"] == "missing"
    elif case.fault_id == "F04":
        target = metadata_rows[0]
        assert target["status"] == "delayed" or target["error_type"] == "data_delay"
    elif case.fault_id == "F05":
        baseline_version, fault_version = metadata_rows[0], metadata_rows[-1]
        assert int(fault_version["version"]) > int(baseline_version["version"])
        assert baseline_version["timezone"] == "UTC"
        assert fault_version["timezone"] == "Asia/Shanghai"
    elif case.fault_id == "F10":
        schema = json.loads(str(metadata_rows[-1]["schema_json"]))
        assert schema["app_build_number"] == "VARCHAR"
    elif case.fault_id == "F11":
        baseline_version, fault_version = metadata_rows[0], metadata_rows[-1]
        assert int(fault_version["version"]) > int(baseline_version["version"])
        assert fault_version["definition_hash"] != baseline_version["definition_hash"]
        assert fault_version["query"] != baseline_version["query"]
    elif case.fault_id == "F12":
        target = metadata_rows[-1]
        assert float(target["control_ratio"]) == pytest.approx(0.20)
        assert float(target["treatment_ratio"]) == pytest.approx(0.80)


@pytest.mark.parametrize("case_id", SQL_EVIDENCE_CASE_IDS)
def test_f07_to_f09_causal_evidence_is_queryable_through_sql_runner(
    case_id: str,
    tmp_path: Path,
    baseline: dict[str, pd.DataFrame],
    all_cases: dict[str, GroundTruthCase],
) -> None:
    case = all_cases[case_id]
    result = _inject(baseline, case)
    baseline_dir = tmp_path / "baseline"
    fault_dir = tmp_path / "fault"
    write_outputs(baseline_dir, baseline)
    write_outputs(fault_dir, result.tables)

    business_sql, metadata_sql = _evidence_sql(case)
    baseline_business = run_readonly_sql(
        baseline_dir / "datasherlock.duckdb", business_sql
    )
    fault_business = run_readonly_sql(
        fault_dir / "datasherlock.duckdb", business_sql
    )
    fault_metadata = run_readonly_sql(
        fault_dir / "datasherlock.duckdb", metadata_sql
    )
    assert fault_business.row_count == 1
    assert fault_business.columns
    assert fault_metadata.row_count >= 1
    assert fault_metadata.columns

    baseline_values = dict(
        zip(baseline_business.columns, baseline_business.rows[0], strict=True)
    )
    fault_values = dict(
        zip(fault_business.columns, fault_business.rows[0], strict=True)
    )
    if case.fault_id == "F07":
        assert fault_values["matched_users"] < fault_values["event_users"]
        assert fault_values["unmatched_users"] > 0
        assert fault_values["matched_users"] == baseline_values["matched_users"]
    elif case.fault_id == "F08":
        assert baseline_values["assignment_rows"] == baseline_values["assignment_users"]
        assert fault_values["assignment_rows"] > fault_values["assignment_users"]
        assert fault_values["joined_rows"] > baseline_values["joined_rows"]
        assert fault_values["joined_events"] == baseline_values["joined_events"]
    else:
        assert fault_values["total_events"] == baseline_values["total_events"]
        assert fault_values["run_ai_task_rows"] < baseline_values["run_ai_task_rows"]
        assert fault_values["execute_ai_task_rows"] > 0
