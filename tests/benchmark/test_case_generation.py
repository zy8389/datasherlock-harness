import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from benchmark.case_generator import (
    generate_case_manifests,
    load_variant_config,
    severity_for_effect,
    validate_case_manifest,
)
from benchmark.cases import manifest_payload
from benchmark.fault_injector import inject_case, validate_expected_evidence
from config.faults import (
    INDEPENDENT_METADATA_EVIDENCE_FAULT_IDS,
    EvidenceSourceType,
    load_fault_catalog,
    load_ground_truth_cases,
    validate_ground_truth_case,
)
from data.generator import generate_dataset

ROOT = Path(__file__).parents[2]
GROUND_TRUTH_DIRECTORY = ROOT / "benchmark" / "ground_truth"
CASES_DIRECTORY = ROOT / "benchmark" / "cases"


@pytest.fixture(scope="module")
def variant_config():
    return load_variant_config(CASES_DIRECTORY / "variants.yaml")


@pytest.fixture(scope="module")
def baseline(variant_config: object) -> dict[str, pd.DataFrame]:
    config = variant_config.baseline
    return generate_dataset(
        config.user_count,
        config.days,
        config.event_count,
        config.seed,
        pd.Timestamp(config.start_date),
    )


@pytest.fixture(scope="module")
def generated_manifests(
    baseline: dict[str, pd.DataFrame],
):
    return generate_case_manifests(baseline_tables=baseline)


def test_all_12_seed_cases_validate_against_catalog() -> None:
    catalog = load_fault_catalog()
    cases = load_ground_truth_cases(GROUND_TRUTH_DIRECTORY, catalog)

    assert len(cases) == 12
    assert {case.fault_id for case in cases} == {
        f"F{number:02d}" for number in range(1, 13)
    }
    assert {case.case_id for case in cases} == {
        f"F{number:02d}-001" for number in range(1, 13)
    }
    for case in cases:
        validate_ground_truth_case(case, catalog)
        fault = catalog.by_id(case.fault_id)
        assert case.root_cause_type == fault.root_cause_type
        assert case.affected_metric in fault.affected_metrics
        assert set(case.affected_assets) == set(fault.affected_assets)
        assert case.injection.strategy == fault.injection_strategy
        assert case.expected_direction == fault.expected_direction
        assert case.effect_size_type == fault.effect_size_type
        assert case.minimum_effect_size == fault.minimum_effect_size


@pytest.mark.parametrize("case_id", [f"F{number:02d}-001" for number in range(1, 13)])
def test_all_12_seed_cases_materialize_and_satisfy_contract(
    case_id: str,
    baseline: dict[str, pd.DataFrame],
) -> None:
    seed_cases = {
        case.case_id: case
        for case in load_ground_truth_cases(GROUND_TRUTH_DIRECTORY)
    }
    case = seed_cases[case_id]
    config = load_variant_config(CASES_DIRECTORY / "variants.yaml").baseline
    result = inject_case(
        baseline,
        case,
        rng=np.random.default_rng(99),
        start_date=pd.Timestamp(config.start_date),
        days=config.days,
    )

    validate_expected_evidence(result, baseline, case)
    assert result.tables["daily_metrics"].shape[0] == config.days
    assert result.actual_effect is not None
    assert result.ground_truth_case is case


def test_generator_produces_exactly_60_cases(
    generated_manifests: list[object],
) -> None:
    assert len(generated_manifests) == 60
    assert len({manifest.case_id for manifest in generated_manifests}) == 60
    assert {manifest.fault_id for manifest in generated_manifests} == {
        f"F{number:02d}" for number in range(1, 13)
    }
    assert Counter(manifest.fault_id for manifest in generated_manifests) == {
        f"F{number:02d}": 5 for number in range(1, 13)
    }


def test_generated_variants_are_not_identical_within_fault_family(
    generated_manifests: list[object],
) -> None:
    for fault_id in {manifest.fault_id for manifest in generated_manifests}:
        family = [
            manifest for manifest in generated_manifests if manifest.fault_id == fault_id
        ]
        signatures = {
            json.dumps(
                {
                    "seed": manifest.seed,
                    "metric_date": manifest.metric_date.isoformat(),
                    "injection": manifest.injection.model_dump(mode="json"),
                },
                sort_keys=True,
            )
            for manifest in family
        }
        assert len(signatures) == 5, fault_id


def test_generated_cases_match_canonical_ground_truth(
    generated_manifests: list[object],
) -> None:
    catalog = load_fault_catalog()
    seeds = {
        case.case_id: case
        for case in load_ground_truth_cases(GROUND_TRUTH_DIRECTORY, catalog)
    }
    for manifest in generated_manifests:
        seed = seeds[manifest.source_seed_case_id]
        assert manifest.fault_id == seed.fault_id
        assert manifest.root_cause_type == seed.root_cause_type
        assert manifest.affected_metric == seed.affected_metric
        assert manifest.affected_assets == seed.affected_assets
        assert manifest.injection.strategy == seed.injection.strategy
        assert manifest.expected_direction == seed.expected_direction
        assert manifest.effect_size_type == seed.effect_size_type
        assert manifest.minimum_effect_size == seed.minimum_effect_size
        if manifest.variant_index == 1:
            assert manifest.injection == seed.injection
        assert manifest.original_alert.metric == manifest.affected_metric
        assert manifest.original_alert.observed_at == manifest.metric_date.isoformat()
        assert manifest.original_alert.change_rate == pytest.approx(
            manifest.actual_effect
        )
        assert manifest.severity == severity_for_effect(manifest.actual_effect)

        if manifest.fault_id in INDEPENDENT_METADATA_EVIDENCE_FAULT_IDS:
            sources = {path.source_type for path in manifest.evidence_paths}
            assert EvidenceSourceType.BUSINESS_DATA in sources
            assert len(sources) >= 2


def test_generation_is_deterministic_and_matches_committed_manifests(
    generated_manifests: list[object],
) -> None:
    committed = {
        path.stem: path.read_text(encoding="utf-8")
        for path in CASES_DIRECTORY.glob("F??-???.yaml")
    }
    assert set(committed) == {manifest.case_id for manifest in generated_manifests}
    for manifest in generated_manifests:
        expected = manifest_payload(manifest)
        import yaml

        assert yaml.safe_load(committed[manifest.case_id]) == expected


def test_future_manifest_schema_fails_closed(generated_manifests: list[object]) -> None:
    future_manifest = generated_manifests[0].model_copy(update={"schema_version": 2})
    with pytest.raises(ValueError, match="unsupported manifest schema_version"):
        validate_case_manifest(future_manifest)
