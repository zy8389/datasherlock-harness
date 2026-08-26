import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from agents.planner import Alert
from benchmark.case_generator import load_case_manifests, materialize_case
from benchmark.cases import concrete_case_from_manifest
from benchmark.evaluation import validate_effect
from benchmark.fault_injector import validate_expected_evidence
from config.faults import INDEPENDENT_METADATA_EVIDENCE_FAULT_IDS
from data.generator import generate_dataset

ROOT = Path(__file__).parents[2]
CASES_DIRECTORY = ROOT / "benchmark" / "cases"


@pytest.fixture(scope="module")
def manifests():
    return load_case_manifests(CASES_DIRECTORY)


@pytest.fixture(scope="module")
def baseline(manifests: list[object]) -> dict[str, pd.DataFrame]:
    first = manifests[0]
    return generate_dataset(
        first.baseline_user_count,
        first.baseline_days,
        first.baseline_event_count,
        first.baseline_seed,
        pd.Timestamp(first.baseline_start_date),
    )


def _tables_digest(tables: dict[str, pd.DataFrame]) -> str:
    digest = hashlib.sha256()
    for table_name in sorted(tables):
        frame = tables[table_name]
        digest.update(table_name.encode("utf-8"))
        digest.update(json.dumps(list(frame.columns)).encode("utf-8"))
        digest.update(
            frame.to_json(
                orient="split",
                date_format="iso",
                date_unit="us",
                default_handler=str,
            ).encode("utf-8")
        )
    return digest.hexdigest()


def test_all_60_cases_materialize(
    manifests: list[object],
    baseline: dict[str, pd.DataFrame],
) -> None:
    assert len(manifests) == 60
    results = []
    for manifest in manifests:
        result = materialize_case(manifest, baseline_tables=baseline)
        results.append(result)
        assert set(result.tables) >= {
            "events",
            "daily_metrics",
            "pipeline_runs",
            "partition_metadata",
            "schema_snapshots",
            "metric_versions",
            "experiment_configs",
        }
        assert result.case_id == manifest.case_id
        assert result.ground_truth_case == concrete_case_from_manifest(manifest)
    assert len(results) == 60


def test_all_60_cases_satisfy_effect_and_evidence_contracts(
    manifests: list[object],
    baseline: dict[str, pd.DataFrame],
) -> None:
    for manifest in manifests:
        result = materialize_case(manifest, baseline_tables=baseline)
        validate_expected_evidence(
            result,
            baseline,
            concrete_case_from_manifest(manifest),
        )
        assert result.actual_effect is not None
        assert result.actual_effect == pytest.approx(manifest.actual_effect)
        assert validate_effect(
            manifest.original_alert.expected_value,
            manifest.original_alert.observed_value,
            expected_direction=manifest.expected_direction,
            effect_size_type=manifest.effect_size_type,
            minimum_effect_size=manifest.minimum_effect_size,
        )
        assert manifest.affected_row_count > 0
        assert manifest.actual_effect != 0
        if manifest.fault_id in INDEPENDENT_METADATA_EVIDENCE_FAULT_IDS:
            assert len(manifest.evidence_paths) >= 2


def test_all_60_cases_have_original_alerts(
    manifests: list[object],
) -> None:
    for manifest in manifests:
        alert = Alert.model_validate(manifest.original_alert.model_dump(mode="json"))
        assert alert.metric == manifest.affected_metric
        assert alert.observed_at == manifest.metric_date.isoformat()
        assert alert.expected_value != alert.observed_value
        assert alert.change_rate != 0
        assert alert.severity == manifest.severity
        assert alert.severity
        runtime_alert = json.dumps(alert.model_dump(mode="json"), sort_keys=True).lower()
        assert "root_cause_type" not in runtime_alert
        assert "expected_evidence" not in runtime_alert
        assert "ground_truth" not in runtime_alert


@pytest.mark.parametrize("case_id", ["F01-003", "F05-004", "F11-005"])
def test_repeated_materialization_is_stable(
    case_id: str,
    manifests: list[object],
    baseline: dict[str, pd.DataFrame],
) -> None:
    manifest = next(item for item in manifests if item.case_id == case_id)
    first = materialize_case(manifest, baseline_tables=baseline)
    second = materialize_case(manifest, baseline_tables=baseline)

    assert _tables_digest(first.tables) == _tables_digest(second.tables)
    assert first.actual_effect == second.actual_effect
    assert first.case_id == second.case_id == case_id
    assert manifest.original_alert.model_dump(mode="json") == manifest.original_alert.model_dump(
        mode="json"
    )
