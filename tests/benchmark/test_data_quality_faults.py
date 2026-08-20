from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from benchmark.fault_injector import inject_fault
from data.generator import generate_dataset, write_outputs
from tools.data_quality import DataQualityScope, check_null_rate


def test_check_null_rate_detects_f03_in_mobile_target_window(tmp_path: Path) -> None:
    """F03 must be checked in its affected device and date scope, not globally."""

    start_date = pd.Timestamp("2026-01-01")
    target_date = date(2026, 1, 30)
    baseline = generate_dataset(500, 30, 10_000, 42, start_date)
    fault = inject_fault(
        baseline,
        "F03",
        target_date,
        rng=np.random.default_rng(99),
        start_date=start_date,
        days=30,
    )
    write_outputs(tmp_path, fault.tables)

    result = check_null_rate(
        tmp_path / "datasherlock.duckdb",
        "events",
        "user_id",
        threshold=0.01,
        scope=DataQualityScope(
            equals={"device_type": ["ios", "android"]},
            time_column="event_time",
            start=datetime(2026, 1, 30, tzinfo=UTC),
            end=datetime(2026, 1, 31, tzinfo=UTC),
        ),
    )

    assert result.status == "success"
    assert result.passed is False
    assert result.observed_value is not None
    assert result.observed_value > 0.01
    assert result.evidence[0].details["scope"]["equals"] == {
        "device_type": ["ios", "android"]
    }
