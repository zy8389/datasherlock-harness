from pathlib import Path
import subprocess
import sys

import duckdb
import numpy as np
import pandas as pd

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
