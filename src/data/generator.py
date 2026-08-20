from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from config.metrics import DEFAULT_METRICS_PATH, load_metrics_config

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

APP_BUILD_BY_VERSION = {"1.0.0": 100, "1.1.0": 110, "1.2.0": 120, "2.0.0": 200}


def compute_definition_hash(query: str) -> str:
    """Return the canonical SHA256 hash for a metric definition."""
    return hashlib.sha256(query.strip().encode("utf-8")).hexdigest()


def logical_dtype(dtype: object) -> str:
    """Map a Pandas dtype to the warehouse type used in schema evidence."""
    if pd.api.types.is_bool_dtype(dtype):
        return "BOOLEAN"
    if pd.api.types.is_integer_dtype(dtype):
        return "BIGINT"
    if pd.api.types.is_float_dtype(dtype):
        return "DOUBLE"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "TIMESTAMP"
    if pd.api.types.is_string_dtype(dtype) or pd.api.types.is_object_dtype(dtype):
        return "VARCHAR"
    return str(dtype).upper()


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


def _allocate_daily_counts(
    total: int, start_date: pd.Timestamp, days: int, rng: np.random.Generator
) -> np.ndarray:
    """Allocate an exact event total across a smooth business-day baseline."""
    dates = pd.date_range(start_date, periods=days, freq="D")
    weekday_factor = np.array(
        [1.05 if date.weekday() < 5 else 0.90 for date in dates], dtype=float
    )
    trend = np.linspace(1.0, 1.15, days)
    noise = rng.uniform(0.95, 1.05, days)
    weights = weekday_factor * trend * noise
    raw = total * weights / weights.sum()
    counts = np.floor(raw).astype(int)
    remainder = total - int(counts.sum())
    if remainder:
        order = np.argsort(-(raw - counts))
        counts[order[:remainder]] += 1
    return counts


def generate_users(user_count: int, start_date: pd.Timestamp, days: int, rng: np.random.Generator) -> pd.DataFrame:
    register_offsets = np.floor(rng.beta(2.0, 5.0, size=user_count) * days).astype(int)
    register_offsets = np.clip(register_offsets, 0, days - 1)
    register_offsets[0] = 0

    return pd.DataFrame(
        {
            "user_id": np.arange(1, user_count + 1),
            "register_time": start_date + pd.to_timedelta(register_offsets, unit="D"),
            "region": rng.choice(REGIONS, size=user_count, p=[0.35, 0.25, 0.2, 0.15, 0.05]),
            "device_type": rng.choice(DEVICES, size=user_count, p=[0.5, 0.25, 0.25]),
            "acquisition_channel": rng.choice(CHANNELS, size=user_count),
            "user_type": rng.choice(USER_TYPES, size=user_count, p=[0.65, 0.2, 0.15]),
            # These latent values are generated once and reused for paired
            # counterfactual subscription outcomes.
            "conversion_score": rng.random(user_count),
            "subscription_timing_score": rng.random(user_count),
            "subscription_plan_score": rng.random(user_count),
            "subscription_cancel_score": rng.random(user_count),
        }
    )


def generate_events(
    users: pd.DataFrame,
    event_count: int,
    start_date: pd.Timestamp,
    days: int,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Generate events by day, then sessions, with an exact event total."""
    user_device_map = users.set_index("user_id")["device_type"]
    daily_counts = _allocate_daily_counts(event_count, start_date, days, rng)
    user_chunks: list[np.ndarray] = []
    time_chunks: list[pd.DatetimeIndex] = []
    session_chunks: list[np.ndarray] = []
    day_chunks: list[np.ndarray] = []

    for day_offset, target_count in enumerate(daily_counts):
        if target_count == 0:
            continue
        metric_date = start_date + pd.Timedelta(days=day_offset)
        eligible = users.loc[users["register_time"] <= metric_date]
        if eligible.empty:
            raise ValueError("at least one user must be registered before each event day")
        session_count = max(1, min(target_count, round(target_count / 3)))
        activity_weight = np.array(
            eligible["user_type"].map({"free": 0.8, "trial": 1.0, "paid": 1.2}),
            dtype=float,
            copy=True,
        )
        activity_weight /= activity_weight.sum()
        owners = rng.choice(
            eligible["user_id"].to_numpy(), size=session_count, replace=True, p=activity_weight
        )
        event_counts = np.ones(session_count, dtype=int)
        if target_count > session_count:
            event_counts += rng.multinomial(
                target_count - session_count, np.full(session_count, 1 / session_count)
            )

        user_chunks.append(np.repeat(owners, event_counts))
        session_numbers = np.repeat(np.arange(session_count), event_counts)
        session_starts = np.repeat(
            rng.integers(0, SECONDS_PER_DAY - 1800, size=session_count), event_counts
        )
        offsets = rng.integers(0, 1800, size=target_count)
        time_chunks.append(
            metric_date + pd.to_timedelta(session_starts + offsets, unit="s")
        )
        session_chunks.append(session_numbers)
        day_chunks.append(np.full(target_count, day_offset, dtype=np.int64))

    user_ids = np.concatenate(user_chunks)
    event_times = pd.DatetimeIndex(np.concatenate(time_chunks))
    session_numbers = np.concatenate(session_chunks)
    event_offsets = np.concatenate(day_chunks)
    app_versions = rng.choice(
        ["1.0.0", "1.1.0", "1.2.0", "2.0.0"], size=event_count
    )
    frame = pd.DataFrame(
        {
            "event_id": np.arange(1, event_count + 1, dtype=np.int64),
            "user_id": pd.Series(user_ids, dtype="Int64"),
            "event_time": event_times,
            "event_name": rng.choice(
                EVENT_NAMES, size=event_count, p=[0.35, 0.12, 0.16, 0.22, 0.1, 0.05]
            ),
            "session_id": [
                f"s_{user_id}_{day_offset:03d}_{session_number:05d}"
                for user_id, day_offset, session_number in zip(
                    user_ids, event_offsets, session_numbers
                )
            ],
            "device_type": pd.Series(user_ids).map(user_device_map).to_numpy(),
            "duration_seconds": rng.gamma(
                shape=2.0, scale=120.0, size=event_count
            ).round(2),
            "batch_id": [f"batch_{day_offset:03d}" for day_offset in event_offsets],
            "app_version": app_versions,
            "app_build_number": pd.Series(app_versions)
            .map(APP_BUILD_BY_VERSION)
            .to_numpy(),
        }
    )
    return frame


def generate_subscriptions(
    users: pd.DataFrame,
    start_date: pd.Timestamp,
    days: int,
    rng: np.random.Generator,
    experiment_assignments: pd.DataFrame | None = None,
    events: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Generate deterministic subscription outcomes from user latent variables."""
    candidates = users.loc[users["user_type"].isin(["trial", "paid"])].copy()
    assignment_map: dict[int, str] = {}
    if experiment_assignments is not None and not experiment_assignments.empty:
        assignment_map = experiment_assignments.set_index("user_id")["variant"].to_dict()
    candidate_ids: list[int] = []
    for row in candidates.itertuples(index=False):
        base_probability = 0.72 if row.user_type == "paid" else 0.32
        variant = assignment_map.get(int(row.user_id))
        if variant == "treatment":
            base_probability += 0.20
        elif variant == "control":
            base_probability += 0.02
        conversion_score = getattr(row, "conversion_score", None)
        converted = (
            float(conversion_score) < min(base_probability, 0.95)
            if conversion_score is not None
            else rng.random() < min(base_probability, 0.95)
        )
        if converted:
            candidate_ids.append(int(row.user_id))

    paid_users = np.asarray(candidate_ids, dtype=np.int64)
    sub_count = len(paid_users)
    user_register_map = users.set_index("user_id")["register_time"]
    register_times = pd.Series(paid_users).map(user_register_map)
    register_offsets = ((register_times - start_date) / pd.Timedelta(days=1)).to_numpy(dtype=np.int64)
    available_days = days - 1 - register_offsets
    latent = candidates.set_index("user_id") if "conversion_score" in candidates else None
    if sub_count and latent is not None:
        timing_scores = latent.loc[paid_users, "subscription_timing_score"].to_numpy()
        start_offsets = register_offsets + np.floor(timing_scores * (available_days + 1)).astype(np.int64)
        plan_scores = latent.loc[paid_users, "subscription_plan_score"].to_numpy()
        plan_type = np.select(
            [plan_scores < 0.55, plan_scores < 0.90],
            ["basic", "pro"],
            default="enterprise",
        )
        cancel_scores = latent.loc[paid_users, "subscription_cancel_score"].to_numpy()
    else:
        start_offsets = register_offsets + np.array(
            [rng.integers(0, available + 1) for available in available_days]
        ) if sub_count else np.array([], dtype=np.int64)
        plan_type = rng.choice(PLANS[1:], size=sub_count, p=[0.55, 0.35, 0.10])
        cancel_scores = rng.random(sub_count)
    fee_map = {"basic": 19.0, "pro": 49.0, "enterprise": 199.0}
    subscription_status = np.where(cancel_scores < 0.15, "cancelled", "active")
    start_times = start_date + pd.to_timedelta(start_offsets, unit="D")
    if events is not None and sub_count:
        event_days = events.assign(event_day=events["event_time"].dt.normalize()).groupby(
            "user_id"
        )["event_day"].agg(lambda values: sorted(set(values)))
        selected_event_days: list[pd.Timestamp] = []
        for user_id, register_time, timing_score, fallback in zip(
            paid_users,
            register_times,
            timing_scores if latent is not None else np.zeros(sub_count),
            start_times,
        ):
            candidates = [
                day for day in event_days.get(user_id, []) if day >= pd.Timestamp(register_time)
            ]
            if candidates:
                # Timing is a fixed counterfactual basis: a conversion starts
                # on the user's latest observed active day, never a newly drawn day.
                selected_event_days.append(candidates[-1])
            else:
                selected_event_days.append(pd.Timestamp(fallback))
        start_times = pd.DatetimeIndex(selected_event_days)
    cancellation_days = 1 + np.floor(
        cancel_scores * np.maximum(1, np.minimum(30, days - start_offsets))
    ).astype(np.int64)
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
    cohort_size = int(len(users) * 0.4)
    cohort_size -= cohort_size % 2
    sampled_users = rng.choice(
        users["user_id"].to_numpy(), size=cohort_size, replace=False
    )
    user_register_map = users.set_index("user_id")["register_time"]
    register_times = pd.Series(sampled_users).map(user_register_map)
    variants = np.array(
        ["control"] * (cohort_size // 2) + ["treatment"] * (cohort_size // 2),
        dtype=object,
    )
    rng.shuffle(variants)
    return pd.DataFrame(
        {
            "experiment_id": "exp_onboarding_v1",
            "user_id": sampled_users,
            "variant": variants,
            "assigned_time": register_times.to_numpy(),
        }
    )


def _schema_json(frame: pd.DataFrame) -> str:
    return json.dumps(
        {column: logical_dtype(dtype) for column, dtype in frame.dtypes.items()},
        sort_keys=True,
    )


def generate_operational_metadata(
    users: pd.DataFrame,
    events: pd.DataFrame,
    subscriptions: pd.DataFrame,
    experiment_assignments: pd.DataFrame,
    daily_metrics: pd.DataFrame,
    start_date: pd.Timestamp,
    days: int,
    metrics_path: Path = DEFAULT_METRICS_PATH,
) -> dict[str, pd.DataFrame]:
    """Create operational evidence tables for the normal, healthy baseline."""
    dates = pd.date_range(start_date, periods=days, freq="D")
    source_tables = {
        "users": users,
        "events": events,
        "subscriptions": subscriptions,
        "experiment_assignments": experiment_assignments,
        "daily_metrics": daily_metrics,
    }
    pipeline_rows: list[dict[str, Any]] = []
    for table_name in source_tables:
        for date in dates:
            job_id = f"job_{table_name}_{date:%Y%m%d}"
            pipeline_rows.append(
                {
                    "job_id": job_id,
                    "job_name": f"load_{table_name}",
                    "run_date": date.date(),
                    "status": "success",
                    "started_at": date,
                    "finished_at": date + pd.Timedelta(minutes=10),
                    "error_type": None,
                    "error_message": None,
                    "target_table": table_name,
                    "target_partition": date.strftime("%Y-%m-%d"),
                }
            )

    event_dates = events["event_time"].dt.date
    partition_rows: list[dict[str, Any]] = []
    for date in dates:
        for device in DEVICES:
            mask = (event_dates == date.date()) & (events["device_type"] == device)
            partition_rows.append(
                {
                    "table_name": "events",
                    "partition_key": "metric_date/device_type",
                    "partition_value": f"{date:%Y-%m-%d}/{device}",
                    "row_count": int(mask.sum()),
                    "updated_at": date + pd.Timedelta(minutes=10),
                    "status": "ready",
                    "source_job_id": f"job_events_{date:%Y%m%d}",
                }
            )

    schema_rows = [
        {
            "table_name": table_name,
            "version": 1,
            "schema_json": _schema_json(frame),
            "effective_at": start_date,
        }
        for table_name, frame in source_tables.items()
    ]
    metric_config = load_metrics_config(metrics_path)
    metric_rows = [
        {
            "metric_id": metric.id,
            "version": 1,
            "definition_hash": compute_definition_hash(metric.query),
            "query": metric.query,
            "effective_at": start_date,
            "timezone": metric_config.timezone,
            "date_grain": metric_config.date_grain,
        }
        for metric in metric_config.metrics
    ]
    experiment_rows = [
        {
            "experiment_id": "exp_onboarding_v1",
            "version": 1,
            "control_ratio": 0.5,
            "treatment_ratio": 0.5,
            "hash_key": "user_id",
            "effective_at": start_date,
            "status": "active",
        }
    ]
    return {
        "pipeline_runs": pd.DataFrame(pipeline_rows),
        "partition_metadata": pd.DataFrame(partition_rows),
        "schema_snapshots": pd.DataFrame(schema_rows),
        "metric_versions": pd.DataFrame(metric_rows),
        "experiment_configs": pd.DataFrame(experiment_rows),
    }


def materialize_daily_metrics(
    tables: dict[str, pd.DataFrame],
    *,
    start_date: pd.Timestamp,
    days: int,
    query_overrides: dict[str, str] | None = None,
    metrics_path: Path = DEFAULT_METRICS_PATH,
) -> pd.DataFrame:
    """Materialize the complete metric table from source tables and SQL."""
    metric_config = load_metrics_config(metrics_path)
    metric_ids = [metric.id for metric in metric_config.metrics]
    queries = [
        (query_overrides or {}).get(metric.id, metric.query)
        for metric in metric_config.metrics
    ]

    metric_dates = pd.DataFrame(
        {"metric_date": pd.date_range(start_date, periods=days).date}
    )
    metrics = metric_dates.copy()

    with duckdb.connect(":memory:") as conn:
        for table_name, frame in tables.items():
            if table_name not in {
                "users", "events", "subscriptions", "experiment_assignments", "metric_dates"
            }:
                continue
            conn.register(table_name, frame)
        conn.register("metric_dates", metric_dates)

        for metric_id, query in zip(metric_ids, queries):
            result = conn.execute(query).df()
            expected_columns = {"metric_date", metric_id}
            if set(result.columns) != expected_columns:
                raise ValueError(
                    f"metric {metric_id!r} query must return exactly "
                    f"{sorted(expected_columns)!r}"
                )
            # TODO: normalize event timestamps with MetricsConfig.timezone before
            # applying date grain once the timezone-aware metric engine lands.
            result["metric_date"] = pd.to_datetime(result["metric_date"]).dt.date
            if result["metric_date"].duplicated().any():
                raise ValueError(
                    f"metric {metric_id!r} query returned duplicate metric dates"
                )
            metrics = metrics.merge(result, on="metric_date", how="left")

    metrics[metric_ids] = metrics[metric_ids].fillna(0)
    return metrics


def generate_daily_metrics(
    users: pd.DataFrame,
    events: pd.DataFrame,
    subscriptions: pd.DataFrame,
    start_date: pd.Timestamp,
    days: int,
    metrics_path: Path = DEFAULT_METRICS_PATH,
) -> pd.DataFrame:
    """Calculate every daily metric from canonical SQL in metrics.yaml."""
    return materialize_daily_metrics(
        {
            "users": users,
            "events": events,
            "subscriptions": subscriptions,
        },
        start_date=start_date,
        days=days,
        metrics_path=metrics_path,
    )


def generate_dataset(
    user_count: int,
    days: int,
    event_count: int,
    seed: int,
    start_date: pd.Timestamp,
    metrics_path: Path = DEFAULT_METRICS_PATH,
) -> dict[str, pd.DataFrame]:
    """Generate all source, metric, and operational metadata tables reproducibly."""
    rng = np.random.default_rng(seed)
    users = generate_users(user_count, start_date, days, rng)
    experiment_assignments = generate_experiment_assignments(
        users, start_date, days, rng
    )
    events = generate_events(users, event_count, start_date, days, rng)
    subscriptions = generate_subscriptions(
        users, start_date, days, rng, experiment_assignments, events
    )
    daily_metrics = generate_daily_metrics(
        users, events, subscriptions, start_date, days, metrics_path
    )
    metadata = generate_operational_metadata(
        users,
        events,
        subscriptions,
        experiment_assignments,
        daily_metrics,
        start_date,
        days,
        metrics_path,
    )
    return {
        "users": users,
        "events": events,
        "subscriptions": subscriptions,
        "experiment_assignments": experiment_assignments,
        "daily_metrics": daily_metrics,
        **metadata,
    }


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

    start_date = args.start_date
    tables = generate_dataset(args.users, args.days, args.events, args.seed, start_date)

    write_outputs(args.output_dir, tables)

    print("SaaS mock data generated successfully.")
    print(f"Output directory: {args.output_dir}")
    print(f"Users: {len(tables['users'])}")
    print(f"Events: {len(tables['events'])}")
    print(f"Daily metrics rows: {len(tables['daily_metrics'])}")


if __name__ == "__main__":
    main()
