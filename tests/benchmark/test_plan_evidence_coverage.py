import hashlib
import json

from benchmark.plan_evidence_coverage import (
    audit_frozen_plan_evidence_coverage,
    render_frozen_plan_coverage_markdown,
)


def _hypothesis(hypothesis_id: str, root_cause_type: str) -> dict[str, object]:
    return {
        "hypothesis_id": hypothesis_id,
        "root_cause_type": root_cause_type,
        "description": "deterministic audit candidate",
        "initial_confidence": 0.2,
    }


def _step(step_id: str, sql: str) -> dict[str, object]:
    return {
        "step_id": step_id,
        "purpose": "inspect one source",
        "hypothesis_id": "H01",
        "tool": "sql_query",
        "arguments": {"sql": sql},
        "expected_evidence": ["one structured observation"],
        "stop_condition": "stop after the bounded observation",
    }


def _record(
    case_id: str,
    fault_id: str,
    root_cause_type: str,
    steps: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "fault_id": fault_id,
        "variant": "full_harness",
        "status": "completed",
        "completion_status": "UNRESOLVED",
        "expected_root_cause": root_cause_type,
        "top1_correct": False,
        "top3_correct": False,
        "abstention": True,
        "trace_payload": {
            "planner": {
                "plan": {
                    "incident_id": f"INC-{case_id}",
                    "hypotheses": [
                        _hypothesis("H01", root_cause_type),
                        _hypothesis("H02", "duplicate_batch"),
                        _hypothesis("H03", "null_value_anomaly"),
                    ],
                    "steps": steps,
                }
            }
        },
    }


def test_frozen_plan_coverage_audit_counts_complete_and_missing_sources(
    tmp_path,
) -> None:
    records = [
        _record(
            "F10-SYNTHETIC",
            "F10",
            "schema_change",
            [
                _step("S01", "SELECT COUNT(*) FROM events"),
                _step("S02", "SELECT * FROM schema_snapshots"),
            ],
        ),
        _record(
            "F05-SYNTHETIC",
            "F05",
            "timezone_error",
            [_step("S01", "SELECT COUNT(*) FROM events")],
        ),
    ]
    raw = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    path = tmp_path / "results.jsonl"
    path.write_text(raw, encoding="utf-8")

    audit = audit_frozen_plan_evidence_coverage(path, run_id="synthetic-run")

    assert audit.raw_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert audit.record_count == 2
    assert audit.golden_hypothesis_present_cases == 2
    assert audit.declared_multisource_golden_candidates == 2
    assert audit.coverage_complete == 1
    assert audit.coverage_incomplete == 1
    assert audit.missing_source_counts["metric_version"] == 1
    assert audit.missing_source_counts["schema_metadata"] == 0
    assert next(
        item for item in audit.cases if item.fault_id == "F10"
    ).planned_sources == [
        "business_data",
        "schema_metadata",
    ]
    f11 = next(item for item in audit.per_fault if item.fault_id == "F11")
    assert f11.candidates == 0

    report = render_frozen_plan_coverage_markdown(audit)
    assert "Coverage complete | 1" in report
    assert "`F11` | `metric_definition_change` | 0" in report
    assert "does not execute a model" in report
