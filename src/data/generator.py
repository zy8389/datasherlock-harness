from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

EVENT_NAMES = [
    "login",
    "create_project",
    "upload_file",
    "run_ai_task",
    "export_result",
    "invite_member",
]

SECONDS_PER_DAY = 24 * 60 * 60

REGIONS = ["CN", "US", "EU", "SEA", "JP"]
DEVICES = ["web", "ios", "android"]
CHANNELS = ["organic", "ads", "referral", "partner"]
USER_TYPES = ["free", "trial", "paid"]
PLANS = ["free", "basic", "pro", "enterprise"]

DEFAULT_METRICS_PATH = Path(__file__).parents[2] / "config" / "metrics.yaml"


def positive_int(value: str) -> int:
    parsed_value = int(value)
    if parsed_value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed_value


def parse_start_date(value: str) -> pd.Timestamp:
    try:
        start_date = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError(f"invalid date: {value!r}") from exc
    if pd.isna(start_date):
        raise argparse.ArgumentTypeError(f"invalid date: {value!r}")
    return start_date


def generate_users(user_count: int, start_date: pd.Timestamp, days: int, rng: np.random.Generator) -> pd.DataFrame:
    register_offsets = rng.integers(0, days, size=user_count)

    return pd.DataFrame(
        {
            "user_id": np.arange(1, user_count + 1),
            "register_time": start_date + pd.to_timedelta(register_offsets, unit="D"),
            "region": rng.choice(REGIONS, size=user_count, p=[0.35, 0.25, 0.2, 0.15, 0.05]),
            "device_type": rng.choice(DEVICES, size=user_count, p=[0.5, 0.25, 0.25]),
            "acquisition_channel": rng.choice(CHANNELS, size=user_count),
            "user_type": rng.choice(USER_TYPES, size=user_count, p=[0.65, 0.2, 0.15]),
        }
    )


def generate_events(
    users: pd.DataFrame,
    event_count: int,
    start_date: pd.Timestamp,
    days: int,
    rng: np.random.Generator,
) -> pd.DataFrame:
    user_ids = rng.choice(users["user_id"].to_numpy(), size=event_count)

    user_device_map = users.set_index("user_id")["device_type"]
    user_register_map = users.set_index("user_id")["register_time"]
    device_type = pd.Series(user_ids).map(user_device_map).to_numpy()
    register_times = pd.Series(user_ids).map(user_register_map)
    register_offsets = ((register_times - start_date) / pd.Timedelta(seconds=1)).to_numpy(dtype=np.int64)
    remaining_seconds = days * SECONDS_PER_DAY - register_offsets - 1
    event_delays = np.array([rng.integers(0, remaining + 1) for remaining in remaining_seconds])
    event_times = register_times + pd.to_timedelta(event_delays, unit="s")
    event_offsets = ((event_times - start_date) / pd.Timedelta(days=1)).to_numpy(dtype=np.int64)
    session_slots = (event_times.dt.hour * 2 + event_times.dt.minute // 30).to_numpy()

    return pd.DataFrame(
        {
            "event_id": np.arange(1, event_count + 1),
            "user_id": user_ids,
            "event_time": event_times,
            "event_name": rng.choice(EVENT_NAMES, size=event_count, p=[0.35, 0.12, 0.16, 0.22, 0.1, 0.05]),
            "session_id": [
                f"s_{user_id}_{day_offset:03d}_{slot:02d}"
                for user_id, day_offset, slot in zip(user_ids, event_offsets, session_slots)
            ],
            "device_type": device_type,
            "duration_seconds": rng.gamma(shape=2.0, scale=120.0, size=event_count).round(2),
            "batch_id": [f"batch_{d:03d}" for d in event_offsets],
            "app_version": rng.choice(["1.0.0", "1.1.0", "1.2.0", "2.0.0"], size=event_count),
        }
    )


def generate_subscriptions(users: pd.DataFrame, start_date: pd.Timestamp, days: int, rng: np.random.Generator) -> pd.DataFrame:
    paid_users = users.loc[users["user_type"].isin(["trial", "paid"]), "user_id"].to_numpy()
    sub_count = len(paid_users)

    user_register_map = users.set_index("user_id")["register_time"]
    register_times = pd.Series(paid_users).map(user_register_map)
    register_offsets = ((register_times - start_date) / pd.Timedelta(days=1)).to_numpy(dtype=np.int64)
    available_days = days - 1 - register_offsets
    start_offsets = register_offsets + np.array(
        [rng.integers(0, available + 1) for available in available_days]
    )
    plan_type = rng.choice(PLANS[1:], size=sub_count, p=[0.55, 0.35, 0.10])
    fee_map = {"basic": 19.0, "pro": 49.0, "enterprise": 199.0}
    subscription_status = rng.choice(["active", "cancelled"], size=sub_count, p=[0.85, 0.15])
    start_times = start_date + pd.to_timedelta(start_offsets, unit="D")
    cancellation_days = np.array(
        [rng.integers(1, max(1, min(30, days - offset)) + 1) for offset in start_offsets]
    )
    end_times = start_times + pd.to_timedelta(cancellation_days, unit="D")
    end_times = pd.Series(end_times).where(subscription_status == "cancelled", pd.NaT)

    return pd.DataFrame(
        {
            "subscription_id": np.arange(1, sub_count + 1),
            "user_id": paid_users,
            "plan_type": plan_type,
            "start_time": start_times,
            "end_time": end_times,
            "subscription_status": subscription_status,
            "monthly_fee": [fee_map[p] for p in plan_type],
        }
    )


def generate_experiment_assignments(
    users: pd.DataFrame,
    start_date: pd.Timestamp,
    days: int,
    rng: np.random.Generator,
) -> pd.DataFrame:
    sampled_users = rng.choice(users["user_id"].to_numpy(), size=int(len(users) * 0.4), replace=False)
    user_register_map = users.set_index("user_id")["register_time"]
    register_times = pd.Series(sampled_users).map(user_register_map)
    register_offsets = ((register_times - start_date) / pd.Timedelta(days=1)).to_numpy(dtype=np.int64)
    available_days = days - 1 - register_offsets
    assignment_offsets = register_offsets + np.array(
        [rng.integers(0, available + 1) for available in available_days]
    )

    return pd.DataFrame(
        {
            "experiment_id": "exp_onboarding_v1",
            "user_id": sampled_users,
            "variant": rng.choice(["control", "treatment"], size=len(sampled_users), p=[0.5, 0.5]),
            "assigned_time": start_date + pd.to_timedelta(assignment_offsets, unit="D"),
        }
    )


def generate_daily_metrics(
    users: pd.DataFrame,
    events: pd.DataFrame,
    subscriptions: pd.DataFrame,
    start_date: pd.Timestamp,
    days: int,
    metrics_path: Path = DEFAULT_METRICS_PATH,
) -> pd.DataFrame:
    """Calculate every daily metric from the canonical SQL in metrics.yaml."""
    with metrics_path.open(encoding="utf-8") as file:
        config = yaml.safe_load(file)

    definitions = config.get("metrics") if isinstance(config, dict) else None
    if not isinstance(definitions, list) or not definitions:
        raise ValueError("metrics config must contain a non-empty 'metrics' list")

    metric_ids: list[str] = []
    queries: list[str] = []
    for definition in definitions:
        if not isinstance(definition, dict):
            raise TypeError("each metric definition must be a mapping")
        metric_id = definition.get("id")
        query = definition.get("query")
        if not isinstance(metric_id, str) or not metric_id:
            raise ValueError("each metric definition must have a non-empty string id")
        if metric_id in metric_ids:
            raise ValueError(f"duplicate metric id: {metric_id}")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"metric {metric_id!r} must have a non-empty query")
        metric_ids.append(metric_id)
        queries.append(query)

    metric_dates = pd.DataFrame(
        {"metric_date": pd.date_range(start_date, periods=days).date}
    )
    metrics = metric_dates.copy()

    with duckdb.connect(":memory:") as conn:
        conn.register("metric_dates", metric_dates)
        conn.register("users", users)
        conn.register("events", events)
        conn.register("subscriptions", subscriptions)

        for metric_id, query in zip(metric_ids, queries):
            result = conn.execute(query).df()
            expected_columns = {"metric_date", metric_id}
            if set(result.columns) != expected_columns:
                raise ValueError(
                    f"metric {metric_id!r} query must return exactly "
                    f"{sorted(expected_columns)!r}"
                )
            result["metric_date"] = pd.to_datetime(result["metric_date"]).dt.date
            if result["metric_date"].duplicated().any():
                raise ValueError(
                    f"metric {metric_id!r} query returned duplicate metric dates"
                )
            metrics = metrics.merge(result, on="metric_date", how="left")

    metrics[metric_ids] = metrics[metric_ids].fillna(0)
    return metrics


def write_outputs(output_dir: Path, tables: dict[str, pd.DataFrame]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    for table_name, df in tables.items():
        df.to_parquet(output_dir / f"{table_name}.parquet", index=False)

    db_path = output_dir / "datasherlock.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        for table_name, df in tables.items():
            conn.register(table_name, df)
            conn.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM {table_name}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=positive_int, default=20_000)
    parser.add_argument("--days", type=positive_int, default=180)
    parser.add_argument("--events", type=positive_int, default=1_000_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--start-date", type=parse_start_date, default=pd.Timestamp("2026-01-01"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed"))
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    start_date = args.start_date

    users = generate_users(args.users, start_date, args.days, rng)
    events = generate_events(users, args.events, start_date, args.days, rng)
    subscriptions = generate_subscriptions(users, start_date, args.days, rng)
    experiment_assignments = generate_experiment_assignments(users, start_date, args.days, rng)
    daily_metrics = generate_daily_metrics(users, events, subscriptions, start_date, args.days)

    tables = {
        "users": users,
        "events": events,
        "subscriptions": subscriptions,
        "experiment_assignments": experiment_assignments,
        "daily_metrics": daily_metrics,
    }

    write_outputs(args.output_dir, tables)

    print("SaaS mock data generated successfully.")
    print(f"Output directory: {args.output_dir}")
    print(f"Users: {len(users)}")
    print(f"Events: {len(events)}")
    print(f"Daily metrics rows: {len(daily_metrics)}")


if __name__ == "__main__":
    main()
