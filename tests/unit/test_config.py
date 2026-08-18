from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml
from pydantic import ValidationError

from benchmark.evaluation import calculate_effect, validate_effect
from config.faults import InjectionSpec
from config.metrics import MetricsConfig, load_metrics_config
from data.generator import (
    generate_daily_metrics,
    generate_events,
    generate_subscriptions,
    generate_users,
)


def test_metrics_config_has_six_unique_executable_metrics() -> None:
    config = load_metrics_config()
    assert len(config.metrics) == 6
    assert len({metric.id for metric in config.metrics}) == 6
    assert config.timezone == "UTC"
    assert config.date_grain == "day"


def test_metrics_config_rejects_invalid_timezone_and_date_grain() -> None:
    payload = load_metrics_config().model_dump()
    payload["timezone"] = "Not/A_Timezone"
    with pytest.raises(ValidationError, match="timezone"):
        MetricsConfig.model_validate(payload)

    payload = load_metrics_config().model_dump()
    payload["date_grain"] = "hour"
    with pytest.raises(ValidationError):
        MetricsConfig.model_validate(payload)


def test_metrics_config_rejects_missing_required_fields() -> None:
    payload = load_metrics_config().model_dump()
    del payload["metrics"][0]["query"]
    with pytest.raises(ValidationError, match="query"):
        MetricsConfig.model_validate(payload)


def test_metric_query_output_schema_is_validated(tmp_path: Path) -> None:
    config_path = Path(__file__).parents[2] / "config" / "metrics.yaml"
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["metrics"][0]["query"] = (
        "SELECT metric_date, 1 AS wrong_name FROM metric_dates"
    )
    broken_path = tmp_path / "broken.yaml"
    broken_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    start_date = pd.Timestamp("2026-01-01")
    users = generate_users(20, start_date, 3, np.random.default_rng(42))
    events = generate_events(users, 100, start_date, 3, np.random.default_rng(43))
    subscriptions = generate_subscriptions(
        users, start_date, 3, np.random.default_rng(44)
    )
    with pytest.raises(ValueError, match="exactly"):
        generate_daily_metrics(users, events, subscriptions, start_date, 3, broken_path)


def test_metric_query_rejects_duplicate_metric_dates(tmp_path: Path) -> None:
    config_path = Path(__file__).parents[2] / "config" / "metrics.yaml"
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["metrics"][0]["query"] = (
        "SELECT metric_date, 1 AS daily_active_users FROM metric_dates "
        "UNION ALL SELECT metric_date, 2 AS daily_active_users FROM metric_dates"
    )
    broken_path = tmp_path / "duplicate.yaml"
    broken_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    start_date = pd.Timestamp("2026-01-01")
    rng = np.random.default_rng(42)
    users = generate_users(20, start_date, 3, rng)
    events = generate_events(users, 100, start_date, 3, rng)
    subscriptions = generate_subscriptions(users, start_date, 3, rng)
    with pytest.raises(ValueError, match="duplicate metric dates"):
        generate_daily_metrics(users, events, subscriptions, start_date, 3, broken_path)


def test_metric_and_injection_specs_reject_unknown_fields() -> None:
    payload = load_metrics_config().model_dump()
    payload["metrics"][0]["time_colum"] = "event_time"
    with pytest.raises(ValidationError):
        MetricsConfig.model_validate(payload)

    with pytest.raises(ValidationError):
        InjectionSpec.model_validate({"strategy": "x", "ratto": 0.5})


def test_effect_evaluator_handles_relative_absolute_and_zero_baselines() -> None:
    assert calculate_effect(100, 75, effect_size_type="relative") == -0.25
    assert calculate_effect(0.08, 0.13, effect_size_type="absolute") == 0.05
    assert calculate_effect(0, 0, effect_size_type="relative") == 0
    assert validate_effect(
        0,
        1,
        expected_direction="increase",
        effect_size_type="relative",
        minimum_effect_size=0.2,
    )
