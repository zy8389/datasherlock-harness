from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

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
    session_slots = rng.integers(1, 4, size=event_count)

    return pd.DataFrame(
        {
            "event_id": np.arange(1, event_count + 1),
            "user_id": user_ids,
            "event_time": event_times,
            "event_name": rng.choice(EVENT_NAMES, size=event_count, p=[0.35, 0.12, 0.16, 0.22, 0.1, 0.05]),
            "session_id": [
                f"s_{user_id}_{day_offset:03d}_{slot}"
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
) -> pd.DataFrame:
    events = events.copy()
    events["metric_date"] = events["event_time"].dt.date

    users = users.copy()
    users["metric_date"] = users["register_time"].dt.date

    subscriptions = subscriptions.copy()
    all_dates = pd.DataFrame({"metric_date": pd.date_range(start_date, periods=days).date})

    dau = events.groupby("metric_date")["user_id"].nunique().rename("daily_active_users")
    new_users = users.groupby("metric_date")["user_id"].nunique().rename("new_users")
    metric_timestamps = pd.to_datetime(all_dates["metric_date"])
    subscription_starts = subscriptions["start_time"].dt.normalize()
    subscription_ends = subscriptions["end_time"].dt.normalize()
    paid_users = pd.Series(
        [
            subscriptions.loc[
                (subscription_starts <= metric_date)
                & (subscription_ends.isna() | (subscription_ends > metric_date)),
                "user_id",
            ].nunique()
            for metric_date in metric_timestamps
        ],
        index=all_dates.index,
        name="paid_users",
    )
    ai_task_count = events.loc[events["event_name"] == "run_ai_task"].groupby("metric_date")["event_id"].count().rename("ai_task_count")
    subscription_start_dates = subscriptions["start_time"].dt.date
    newly_paid_users = (
        subscriptions.assign(metric_date=subscription_start_dates)[["metric_date", "user_id"]]
        .drop_duplicates()
    )
    active_users = events[["metric_date", "user_id"]].drop_duplicates()
    converted_users = (
        active_users.merge(newly_paid_users, on=["metric_date", "user_id"], how="inner")
        .groupby("metric_date")["user_id"]
        .nunique()
        .rename("_converted_users")
    )
    session_durations = (
        events.groupby(["metric_date", "user_id", "session_id"], as_index=False)["duration_seconds"]
        .sum()
    )
    avg_duration = (
        session_durations.groupby("metric_date")["duration_seconds"]
        .mean()
        .rename("average_session_duration")
    )

    metrics = all_dates.merge(dau, on="metric_date", how="left")
    metrics = metrics.merge(new_users, on="metric_date", how="left")
    metrics = metrics.join(paid_users)
    metrics = metrics.merge(ai_task_count, on="metric_date", how="left")
    metrics = metrics.merge(avg_duration, on="metric_date", how="left")
    metrics = metrics.merge(converted_users, on="metric_date", how="left")

    metrics = metrics.fillna(0)
    metrics["conversion_rate"] = np.where(
        metrics["daily_active_users"] > 0,
        metrics["_converted_users"] / metrics["daily_active_users"],
        0,
    )

    return metrics.drop(columns="_converted_users")


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
