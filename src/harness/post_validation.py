"""Metric-based validation of completed DuckDB sandbox repairs."""

from __future__ import annotations

import hmac
import math
from datetime import date
from pathlib import Path
from uuid import uuid4

from config.metrics import DEFAULT_METRICS_PATH, MetricDefinition, load_metrics_config
from harness.repair import (
    PostValidationResult,
    PostValidationStatus,
    RepairProposal,
    SandboxRun,
    SandboxRunStatus,
)
from tools.sql_runner import execute_readonly_sql


class PostRepairValidationError(ValueError):
    """Raised when a configured metric cannot be measured for post-validation."""


class PostRepairValidator:
    """Compare configured metric values in the source and sandbox databases.

    Metric SQL is loaded only from the validated ``metrics.yaml`` configuration
    and passed through the existing read-only SQL Runner. Callers provide
    expected values and a fixed set of regression metrics; no proposal text is
    evaluated as SQL or code.
    """

    def __init__(
        self,
        source_database_path: str | Path,
        *,
        metrics_path: str | Path = DEFAULT_METRICS_PATH,
    ) -> None:
        self._source_database_path = self._require_database(
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
        """Return a pass only when the target recovers without regressions.

        A non-target regression is an observed decrease greater than
        ``max_regression_ratio`` versus the pre-repair source value. Metrics
        whose source value is zero cannot decrease and are therefore skipped.
        """

        self._validate_binding(proposal, run)
        self._validate_ratio(allowed_relative_error, "allowed_relative_error")
        self._validate_ratio(max_regression_ratio, "max_regression_ratio")
        if not isinstance(expected_value, (int, float)) or isinstance(
            expected_value, bool
        ) or not math.isfinite(expected_value):
            raise PostRepairValidationError("expected_value must be a finite number")
        target_metric = self._metric(metric_id)
        regression_ids = self._validate_regression_metric_ids(
            regression_metric_ids, metric_id
        )
        sandbox_database_path = self._require_database(run.sandbox_path, "sandbox_path")

        observed_before = self._measure(
            self._source_database_path, target_metric, metric_date
        )
        observed_after = self._measure(sandbox_database_path, target_metric, metric_date)
        target_met = self._within_relative_error(
            observed_after, float(expected_value), allowed_relative_error
        )

        regressions: list[str] = []
        for regression_id in regression_ids:
            metric = self._metric(regression_id)
            before = self._measure(self._source_database_path, metric, metric_date)
            after = self._measure(sandbox_database_path, metric, metric_date)
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
        summary = self._summary(
            metric_id,
            observed_before,
            observed_after,
            expected_value,
            allowed_relative_error,
            regressions,
        )
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
        self, database_path: Path, metric: MetricDefinition, metric_date: date
    ) -> float:
        response = execute_readonly_sql(
            database_path,
            metric.query,
            max_rows=metric.validation.max_result_rows,
        )
        if response.status != "success" or response.truncated:
            raise PostRepairValidationError(
                f"could not measure {metric.id}: {response.error or 'query failed'}"
            )
        try:
            date_index = response.columns.index("metric_date")
            metric_index = response.columns.index(metric.id)
        except ValueError as exc:
            raise PostRepairValidationError(
                f"metric query for {metric.id} returned an unexpected schema"
            ) from exc
        matches = [row for row in response.rows if self._matches_date(row[date_index], metric_date)]
        if len(matches) != 1:
            raise PostRepairValidationError(
                f"metric {metric.id} returned {len(matches)} rows for {metric_date.isoformat()}"
            )
        value = matches[0][metric_index]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PostRepairValidationError(
                f"metric {metric.id} returned a non-numeric value for {metric_date.isoformat()}"
            )
        numeric_value = float(value)
        if not math.isfinite(numeric_value):
            raise PostRepairValidationError(
                f"metric {metric.id} returned a non-finite value for {metric_date.isoformat()}"
            )
        return numeric_value

    def _metric(self, metric_id: str) -> MetricDefinition:
        try:
            return self._metrics[metric_id]
        except KeyError as exc:
            raise PostRepairValidationError(f"unknown configured metric: {metric_id}") from exc

    @staticmethod
    def _require_database(value: str | Path, field_name: str) -> Path:
        path = Path(value).resolve()
        if not path.is_file():
            raise PostRepairValidationError(f"{field_name} must be an existing file")
        return path

    @staticmethod
    def _validate_ratio(value: float, field_name: str) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise PostRepairValidationError(
                f"{field_name} must be a finite number between 0 and 1"
            )

    def _validate_regression_metric_ids(
        self, metric_ids: tuple[str, ...], target_metric_id: str
    ) -> tuple[str, ...]:
        if len(metric_ids) != len(set(metric_ids)):
            raise PostRepairValidationError("regression_metric_ids must be unique")
        if target_metric_id in metric_ids:
            raise PostRepairValidationError(
                "regression_metric_ids must not include the target metric"
            )
        for metric_id in metric_ids:
            self._metric(metric_id)
        return metric_ids

    @staticmethod
    def _matches_date(value: object, expected: date) -> bool:
        if isinstance(value, date):
            return value == expected
        return isinstance(value, str) and value == expected.isoformat()

    @staticmethod
    def _within_relative_error(
        observed: float, expected: float, allowed_relative_error: float
    ) -> bool:
        if expected == 0:
            return observed == 0
        return abs(observed - expected) <= abs(expected) * allowed_relative_error

    @staticmethod
    def _summary(
        metric_id: str,
        observed_before: float,
        observed_after: float,
        expected_value: float,
        allowed_relative_error: float,
        regressions: list[str],
    ) -> str:
        target = (
            f"{metric_id} changed from {observed_before:g} to {observed_after:g}; "
            f"expected {expected_value:g} within {allowed_relative_error:.0%}."
        )
        if not regressions:
            return target + " No configured regression was observed."
        return target + " Regressions: " + "; ".join(regressions)

    @staticmethod
    def _validate_binding(proposal: RepairProposal, run: SandboxRun) -> None:
        if run.status is not SandboxRunStatus.SUCCEEDED:
            raise PostRepairValidationError("post-validation requires a successful sandbox run")
        if proposal.content_hash() != proposal.proposal_hash:
            raise PostRepairValidationError("repair proposal content hash is invalid")
        if (
            run.incident_id != proposal.incident_id
            or run.proposal_id != proposal.proposal_id
            or not hmac.compare_digest(run.proposal_hash, proposal.proposal_hash)
        ):
            raise PostRepairValidationError("sandbox run does not bind to repair proposal")


__all__ = ["PostRepairValidationError", "PostRepairValidator"]
