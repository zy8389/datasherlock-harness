"""Confined, deterministic DuckDB repair execution."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import duckdb

from harness.repair import (
    RepairAction,
    RepairProposal,
    SandboxRun,
    SandboxRunStatus,
    proposal_is_intact,
)


class SandboxRepairError(ValueError):
    """Raised when a repair invocation is unsafe or incorrectly bound."""


_SAFE_SEGMENT: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PARTITION_VALUE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})/(?P<device>[A-Za-z0-9][A-Za-z0-9_.-]*)$"
)


class SandboxRepairExecutor:
    """Copy a source DB and run one allowlisted handler in the copy only."""

    def __init__(
        self,
        source_database_path: str | Path,
        sandbox_root: str | Path,
        *,
        repair_source_database_path: str | Path | None = None,
    ) -> None:
        self._source_database_path = self._existing_file(
            source_database_path, "source_database_path"
        )
        self._repair_source_database_path = (
            self._existing_file(
                repair_source_database_path, "repair_source_database_path"
            )
            if repair_source_database_path is not None
            else None
        )
        self._sandbox_root = Path(sandbox_root).resolve()
        self._sandbox_root.mkdir(parents=True, exist_ok=True)
        if not self._sandbox_root.is_dir():
            raise SandboxRepairError("sandbox_root must be a directory")
        self._assert_no_reparse_points(self._sandbox_root, include_root=True)

    def sandbox_path_for(self, run: SandboxRun) -> Path:
        """Derive the only database path that this executor may write."""

        incident_id = self._safe_segment(run.incident_id, "incident_id")
        run_id = self._safe_segment(run.run_id, "run_id")
        target = self._sandbox_root / incident_id / run_id / "datasherlock.duckdb"
        self._assert_confined(target)
        return target

    def execute(self, proposal: RepairProposal, run: SandboxRun) -> SandboxRun:
        """Execute exactly once and return a terminal run artifact."""

        self._validate_binding(proposal, run)
        target = self.sandbox_path_for(run)
        self._validate_supplied_sandbox_path(run, target)
        self._assert_confined(target)
        self._assert_no_reparse_points(target.parent, include_root=True)
        if target.parent.exists():
            recovered = self.recover_run(proposal, run)
            if recovered is not None:
                return recovered
            raise SandboxRepairError("sandbox run directory already exists")

        source_before = _sha256_file(self._source_database_path)
        started_at = datetime.now(UTC)
        operation_details: dict[str, object] = {}
        changed_row_counts: dict[str, int] = {}
        handler_invocation_count = 0
        try:
            target.parent.mkdir(parents=True, exist_ok=False)
            self._assert_no_reparse_points(target.parent, include_root=False)
            shutil.copy2(self._source_database_path, target)
            sandbox_before = _sha256_file(target)
            handler_invocation_count = 1
            self._write_invocation_marker(
                target.parent,
                run_id=run.run_id,
                proposal=proposal,
            )
            changed_row_counts, operation_details = self._execute_handler(
                proposal, target
            )
            source_after = _sha256_file(self._source_database_path)
            if not hmac.compare_digest(source_before, source_after):
                raise SandboxRepairError("source database changed during repair")
            sandbox_after = _sha256_file(target)
        except Exception as exc:  # noqa: BLE001 - normalize into a failed run artifact
            source_after = _sha256_file(self._source_database_path)
            sandbox_before = (
                _sha256_file(target) if target.is_file() else None
            )
            sandbox_after = sandbox_before
            return self._finished_run(
                run,
                status=SandboxRunStatus.FAILED,
                started_at=started_at,
                source_hash_before=source_before,
                source_hash_after=source_after,
                sandbox_hash_before=sandbox_before,
                sandbox_hash_after=sandbox_after,
                handler_invocation_count=handler_invocation_count,
                changed_row_counts=changed_row_counts,
                operation_details=operation_details,
                error=f"{type(exc).__name__}: {exc}",
                sandbox_path=target,
            )
        return self._finished_run(
            run,
            status=SandboxRunStatus.SUCCEEDED,
            started_at=started_at,
            source_hash_before=source_before,
            source_hash_after=source_after,
            sandbox_hash_before=sandbox_before,
            sandbox_hash_after=sandbox_after,
            handler_invocation_count=handler_invocation_count,
            changed_row_counts=changed_row_counts,
            operation_details=operation_details,
            sandbox_path=target,
        )

    def recover_run(
        self,
        proposal: RepairProposal,
        pending_run: SandboxRun,
    ) -> SandboxRun | None:
        """Recover a durable terminal run without invoking its handler."""

        self._validate_binding(proposal, pending_run)
        target = self.sandbox_path_for(pending_run)
        self._validate_supplied_sandbox_path(pending_run, target)
        self._assert_confined(target)
        run_directory = target.parent
        if not run_directory.exists():
            return None
        self._assert_no_reparse_points(run_directory, include_root=True)
        result_path = run_directory / "repair-result.json"
        temporary_result_path = run_directory / "repair-result.json.tmp"
        marker_path = run_directory / "repair-invocation.json"
        self._assert_no_reparse_points(result_path, include_root=True)
        self._assert_no_reparse_points(temporary_result_path, include_root=True)
        self._assert_no_reparse_points(marker_path, include_root=True)
        if temporary_result_path.exists():
            raise SandboxRepairError(
                "repair run is indeterminate: terminal result write was incomplete"
            )
        if not result_path.is_file():
            if marker_path.exists():
                raise SandboxRepairError(
                    "repair run is indeterminate: invocation marker has no durable terminal result"
                )
            raise SandboxRepairError(
                "sandbox run directory exists without a durable terminal result"
            )
        try:
            raw = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                raise TypeError("result JSON must contain an object")
            required_fields = {
                "run_id",
                "incident_id",
                "proposal_id",
                "proposal_hash",
                "approval_decision_id",
                "action",
                "sandbox_path",
                "status",
                "source_hash_before",
                "source_hash_after",
                "sandbox_hash_before",
                "sandbox_hash_after",
                "handler_invocation_count",
                "changed_row_counts",
                "operation_details",
                "started_at",
                "finished_at",
                "error",
            }
            if not required_fields.issubset(raw):
                raise TypeError("result JSON is missing terminal run fields")
            recovered = SandboxRun.model_validate(raw)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SandboxRepairError("durable repair result is malformed") from exc
        if recovered.status not in {
            SandboxRunStatus.SUCCEEDED,
            SandboxRunStatus.FAILED,
            SandboxRunStatus.CANCELLED,
        }:
            raise SandboxRepairError("durable repair result is not terminal")
        if (
            not proposal_is_intact(proposal)
            or recovered.run_id != pending_run.run_id
            or recovered.incident_id != proposal.incident_id
            or recovered.proposal_id != proposal.proposal_id
            or not hmac.compare_digest(recovered.proposal_hash, proposal.proposal_hash)
            or recovered.action is not proposal.action
            or recovered.approval_decision_id != pending_run.approval_decision_id
        ):
            raise SandboxRepairError(
                "durable repair result does not bind to the pending proposal"
            )
        self._validate_supplied_sandbox_path(recovered, target)
        self._validate_terminal_run_contract(recovered)
        self._validate_invocation_marker(marker_path, recovered)
        source_current = _sha256_file(self._source_database_path)
        if recovered.status is SandboxRunStatus.SUCCEEDED:
            if not target.is_file():
                raise SandboxRepairError(
                    "durable successful repair is missing its sandbox database"
                )
            if recovered.source_hash_before != source_current or recovered.source_hash_after != source_current:
                raise SandboxRepairError(
                    "source database hash no longer matches durable successful repair"
                )
            if recovered.sandbox_hash_after != _sha256_file(target):
                raise SandboxRepairError(
                    "sandbox database hash no longer matches durable successful repair"
                )
        elif recovered.source_hash_before is not None and recovered.source_hash_after is not None:
            if (
                recovered.source_hash_before != source_current
                or recovered.source_hash_after != source_current
            ):
                raise SandboxRepairError(
                    "source database hash no longer matches durable repair result"
                )
        return recovered

    def _execute_handler(
        self,
        proposal: RepairProposal,
        sandbox_path: Path,
    ) -> tuple[dict[str, int], dict[str, object]]:
        if proposal.action is not RepairAction.RERUN_PARTITION:
            raise SandboxRepairError(
                f"no deterministic sandbox handler is implemented for {proposal.action.value}"
            )
        with duckdb.connect(str(sandbox_path)) as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                changed, details = self._rerun_partition(connection, proposal.parameters)
            except Exception:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")
        return changed, details

    def _rerun_partition(
        self,
        connection: duckdb.DuckDBPyConnection,
        parameters: Mapping[str, object],
    ) -> tuple[dict[str, int], dict[str, object]]:
        """Restore events and metadata for one evidence-derived partition."""

        required = {"table", "source_table", "partition_column", "partition_value"}
        if set(parameters) != required:
            raise SandboxRepairError(
                "rerun_partition parameters must be exactly: "
                + ", ".join(sorted(required))
            )
        if (
            parameters.get("table") != "events"
            or parameters.get("source_table") != "events"
            or parameters.get("partition_column") != "device_type"
        ):
            raise SandboxRepairError("F01 rerun_partition identifiers are fixed")
        partition_value = parameters.get("partition_value")
        if not isinstance(partition_value, str):
            raise SandboxRepairError("partition_value must be a string")
        match = _PARTITION_VALUE.fullmatch(partition_value)
        if match is None:
            raise SandboxRepairError("partition_value must be YYYY-MM-DD/device")
        try:
            from datetime import date

            target_date = date.fromisoformat(match.group("date"))
        except ValueError as exc:
            raise SandboxRepairError("partition_value contains an invalid date") from exc
        device_type = match.group("device")
        if self._repair_source_database_path is None:
            raise SandboxRepairError(
                "rerun_partition requires configured repair_source_database_path"
            )

        connection.execute(
            f"ATTACH {_sql_literal(str(self._repair_source_database_path))} "
            "AS repair_source (READ_ONLY)"
        )
        event_deleted = int(
            connection.execute(
                "DELETE FROM events WHERE CAST(event_time AS DATE) = ? "
                "AND device_type = ? RETURNING *",
                [target_date, device_type],
            ).fetchall().__len__()
        )
        event_inserted = int(
            connection.execute(
                "INSERT INTO events SELECT * FROM repair_source.events "
                "WHERE CAST(event_time AS DATE) = ? AND device_type = ? RETURNING *",
                [target_date, device_type],
            ).fetchall().__len__()
        )
        metadata_deleted = int(
            connection.execute(
                "DELETE FROM partition_metadata WHERE table_name = ? "
                "AND partition_value = ? RETURNING *",
                ["events", partition_value],
            ).fetchall().__len__()
        )
        metadata_inserted = int(
            connection.execute(
                "INSERT INTO partition_metadata "
                "SELECT * FROM repair_source.partition_metadata "
                "WHERE table_name = ? AND partition_value = ? RETURNING *",
                ["events", partition_value],
            ).fetchall().__len__()
        )
        if event_inserted <= 0 or metadata_inserted != 1:
            raise SandboxRepairError(
                "trusted repair source did not contain the requested healthy partition"
            )
        return (
            {
                "events_deleted": event_deleted,
                "events_inserted": event_inserted,
                "partition_metadata_deleted": metadata_deleted,
                "partition_metadata_inserted": metadata_inserted,
            },
            {
                "partition_value": partition_value,
                "target_date": target_date.isoformat(),
                "device_type": device_type,
            },
        )

    def _write_invocation_marker(
        self,
        run_directory: Path,
        *,
        run_id: str,
        proposal: RepairProposal,
    ) -> None:
        marker = run_directory / "repair-invocation.json"
        marker_payload = {
            "handler_invocation_count": 1,
            "run_id": run_id,
            "proposal_id": proposal.proposal_id,
            "proposal_hash": proposal.proposal_hash,
        }
        with marker.open("x", encoding="utf-8") as file:
            json.dump(marker_payload, file, sort_keys=True, separators=(",", ":"))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

    def _write_terminal_result(self, run: SandboxRun) -> None:
        result_path = Path(run.sandbox_path).parent / "repair-result.json"
        temporary_path = result_path.with_name("repair-result.json.tmp")
        self._assert_no_reparse_points(result_path, include_root=True)
        self._assert_no_reparse_points(temporary_path, include_root=True)
        if result_path.exists() or temporary_path.exists():
            raise SandboxRepairError("durable terminal repair result already exists")
        try:
            with temporary_path.open("x", encoding="utf-8") as file:
                json.dump(
                    run.model_dump(mode="json"),
                    file,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, result_path)
            _fsync_directory(result_path.parent)
        except (OSError, TypeError, ValueError) as exc:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise SandboxRepairError(
                "could not durably persist terminal repair result"
            ) from exc

    def _validate_binding(self, proposal: RepairProposal, run: SandboxRun) -> None:
        if run.status is not SandboxRunStatus.PENDING:
            raise SandboxRepairError("only pending sandbox runs may execute")
        if not proposal_is_intact(proposal):
            raise SandboxRepairError("repair proposal content hash is invalid")
        if (
            run.incident_id != proposal.incident_id
            or run.proposal_id != proposal.proposal_id
            or run.action is not proposal.action
            or not hmac.compare_digest(run.proposal_hash, proposal.proposal_hash)
        ):
            raise SandboxRepairError("sandbox run does not bind to repair proposal")

    @staticmethod
    def _validate_supplied_sandbox_path(run: SandboxRun, target: Path) -> None:
        supplied = Path(run.sandbox_path)
        if not supplied.is_absolute() or ".." in supplied.parts:
            raise SandboxRepairError(
                "sandbox_path must be an absolute executor-derived path without traversal"
            )
        try:
            supplied_resolved = supplied.resolve(strict=False)
        except OSError as exc:
            raise SandboxRepairError("sandbox_path could not be resolved") from exc
        if supplied_resolved != target:
            raise SandboxRepairError(
                "sandbox_path must equal the executor-derived sandbox path"
            )

    @staticmethod
    def _validate_terminal_run_contract(run: SandboxRun) -> None:
        if run.status is SandboxRunStatus.SUCCEEDED:
            if (
                not _is_sha256(run.source_hash_before)
                or not _is_sha256(run.source_hash_after)
                or run.source_hash_before != run.source_hash_after
                or not _is_sha256(run.sandbox_hash_before)
                or not _is_sha256(run.sandbox_hash_after)
                or run.finished_at is None
                or run.error is not None
                or run.handler_invocation_count != 1
            ):
                raise SandboxRepairError(
                    "successful sandbox run is incomplete or internally inconsistent"
                )
        elif (
            not run.error
            or run.finished_at is None
            or run.handler_invocation_count < 0
        ):
            raise SandboxRepairError(
                "failed sandbox run requires error and finished_at"
            )
        for value in (
            run.source_hash_before,
            run.source_hash_after,
            run.sandbox_hash_before,
            run.sandbox_hash_after,
        ):
            if value is not None and not _is_sha256(value):
                raise SandboxRepairError("sandbox run contains an invalid hash")

    @staticmethod
    def _validate_invocation_marker(marker_path: Path, run: SandboxRun) -> None:
        if not marker_path.exists():
            if run.status is SandboxRunStatus.SUCCEEDED or run.handler_invocation_count != 0:
                raise SandboxRepairError("terminal repair result is missing invocation marker")
            return
        try:
            payload = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SandboxRepairError("repair invocation marker is malformed") from exc
        if (
            not isinstance(payload, Mapping)
            or payload.get("handler_invocation_count") != 1
            or payload.get("run_id") != run.run_id
            or payload.get("proposal_id") != run.proposal_id
            or payload.get("proposal_hash") != run.proposal_hash
            or run.handler_invocation_count != 1
        ):
            raise SandboxRepairError("repair invocation marker does not bind to result")

    def _assert_confined(self, path: Path) -> None:
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(self._sandbox_root):
            raise SandboxRepairError("sandbox path escapes sandbox_root")

    @staticmethod
    def _existing_file(value: str | Path, name: str) -> Path:
        path = Path(value).resolve()
        if not path.is_file():
            raise SandboxRepairError(f"{name} must be an existing file")
        return path

    @staticmethod
    def _safe_segment(value: str, name: str) -> str:
        if not isinstance(value, str) or _SAFE_SEGMENT.fullmatch(value) is None:
            raise SandboxRepairError(
                f"{name} may contain only letters, numbers, dots, underscores, and hyphens"
            )
        return value

    @staticmethod
    def _assert_no_reparse_points(path: Path, *, include_root: bool) -> None:
        parts = list(path.parents)[::-1]
        candidates = parts + ([path] if include_root else [])
        for candidate in candidates:
            if candidate.exists() and _is_reparse_point(candidate):
                raise SandboxRepairError("sandbox path contains a symlink or reparse point")

    def _finished_run(
        self,
        run: SandboxRun,
        *,
        status: SandboxRunStatus,
        started_at: datetime,
        source_hash_before: str,
        source_hash_after: str,
        sandbox_hash_before: str | None,
        sandbox_hash_after: str | None,
        handler_invocation_count: int,
        changed_row_counts: Mapping[str, int],
        operation_details: Mapping[str, object],
        sandbox_path: Path,
        error: str | None = None,
    ) -> SandboxRun:
        finished = SandboxRun.model_validate(
            {
                **run.model_dump(mode="json"),
                "sandbox_path": str(sandbox_path),
                "status": status,
                "source_hash_before": source_hash_before,
                "source_hash_after": source_hash_after,
                "sandbox_hash_before": sandbox_hash_before,
                "sandbox_hash_after": sandbox_hash_after,
                "handler_invocation_count": handler_invocation_count,
                "changed_row_counts": dict(changed_row_counts),
                "operation_details": dict(operation_details),
                "started_at": started_at,
                "finished_at": datetime.now(UTC),
                "error": error,
            }
        )
        self._validate_terminal_run_contract(finished)
        self._write_terminal_result(finished)
        return finished


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = path.stat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & 0x400)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _fsync_directory(path: Path) -> None:
    """Best-effort directory durability across POSIX and Windows."""

    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = ["SandboxRepairError", "SandboxRepairExecutor"]
