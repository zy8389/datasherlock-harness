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


@pytest.fixture(scope="module")
def baseline() -> dict[str, pd.DataFrame]:
    return generate_dataset(500, 30, 10_000, 42, START_DATE)


@pytest.fixture(scope="module")
def focused_cases() -> dict[str, GroundTruthCase]:
    cases = load_ground_truth_cases(Path("benchmark/ground_truth"))
    return {case.case_id: case for case in cases if case.case_id in FOCUSED_CASE_IDS}


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


@pytest.mark.parametrize("case_id", FOCUSED_CASE_IDS)
def test_evidence_validator_uses_the_bound_case_contract(
    case_id: str,
    baseline: dict[str, pd.DataFrame],
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    result = _inject(baseline, focused_cases[case_id])

    validate_expected_evidence(result, baseline)


@pytest.mark.parametrize("case_id", FOCUSED_CASE_IDS)
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


def test_single_evidence_path_fails_complete_ground_truth_validation(
    focused_cases: dict[str, GroundTruthCase],
) -> None:
    case = focused_cases["F01-001"]
    invalid_case = case.model_copy(update={"evidence_paths": case.evidence_paths[:1]})

    with pytest.raises(ValueError, match="at least two"):
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

    with pytest.raises(ValueError, match="non-business"):
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


def test_loader_is_the_catalog_aware_ground_truth_entry_point(
    tmp_path: Path, focused_cases: dict[str, GroundTruthCase]
) -> None:
    payload = yaml.safe_load(Path("benchmark/ground_truth/F01-001.yaml").read_text())
    payload["evidence_paths"] = payload["evidence_paths"][:1]
    (tmp_path / "F01-001.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="at least two"):
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

    for sql in _evidence_sql(case):
        query_result = run_readonly_sql(database_path, sql)
        assert query_result.row_count >= 1
        assert query_result.columns
