import subprocess
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest
import yaml

from src.data.generator import (
    generate_daily_metrics,
    generate_events,
    generate_experiment_assignments,
    generate_subscriptions,
    generate_users,
)


def test_saas_generator_creates_expected_outputs(tmp_path: Path) -> None:
    output_dir = tmp_path / "processed"

    result = subprocess.run(
        [
            sys.executable,
            "src/data/generator.py",
            "--users",
            "100",
            "--days",
            "7",
            "--events",
            "500",
            "--seed",
            "42",
            "--output-dir",
            str(output_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "SaaS mock data generated successfully." in result.stdout
    assert (output_dir / "users.parquet").exists()
    assert (output_dir / "events.parquet").exists()
    assert (output_dir / "subscriptions.parquet").exists()
    assert (output_dir / "experiment_assignments.parquet").exists()
    assert (output_dir / "daily_metrics.parquet").exists()
    assert (output_dir / "datasherlock.duckdb").exists()

    users = pd.read_parquet(output_dir / "users.parquet")
    events = pd.read_parquet(output_dir / "events.parquet")
    metrics = pd.read_parquet(output_dir / "daily_metrics.parquet")
    assert len(users) == 100
    assert len(events) == 500
    assert len(metrics) == 7
    assert {"user_id", "register_time", "user_type"}.issubset(users.columns)
    assert {"event_id", "user_id", "event_time", "event_name"}.issubset(events.columns)
    assert {"metric_date", "daily_active_users", "paid_users", "conversion_rate"}.issubset(metrics.columns)

    with duckdb.connect(str(output_dir / "datasherlock.duckdb"), read_only=True) as conn:
        table_names = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        assert table_names == {
            "users",
            "events",
            "subscriptions",
            "experiment_assignments",
            "daily_metrics",
        }
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 500


def test_generated_timestamps_respect_user_lifecycle() -> None:
    start_date = pd.Timestamp("2026-01-01")
    rng = np.random.default_rng(42)
    users = generate_users(200, start_date, 30, rng)
    events = generate_events(users, 2_000, start_date, 30, rng)
    subscriptions = generate_subscriptions(users, start_date, 30, rng)
    assignments = generate_experiment_assignments(users, start_date, 30, rng)

    register_times = users.set_index("user_id")["register_time"]
    assert (events["event_time"] >= events["user_id"].map(register_times)).all()
    assert (subscriptions["start_time"] >= subscriptions["user_id"].map(register_times)).all()
    assert (assignments["assigned_time"] >= assignments["user_id"].map(register_times)).all()
    assert (subscriptions.loc[subscriptions["subscription_status"] == "cancelled", "end_time"].notna()).all()


def test_paid_users_is_active_on_each_metric_date() -> None:
    start_date = pd.Timestamp("2026-01-01")
    users = pd.DataFrame(
        {
            "user_id": [1, 2],
            "register_time": [start_date, start_date],
        }
    )
    events = pd.DataFrame(
        {
            "event_id": [1, 2],
            "user_id": [1, 2],
            "event_time": [start_date, start_date + pd.Timedelta(days=1)],
            "event_name": ["login", "login"],
            "session_id": ["s1", "s2"],
            "duration_seconds": [10.0, 10.0],
        }
    )
    subscriptions = pd.DataFrame(
        {
            "user_id": [1, 2],
            "start_time": [start_date, start_date + pd.Timedelta(days=1)],
            "end_time": [start_date + pd.Timedelta(days=2), pd.NaT],
        }
    )

    metrics = generate_daily_metrics(users, events, subscriptions, start_date, 3)

    assert metrics["paid_users"].tolist() == [1, 2, 1]


def test_average_session_duration_aggregates_events_in_one_session() -> None:
    start_date = pd.Timestamp("2026-01-01")
    users = pd.DataFrame(
        {
            "user_id": [1, 2],
            "register_time": [start_date, start_date],
        }
    )
    events = pd.DataFrame(
        {
            "event_id": [1, 2, 3],
            "user_id": [1, 1, 2],
            "event_time": [start_date, start_date, start_date],
            "event_name": ["login", "create_project", "login"],
            "session_id": ["s1", "s1", "s2"],
            "duration_seconds": [10.0, 30.0, 20.0],
        }
    )
    subscriptions = pd.DataFrame(
        {
            "user_id": [],
            "start_time": pd.Series([], dtype="datetime64[ns]"),
            "end_time": pd.Series([], dtype="datetime64[ns]"),
        }
    )

    metrics = generate_daily_metrics(users, events, subscriptions, start_date, 1)

    assert metrics.loc[0, "average_session_duration"] == 30.0


def test_metric_semantics_define_required_metrics() -> None:
    metrics_path = Path(__file__).parents[2] / "config" / "metrics.yaml"
    with metrics_path.open(encoding="utf-8") as file:
        config = yaml.safe_load(file)

    definitions = {metric["id"]: metric for metric in config["metrics"]}
    expected_metric_fields = {
        "daily_active_users",
        "new_users",
        "paid_users",
        "ai_task_count",
        "average_session_duration",
        "conversion_rate",
    }
    assert definitions.keys() == expected_metric_fields
    assert definitions["paid_users"]["validity"]["end_operator"] == ">"
    assert definitions["conversion_rate"]["denominator"] == "daily_active_users"
    assert definitions["conversion_rate"]["zero_denominator"] == 0


def test_metric_semantics_match_generated_columns() -> None:
    start_date = pd.Timestamp("2026-01-01")
    rng = np.random.default_rng(42)
    users = generate_users(20, start_date, 3, rng)
    events = generate_events(users, 100, start_date, 3, rng)
    subscriptions = generate_subscriptions(users, start_date, 3, rng)
    metrics = generate_daily_metrics(users, events, subscriptions, start_date, 3)

    metrics_path = Path(__file__).parents[2] / "config" / "metrics.yaml"
    with metrics_path.open(encoding="utf-8") as file:
        definitions = yaml.safe_load(file)["metrics"]

    assert {definition["id"] for definition in definitions} == set(metrics.columns) - {
        "metric_date"
    }


def test_conversion_rate_counts_active_users_starting_paid_subscription() -> None:
    start_date = pd.Timestamp("2026-01-01")
    users = pd.DataFrame(
        {
            "user_id": [1, 2, 3],
            "register_time": [start_date, start_date, start_date],
        }
    )
    events = pd.DataFrame(
        {
            "event_id": [1, 2, 3],
            "user_id": [1, 2, 3],
            "event_time": [start_date, start_date, start_date + pd.Timedelta(days=1)],
            "event_name": ["login", "login", "login"],
            "session_id": ["s1", "s2", "s3"],
            "duration_seconds": [10.0, 10.0, 10.0],
        }
    )
    subscriptions = pd.DataFrame(
        {
            "user_id": [1, 3],
            "start_time": [start_date, start_date + pd.Timedelta(days=1)],
            "end_time": [pd.NaT, pd.NaT],
        }
    )

    metrics = generate_daily_metrics(users, events, subscriptions, start_date, 2)

    assert metrics["paid_users"].tolist() == [1, 2]
    assert metrics["conversion_rate"].tolist() == [0.5, 1.0]
    assert metrics["conversion_rate"].between(0, 1).all()


def test_session_ids_do_not_span_users_or_metric_dates() -> None:
    start_date = pd.Timestamp("2026-01-01")
    rng = np.random.default_rng(42)
    users = generate_users(100, start_date, 10, rng)
    events = generate_events(users, 5_000, start_date, 10, rng)
    events["metric_date"] = events["event_time"].dt.date

    session_owners = events.groupby("session_id").agg(
        user_count=("user_id", "nunique"),
        date_count=("metric_date", "nunique"),
    )

    assert session_owners["user_count"].eq(1).all()
    assert session_owners["date_count"].eq(1).all()


@pytest.mark.parametrize(
    ("argument", "value", "expected_error"),
    [
        ("--users", "0", "must be a positive integer"),
        ("--days", "-1", "must be a positive integer"),
        ("--events", "0", "must be a positive integer"),
        ("--start-date", "not-a-date", "invalid date"),
    ],
)
def test_cli_rejects_invalid_arguments(
    argument: str, value: str, expected_error: str
) -> None:
    result = subprocess.run(
        [sys.executable, "src/data/generator.py", argument, value],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert expected_error in result.stderr
