from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from benchmark.fault_injector import inject_case, validate_expected_evidence
from config.faults import GroundTruthCase, load_ground_truth_cases
from data.generator import generate_dataset, write_outputs
from tools.sql_runner import run_readonly_sql

START_DATE = pd.Timestamp("2026-01-01")


@pytest.fixture(scope="module")
def baseline() -> dict[str, pd.DataFrame]:
    return generate_dataset(500, 30, 10_000, 42, START_DATE)


@pytest.fixture(scope="module")
def cases() -> dict[str, GroundTruthCase]:
    return {
        case.case_id: case
        for case in load_ground_truth_cases(Path("benchmark/ground_truth"))
        if case.case_id in {"F01-001", "F11-001"}
    }


@pytest.mark.parametrize("case_id", ["F01-001", "F11-001"])
def test_minimal_inject_query_evidence_root_cause_e2e(
    case_id: str,
    tmp_path: Path,
    baseline: dict[str, pd.DataFrame],
    cases: dict[str, GroundTruthCase],
) -> None:
    case = cases[case_id]
    result = inject_case(
        baseline,
        case,
        rng=np.random.default_rng(99),
        start_date=START_DATE,
        days=30,
    )
    validate_expected_evidence(result, baseline)

    output_dir = tmp_path / case_id
    write_outputs(output_dir, result.tables)
    database_path = output_dir / "datasherlock.duckdb"
    target_date = case.injection.metric_date
    assert target_date is not None
    date_literal = target_date.isoformat()

    business_sql = (
        "SELECT COUNT(*) AS event_count, "
        "COUNT(DISTINCT user_id) AS user_count "
        f"FROM events WHERE CAST(event_time AS DATE) = DATE '{date_literal}'"
    )
    business = run_readonly_sql(database_path, business_sql)
    assert business.row_count == 1
    assert business.columns == ["event_count", "user_count"]

    if case_id == "F01-001":
        metadata_sql = (
            "SELECT partition_value, row_count, status FROM partition_metadata "
            f"WHERE table_name = 'events' AND partition_value = '{date_literal}/android'"
        )
        metadata = run_readonly_sql(database_path, metadata_sql)
        assert metadata.row_count == 1
        row = dict(zip(metadata.columns, metadata.rows[0], strict=True))
        assert int(row["row_count"]) == 0
        assert row["status"] == "missing"
        assert case.root_cause_type == "missing_partition"
    else:
        metadata_sql = (
            "SELECT version, definition_hash, query FROM metric_versions "
            "WHERE metric_id = 'daily_active_users' ORDER BY version"
        )
        metadata = run_readonly_sql(database_path, metadata_sql)
        assert metadata.row_count == 2
        baseline_version = dict(zip(metadata.columns, metadata.rows[0], strict=True))
        fault_version = dict(zip(metadata.columns, metadata.rows[-1], strict=True))
        assert int(fault_version["version"]) > int(baseline_version["version"])
        assert fault_version["definition_hash"] != baseline_version["definition_hash"]
        assert fault_version["query"] != baseline_version["query"]
        assert case.root_cause_type == "metric_definition_change"

    assert result.ground_truth_case is case
    assert result.ground_truth_case.root_cause_type == case.root_cause_type
