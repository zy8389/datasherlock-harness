from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from tools import data_quality
from tools.data_quality import (
    SCHEMA_DRIFT_ASSESSMENT_INSUFFICIENT_HISTORY,
    DataQualityScope,
    check_duplicate_rate,
    check_freshness,
    check_null_rate,
    detect_distribution_drift,
    detect_schema_drift,
)
from tools.sql_runner import SqlExecutionResponse


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    path = tmp_path / "quality.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE events (event_id INTEGER, user_id INTEGER)")
        connection.execute(
            "INSERT INTO events VALUES (1, 101), (2, 102), (3, 103), (4, 104)"
        )
    return path


def test_check_null_rate_passes_and_records_traceable_evidence(
    database_path: Path, tmp_path: Path
) -> None:
    audit_path = tmp_path / "audit" / "queries.jsonl"

    result = check_null_rate(
        database_path,
        "events",
        "user_id",
        threshold=0.01,
        incident_id="INC-003",
        trace_id="TRACE-003",
        audit_path=audit_path,
    )

    assert result.status == "success"
    assert result.passed is True
    assert result.observed_value == 0.0
    assert result.threshold == 0.01
    assert result.query_id
    assert result.evidence[0].query_id == result.query_id
    assert result.evidence[0].details == {
        "total_rows": 4,
        "null_rows": 0,
        "null_rate": 0.0,
    }
    assert audit_path.exists()


def test_check_null_rate_fails_when_nulls_exceed_threshold(database_path: Path) -> None:
    with duckdb.connect(str(database_path)) as connection:
        connection.execute("INSERT INTO events VALUES (5, NULL), (6, NULL)")

    result = check_null_rate(database_path, "events", "user_id", threshold=0.25)

    assert result.status == "success"
    assert result.passed is False
    assert result.observed_value == pytest.approx(2 / 6)
    assert result.evidence[0].details["null_rows"] == 2
    assert result.evidence[0].details["total_rows"] == 6


def test_check_null_rate_applies_dimension_and_time_scope(database_path: Path) -> None:
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            "CREATE TABLE scoped_events (event_time TIMESTAMP, device_type VARCHAR, user_id INTEGER)"
        )
        connection.execute(
            """
            INSERT INTO scoped_events VALUES
            ('2026-01-29 12:00:00', 'android', 1),
            ('2026-01-30 12:00:00', 'android', NULL),
            ('2026-01-30 12:00:00', 'ios', NULL),
            ('2026-01-30 12:00:00', 'web', 4)
            """
        )

    scope = DataQualityScope(
        equals={"device_type": ["ios", "android"]},
        time_column="event_time",
        start=datetime(2026, 1, 30, tzinfo=UTC),
        end=datetime(2026, 1, 31, tzinfo=UTC),
    )
    result = check_null_rate(
        database_path,
        "scoped_events",
        "user_id",
        threshold=0.5,
        scope=scope,
    )

    assert result.status == "success"
    assert result.passed is False
    assert result.observed_value == 1.0
    assert result.evidence[0].details["total_rows"] == 2
    assert result.evidence[0].details["scope"] == scope.model_dump(mode="json")


def test_check_null_rate_does_not_pass_an_empty_table(database_path: Path) -> None:
    with duckdb.connect(str(database_path)) as connection:
        connection.execute("CREATE TABLE empty_events (user_id INTEGER)")

    result = check_null_rate(database_path, "empty_events", "user_id")

    assert result.status == "success"
    assert result.passed is False
    assert result.observed_value is None
    assert result.evidence[0].details == {
        "total_rows": 0,
        "null_rows": 0,
        "null_rate": None,
    }


def test_check_null_rate_rejects_unsafe_identifiers_without_querying(
    database_path: Path,
) -> None:
    result = check_null_rate(database_path, "events; DROP TABLE events", "user_id")

    assert result.status == "error"
    assert result.passed is None
    assert result.error == {
        "type": "validation",
        "message": (
            "table must contain only letters, numbers, and underscores, "
            "and must not start with a number"
        ),
    }


def test_check_duplicate_rate_passes_for_unique_keys(database_path: Path) -> None:
    result = check_duplicate_rate(database_path, "events", ["event_id"])

    assert result.status == "success"
    assert result.passed is True
    assert result.columns == ["event_id"]
    assert result.observed_value == 0.0
    assert result.evidence[0].details == {
        "total_rows": 4,
        "unique_key_rows": 4,
        "duplicate_rows": 0,
        "duplicate_rate": 0.0,
    }


def test_check_duplicate_rate_fails_when_duplicate_keys_exceed_threshold(
    database_path: Path,
) -> None:
    with duckdb.connect(str(database_path)) as connection:
        connection.execute("INSERT INTO events VALUES (2, 202)")

    result = check_duplicate_rate(
        database_path,
        "events",
        ["event_id"],
        threshold=0.1,
    )

    assert result.status == "success"
    assert result.passed is False
    assert result.observed_value == pytest.approx(1 / 5)
    assert result.evidence[0].details["duplicate_rows"] == 1


def test_check_duplicate_rate_supports_composite_keys(database_path: Path) -> None:
    with duckdb.connect(str(database_path)) as connection:
        connection.execute("INSERT INTO events VALUES (1, 999), (1, 101)")

    result = check_duplicate_rate(database_path, "events", ["event_id", "user_id"])

    assert result.status == "success"
    assert result.passed is False
    assert result.observed_value == pytest.approx(1 / 6)
    assert result.evidence[0].details["unique_key_rows"] == 5


def test_check_duplicate_rate_rejects_empty_keys_without_querying(
    database_path: Path,
) -> None:
    result = check_duplicate_rate(database_path, "events", [])

    assert result.status == "error"
    assert result.passed is None
    assert result.error == {
        "type": "validation",
        "message": "keys must be a non-empty list of column names",
    }


def test_check_freshness_passes_for_recent_data(database_path: Path, tmp_path: Path) -> None:
    with duckdb.connect(str(database_path)) as connection:
        connection.execute("CREATE TABLE event_ingest (event_time TIMESTAMP)")
        connection.execute("INSERT INTO event_ingest VALUES ('2026-01-30 11:55:00')")

    result = check_freshness(
        database_path,
        "event_ingest",
        "event_time",
        reference_time=datetime(2026, 1, 30, 12, tzinfo=UTC),
        max_age=timedelta(minutes=10),
        incident_id="INC-004",
        trace_id="TRACE-004",
        audit_path=tmp_path / "audit" / "freshness.jsonl",
    )

    assert result.status == "success"
    assert result.passed is True
    assert result.observed_value == 300.0
    assert result.threshold == 600.0
    assert result.evidence[0].details["latest_timestamp"] == "2026-01-30T11:55:00+00:00"


def test_check_freshness_fails_for_stale_data(database_path: Path) -> None:
    with duckdb.connect(str(database_path)) as connection:
        connection.execute("CREATE TABLE stale_events (event_time TIMESTAMP)")
        connection.execute("INSERT INTO stale_events VALUES ('2026-01-29 12:00:00')")

    result = check_freshness(
        database_path,
        "stale_events",
        "event_time",
        reference_time=datetime(2026, 1, 30, 12, tzinfo=UTC),
        max_age=timedelta(hours=1),
    )

    assert result.status == "success"
    assert result.passed is False
    assert result.observed_value == 86_400.0
    assert result.evidence[0].details["max_age_seconds"] == 3_600.0


def test_check_freshness_applies_partition_scope(database_path: Path) -> None:
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            CREATE TABLE partition_metadata (
                table_name VARCHAR,
                partition_value VARCHAR,
                updated_at TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO partition_metadata VALUES
            ('events', '2026-01-30/android', '2026-01-30 10:00:00'),
            ('events', '2026-01-30/web', '2026-01-30 11:55:00')
            """
        )

    result = check_freshness(
        database_path,
        "partition_metadata",
        "updated_at",
        reference_time=datetime(2026, 1, 30, 12, tzinfo=UTC),
        max_age=timedelta(hours=1),
        scope=DataQualityScope(
            equals={
                "table_name": "events",
                "partition_value": "2026-01-30/android",
            }
        ),
    )

    assert result.status == "success"
    assert result.passed is False
    assert result.observed_value == 7_200.0
    assert result.evidence[0].details["total_rows"] == 1


def test_check_freshness_does_not_pass_an_empty_table(database_path: Path) -> None:
    with duckdb.connect(str(database_path)) as connection:
        connection.execute("CREATE TABLE empty_ingest (event_time TIMESTAMP)")

    result = check_freshness(
        database_path,
        "empty_ingest",
        "event_time",
        reference_time=datetime(2026, 1, 30, 12, tzinfo=UTC),
        max_age=timedelta(hours=1),
    )

    assert result.status == "success"
    assert result.passed is False
    assert result.observed_value is None
    assert result.evidence[0].details["timestamp_rows"] == 0


def test_check_freshness_returns_structured_error_for_missing_timestamp_column(
    database_path: Path,
) -> None:
    result = check_freshness(
        database_path,
        "events",
        "missing_timestamp",
        reference_time=datetime(2026, 1, 30, 12, tzinfo=UTC),
        max_age=timedelta(hours=1),
    )

    assert result.status == "error"
    assert result.passed is None
    assert result.query_id
    assert result.error is not None
    assert result.error["type"] == "execution"


def _create_schema_snapshots(database_path: Path) -> None:
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            CREATE TABLE schema_snapshots (
                table_name VARCHAR,
                version INTEGER,
                schema_json VARCHAR,
                effective_at TIMESTAMP
            )
            """
        )


@pytest.mark.parametrize("snapshot_count", [0, 1])
def test_detect_schema_drift_is_inconclusive_for_insufficient_history(
    database_path: Path,
    snapshot_count: int,
) -> None:
    _create_schema_snapshots(database_path)
    if snapshot_count:
        with duckdb.connect(str(database_path)) as connection:
            connection.execute(
                """
                INSERT INTO schema_snapshots VALUES
                ('events', 1, '{"event_id": "BIGINT"}', '2026-01-30 00:00:00')
                """
            )

    result = detect_schema_drift(database_path, "events")

    assert result.status == "success"
    assert result.passed is None
    assert result.error is None
    assert result.observed_value is None
    assert result.evidence[0].details == {
        "assessment": SCHEMA_DRIFT_ASSESSMENT_INSUFFICIENT_HISTORY,
        "snapshot_count": snapshot_count,
        "required_snapshot_count": 2,
    }


def test_detect_schema_drift_returns_error_when_snapshot_query_fails(
    database_path: Path,
) -> None:
    result = detect_schema_drift(database_path, "events")

    assert result.status == "error"
    assert result.passed is None
    assert result.error is not None
    assert result.error["type"] == "execution"
    assert result.evidence == []


def test_detect_schema_drift_rejects_invalid_result_shape(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        data_quality,
        "execute_readonly_sql",
        lambda *_args, **_kwargs: SqlExecutionResponse(
            query_id="Q-BAD-SHAPE",
            status="success",
            statement_type="SELECT",
            columns=["version", "schema_json", "effective_at"],
            rows=[[1]],
            row_count=1,
        ),
    )

    result = detect_schema_drift(database_path, "events")

    assert result.status == "error"
    assert result.passed is None
    assert result.error == {
        "type": "execution",
        "message": "schema-drift query returned an unexpected result shape",
    }


def test_detect_schema_drift_passes_for_unchanged_schema(database_path: Path) -> None:
    _create_schema_snapshots(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO schema_snapshots VALUES
            ('events', 1, '{"event_id": "BIGINT", "user_id": "BIGINT"}',
             '2026-01-29 00:00:00'),
            ('events', 2, '{"event_id": "BIGINT", "user_id": "BIGINT"}',
             '2026-01-30 00:00:00')
            """
        )

    result = detect_schema_drift(database_path, "events")

    assert result.status == "success"
    assert result.passed is True
    assert result.observed_value == 0.0
    assert result.columns == []
    assert result.evidence[0].details["type_changes"] == []


def test_detect_schema_drift_fails_for_f10_type_change(database_path: Path) -> None:
    _create_schema_snapshots(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO schema_snapshots VALUES
            ('events', 1, '{"app_build_number": "BIGINT"}',
             '2026-01-29 00:00:00'),
            ('events', 2, '{"app_build_number": "VARCHAR"}',
             '2026-01-30 00:00:00')
            """
        )

    result = detect_schema_drift(database_path, "events")

    assert result.status == "success"
    assert result.passed is False
    assert result.observed_value == 1.0
    assert result.columns == ["app_build_number"]
    assert result.evidence[0].details["type_changes"] == [
        {
            "column": "app_build_number",
            "previous_type": "BIGINT",
            "current_type": "VARCHAR",
        }
    ]
    assert (
        result.evidence[0].details.get("assessment")
        != SCHEMA_DRIFT_ASSESSMENT_INSUFFICIENT_HISTORY
    )


def test_detect_schema_drift_returns_structured_error_for_invalid_schema_json(
    database_path: Path,
) -> None:
    _create_schema_snapshots(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO schema_snapshots VALUES
            ('events', 1, '{"event_id": "BIGINT"}', '2026-01-29 00:00:00'),
            ('events', 2, 'not-json', '2026-01-30 00:00:00')
            """
        )

    result = detect_schema_drift(database_path, "events")

    assert result.status == "error"
    assert result.passed is None
    assert result.query_id
    assert result.error == {
        "type": "execution",
        "message": "schema_json is not valid JSON",
    }


def _create_event_distribution(database_path: Path) -> None:
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            "CREATE TABLE event_distribution (event_time TIMESTAMP, event_name VARCHAR)"
        )


def test_detect_distribution_drift_passes_for_stable_categories(
    database_path: Path,
) -> None:
    _create_event_distribution(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO event_distribution
            SELECT '2026-01-29 12:00:00', 'login' FROM range(11)
            UNION ALL
            SELECT '2026-01-29 12:00:00', 'run_ai_task' FROM range(9)
            UNION ALL
            SELECT '2026-01-30 12:00:00', 'login' FROM range(10)
            UNION ALL
            SELECT '2026-01-30 12:00:00', 'run_ai_task' FROM range(10)
            """
        )

    result = detect_distribution_drift(
        database_path,
        "event_distribution",
        "event_name",
        "event_time",
        baseline_start=datetime(2026, 1, 29, tzinfo=UTC),
        baseline_end=datetime(2026, 1, 30, tzinfo=UTC),
        current_start=datetime(2026, 1, 30, tzinfo=UTC),
        current_end=datetime(2026, 1, 31, tzinfo=UTC),
        threshold=0.1,
    )

    assert result.status == "success"
    assert result.passed is True
    assert result.observed_value == pytest.approx(0.05)
    assert result.evidence[0].details["baseline_window"]["row_count"] == 20


def test_detect_distribution_drift_fails_for_f09_renamed_event(
    database_path: Path,
) -> None:
    _create_event_distribution(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO event_distribution
            SELECT '2026-01-29 12:00:00', 'login' FROM range(10)
            UNION ALL
            SELECT '2026-01-29 12:00:00', 'run_ai_task' FROM range(10)
            UNION ALL
            SELECT '2026-01-30 12:00:00', 'login' FROM range(10)
            UNION ALL
            SELECT '2026-01-30 12:00:00', 'run_ai_task' FROM range(3)
            UNION ALL
            SELECT '2026-01-30 12:00:00', 'execute_ai_task' FROM range(7)
            """
        )

    result = detect_distribution_drift(
        database_path,
        "event_distribution",
        "event_name",
        "event_time",
        baseline_start=datetime(2026, 1, 29, tzinfo=UTC),
        baseline_end=datetime(2026, 1, 30, tzinfo=UTC),
        current_start=datetime(2026, 1, 30, tzinfo=UTC),
        current_end=datetime(2026, 1, 31, tzinfo=UTC),
        threshold=0.1,
    )

    assert result.status == "success"
    assert result.passed is False
    assert result.observed_value == pytest.approx(0.35)
    assert result.evidence[0].details["current_window"]["distribution"] == {
        "execute_ai_task": 0.35,
        "login": 0.5,
        "run_ai_task": 0.15,
    }


def test_detect_distribution_drift_returns_error_for_empty_window(
    database_path: Path,
) -> None:
    _create_event_distribution(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            "INSERT INTO event_distribution VALUES ('2026-01-29 12:00:00', 'login')"
        )

    result = detect_distribution_drift(
        database_path,
        "event_distribution",
        "event_name",
        "event_time",
        baseline_start=datetime(2026, 1, 29, tzinfo=UTC),
        baseline_end=datetime(2026, 1, 30, tzinfo=UTC),
        current_start=datetime(2026, 1, 30, tzinfo=UTC),
        current_end=datetime(2026, 1, 31, tzinfo=UTC),
    )

    assert result.status == "error"
    assert result.passed is None
    assert result.error == {
        "type": "execution",
        "message": "current window contains no rows",
    }


def test_detect_distribution_drift_returns_error_for_missing_column(
    database_path: Path,
) -> None:
    _create_event_distribution(database_path)

    result = detect_distribution_drift(
        database_path,
        "event_distribution",
        "missing_category",
        "event_time",
        baseline_start=datetime(2026, 1, 29, tzinfo=UTC),
        baseline_end=datetime(2026, 1, 30, tzinfo=UTC),
        current_start=datetime(2026, 1, 30, tzinfo=UTC),
        current_end=datetime(2026, 1, 31, tzinfo=UTC),
    )

    assert result.status == "error"
    assert result.passed is None
    assert result.query_id
    assert result.error is not None
    assert result.error["type"] == "execution"
