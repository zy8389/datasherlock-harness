"""Read-only validation of completed sandbox repairs."""

from __future__ import annotations

import hmac
import math
import re
from datetime import date
from pathlib import Path
from uuid import uuid4

from config.metrics import DEFAULT_METRICS_PATH, MetricDefinition, load_metrics_config
from harness.repair import (
    PostValidationResult,
    PostValidationStatus,
    RepairAction,
    RepairProposal,
    SandboxRun,
    SandboxRunStatus,
    proposal_is_intact,
)
from tools.sql_runner import SqlExecutionResponse, execute_readonly_sql
from validators.sql_result import (
    MetricAggregation,
    NumericRange,
    SqlResultExpectation,
    validate_sql_result,
)


class PostRepairValidationError(ValueError):
    """Raised when the validation request itself is invalid or unbound."""


class PostRepairValidator:
    """Validate a repaired F01 sandbox through the current read-only SQL path."""

    def __init__(
        self,
        source_database_path: str | Path,
        *,
        metrics_path: str | Path = DEFAULT_METRICS_PATH,
    ) -> None:
        self._source_database_path = self._existing_file(
            source_database_path, "source_database_path"
        )
        config = load_metrics_config(metrics_path)
        self._metrics = {metric.id: metric for metric in config.metrics}

    def validate(
        self,
        proposal: RepairProposal,
        run: SandboxRun,
        *,
        metric_id: str,
        metric_date: date,
        expected_value: float,
        allowed_relative_error: float = 0.05,
        regression_metric_ids: tuple[str, ...] = (),
        max_regression_ratio: float = 0.05,
        validation_id: str | None = None,
    ) -> PostValidationResult:
        """Return a result only after validating proposal/run identity bindings."""

        self._validate_binding(proposal, run)
        if proposal.action is not RepairAction.RERUN_PARTITION:
            raise PostRepairValidationError("no post-validation contract exists for this action")
        self._validate_ratio(allowed_relative_error, "allowed_relative_error")
        self._validate_ratio(max_regression_ratio, "max_regression_ratio")
        if (
            isinstance(expected_value, bool)
            or not isinstance(expected_value, (int, float))
            or not math.isfinite(expected_value)
        ):
            raise PostRepairValidationError("expected_value must be a finite number")
        target_metric = self._metric(metric_id)
        regression_ids = self._validate_regression_metric_ids(
            regression_metric_ids, metric_id
        )
        sandbox_database_path = self._existing_file(run.sandbox_path, "sandbox_path")
        partition_date, device_type, partition_value = self._partition_scope(proposal)
        if metric_date != partition_date:
            raise PostRepairValidationError(
                "metric_date must match the approved partition scope"
            )

        observed_before: float | None = None
        observed_after: float | None = None
        failures: list[str] = []
        try:
            observed_before = self._measure(
                self._source_database_path, target_metric, metric_date
            )
            observed_after = self._measure(
                sandbox_database_path, target_metric, metric_date
            )
        except PostRepairValidationError as exc:
            failures.append(str(exc))

        if observed_before is None:
            observed_before = 0.0
        if observed_after is None:
            observed_after = 0.0
        target_met = (
            not failures
            and self._within_relative_error(
                observed_after, float(expected_value), allowed_relative_error
            )
        )

        if not self._partition_events_healthy(
            sandbox_database_path, partition_date, device_type
        ):
            failures.append("target business partition was not restored")
            target_met = False
        if not self._partition_metadata_healthy(sandbox_database_path, partition_value):
            failures.append("target partition metadata is not healthy")
            target_met = False
        if not self._unaffected_events_unchanged(
            self._source_database_path,
            sandbox_database_path,
            partition_date,
            device_type,
        ):
            failures.append("events outside the repaired partition regressed")

        regressions: list[str] = list(failures)
        for regression_id in regression_ids:
            try:
                metric = self._metric(regression_id)
                before = self._measure(
                    self._source_database_path, metric, metric_date
                )
                after = self._measure(sandbox_database_path, metric, metric_date)
            except PostRepairValidationError as exc:
                regressions.append(f"{regression_id}: validation query failed: {exc}")
                continue
            if before > 0 and after < before * (1 - max_regression_ratio):
                regressions.append(
                    f"{regression_id}: {before:g} -> {after:g} exceeds "
                    f"{max_regression_ratio:.0%} allowed decrease"
                )

        status = (
            PostValidationStatus.PASSED
            if target_met and not regressions
            else PostValidationStatus.FAILED
        )
        summary = (
            f"{metric_id} changed from {observed_before:g} to {observed_after:g}; "
            f"expected {float(expected_value):g} within {allowed_relative_error:.0%}."
        )
        if regressions:
            summary += " Validation failures: " + "; ".join(regressions)
        else:
            summary += " Target partition and configured checks are healthy."
        return PostValidationResult(
            validation_id=validation_id or f"PV-{uuid4()}",
            incident_id=proposal.incident_id,
            sandbox_run_id=run.run_id,
            proposal_hash=proposal.proposal_hash,
            metric_id=metric_id,
            observed_before=observed_before,
            observed_after=observed_after,
            target_met=target_met,
            regressions=tuple(regressions),
            status=status,
            summary=summary,
        )

    def _measure(
        self,
        database_path: Path,
        metric: MetricDefinition,
        metric_date: date,
    ) -> float:
        response = execute_readonly_sql(
            database_path,
            metric.query,
            max_rows=metric.validation.max_result_rows,
        )
        expectation = SqlResultExpectation(
            required_columns=list(metric.validation.expected_column_types),
            expected_column_types=dict(metric.validation.expected_column_types),
            numeric_ranges={
                column: NumericRange(
                    minimum=limits.minimum,
                    maximum=limits.maximum,
                )
                for column, limits in metric.validation.numeric_ranges.items()
            },
            expected_aggregation=_aggregation(metric.aggregation),
            max_result_rows=metric.validation.max_result_rows,
        )
        validation = validate_sql_result(response, expectation, sql=metric.query)
        if not validation.passed:
            raise PostRepairValidationError(
                f"could not measure {metric.id}: {validation.reason.value if validation.reason else 'invalid result'}"
            )
        date_index = _column_index(response, "metric_date", metric.id)
        metric_index = _column_index(response, metric.id, metric.id)
        matches = [
            row
            for row in response.rows
            if _matches_date(row[date_index], metric_date)
        ]
        if len(matches) != 1:
            raise PostRepairValidationError(
                f"metric {metric.id} returned {len(matches)} rows for {metric_date.isoformat()}"
            )
        value = matches[0][metric_index]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PostRepairValidationError(
                f"metric {metric.id} returned a non-numeric value for {metric_date.isoformat()}"
            )
        numeric = float(value)
        if not math.isfinite(numeric):
            raise PostRepairValidationError(
                f"metric {metric.id} returned a non-finite value for {metric_date.isoformat()}"
            )
        return numeric

    def _partition_events_healthy(
        self,
        database_path: Path,
        metric_date: date,
        device_type: str,
    ) -> bool:
        sql = (
            "SELECT COUNT(*) AS partition_event_count FROM events "
            f"WHERE CAST(event_time AS DATE) = DATE '{metric_date.isoformat()}' "
            f"AND device_type = {_sql_literal(device_type)}"
        )
        response = execute_readonly_sql(database_path, sql, max_rows=1)
        if response.status != "success" or response.row_count != 1:
            return False
        expectation = SqlResultExpectation(
            required_columns=["partition_event_count"],
            expected_column_types={"partition_event_count": "BIGINT"},
            numeric_ranges={"partition_event_count": NumericRange(minimum=1)},
            expected_aggregation=MetricAggregation.COUNT,
            max_result_rows=1,
        )
        return validate_sql_result(response, expectation, sql=sql).passed

    def _partition_metadata_healthy(self, database_path: Path, partition_value: str) -> bool:
        sql = (
            "SELECT row_count, status FROM partition_metadata "
            f"WHERE table_name = 'events' AND partition_value = {_sql_literal(partition_value)}"
        )
        response = execute_readonly_sql(database_path, sql, max_rows=2)
        if response.status != "success" or response.row_count != 1 or len(response.rows) != 1:
            return False
        try:
            row_count_index = response.columns.index("row_count")
            status_index = response.columns.index("status")
        except ValueError:
            return False
        row = response.rows[0]
        return (
            isinstance(row[row_count_index], (int, float))
            and not isinstance(row[row_count_index], bool)
            and row[row_count_index] > 0
            and isinstance(row[status_index], str)
            and row[status_index].strip().lower() in {"ready", "success"}
        )

    def _unaffected_events_unchanged(
        self,
        source: Path,
        sandbox: Path,
        metric_date: date,
        device_type: str,
    ) -> bool:
        predicate = (
            f"NOT (CAST(event_time AS DATE) = DATE '{metric_date.isoformat()}' "
            f"AND device_type = {_sql_literal(device_type)})"
        )
        sql = f"SELECT COUNT(*) AS unaffected_event_count FROM events WHERE {predicate}"
        source_response = execute_readonly_sql(source, sql, max_rows=1)
        sandbox_response = execute_readonly_sql(sandbox, sql, max_rows=1)
        if source_response.status != "success" or sandbox_response.status != "success":
            return False
        try:
            return source_response.rows[0][0] == sandbox_response.rows[0][0]
        except (IndexError, TypeError):
            return False

    def _metric(self, metric_id: str) -> MetricDefinition:
        try:
            return self._metrics[metric_id]
        except KeyError as exc:
            raise PostRepairValidationError(f"unknown configured metric: {metric_id}") from exc

    @staticmethod
    def _partition_scope(proposal: RepairProposal) -> tuple[date, str, str]:
        value = proposal.parameters.get("partition_value")
        if not isinstance(value, str):
            raise PostRepairValidationError("repair proposal partition scope is invalid")
        match = re.fullmatch(
            r"(?P<date>\d{4}-\d{2}-\d{2})/(?P<device>[A-Za-z0-9][A-Za-z0-9_.-]*)",
            value,
        )
        if match is None:
            raise PostRepairValidationError("repair proposal partition scope is invalid")
        try:
            parsed = date.fromisoformat(match.group("date"))
        except ValueError as exc:
            raise PostRepairValidationError("repair proposal partition date is invalid") from exc
        return parsed, match.group("device"), value

    @staticmethod
    def _existing_file(value: str | Path, name: str) -> Path:
        path = Path(value).resolve()
        if not path.is_file():
            raise PostRepairValidationError(f"{name} must be an existing file")
        return path

    @staticmethod
    def _validate_ratio(value: float, name: str) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise PostRepairValidationError(f"{name} must be a finite number between 0 and 1")

    def _validate_regression_metric_ids(
        self, metric_ids: tuple[str, ...], target_metric_id: str
    ) -> tuple[str, ...]:
        if len(metric_ids) != len(set(metric_ids)):
            raise PostRepairValidationError("regression_metric_ids must be unique")
        if target_metric_id in metric_ids:
            raise PostRepairValidationError("regression_metric_ids must not include target metric")
        for metric_id in metric_ids:
            self._metric(metric_id)
        return metric_ids

    @staticmethod
    def _within_relative_error(observed: float, expected: float, allowed: float) -> bool:
        if expected == 0:
            return observed == 0
        return abs(observed - expected) <= abs(expected) * allowed

    @staticmethod
    def _validate_binding(proposal: RepairProposal, run: SandboxRun) -> None:
        if run.status is not SandboxRunStatus.SUCCEEDED:
            raise PostRepairValidationError("post-validation requires a successful sandbox run")
        if not proposal_is_intact(proposal):
            raise PostRepairValidationError("repair proposal content hash is invalid")
        if (
            run.incident_id != proposal.incident_id
            or run.proposal_id != proposal.proposal_id
            or run.action is not proposal.action
            or not hmac.compare_digest(run.proposal_hash, proposal.proposal_hash)
        ):
            raise PostRepairValidationError("sandbox run does not bind to repair proposal")


def _aggregation(value: str) -> MetricAggregation | None:
    try:
        return MetricAggregation(value)
    except ValueError:
        return None


def _column_index(response: SqlExecutionResponse, column: str, metric_id: str) -> int:
    try:
        return response.columns.index(column)
    except ValueError as exc:
        raise PostRepairValidationError(
            f"metric query for {metric_id} returned an unexpected schema"
        ) from exc


def _matches_date(value: object, expected: date) -> bool:
    if isinstance(value, date):
        return value == expected
    return isinstance(value, str) and value[:10] == expected.isoformat()


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


__all__ = ["PostRepairValidationError", "PostRepairValidator"]
