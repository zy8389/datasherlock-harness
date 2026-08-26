"""Deterministic, filesystem-confined repair execution for DuckDB sandboxes."""

from __future__ import annotations

import hmac
import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import duckdb

from harness.repair import RepairAction, RepairProposal, SandboxRun, SandboxRunStatus

_PATH_SEGMENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SandboxRepairError(ValueError):
    """Raised for unsafe or invalid sandbox repair invocations."""


RepairHandler = Callable[[duckdb.DuckDBPyConnection, dict[str, object]], None]


class SandboxRepairExecutor:
    """Copy a DuckDB database and execute one known repair handler in that copy.

    ``repair_source_database_path`` is trusted service configuration, not a
    proposal parameter. This prevents a model from selecting arbitrary local
    files as repair inputs. The executor never evaluates proposal text as SQL,
    shell, or Python code.

    Only ``rerun_partition`` is implemented today. Other values in
    :class:`RepairAction` remain unavailable until they receive a dedicated,
    parameter-validated handler.
    """

    def __init__(
        self,
        source_database_path: str | Path,
        sandbox_root: str | Path,
        *,
        repair_source_database_path: str | Path | None = None,
    ) -> None:
        self._source_database_path = self._resolve_existing_file(
            source_database_path, "source_database_path"
        )
        self._repair_source_database_path = (
            self._resolve_existing_file(
                repair_source_database_path, "repair_source_database_path"
            )
            if repair_source_database_path is not None
            else None
        )
        self._sandbox_root = Path(sandbox_root).resolve()
        self._sandbox_root.mkdir(parents=True, exist_ok=True)
        if not self._sandbox_root.is_dir():
            raise SandboxRepairError("sandbox_root must be a directory")
        self._handlers: dict[RepairAction, RepairHandler] = {
            RepairAction.RERUN_PARTITION: self._rerun_partition,
        }

    def sandbox_path_for(self, run: SandboxRun) -> Path:
        """Return the only database path this executor may write for ``run``."""

        incident_id = self._safe_segment(run.incident_id, "incident_id")
        run_id = self._safe_segment(run.run_id, "run_id")
        target = self._sandbox_root / incident_id / run_id / "datasherlock.duckdb"
        resolved_target = target.resolve()
        if not resolved_target.is_relative_to(self._sandbox_root):
            raise SandboxRepairError("sandbox target escapes sandbox_root")
        return resolved_target

    def execute(self, proposal: RepairProposal, run: SandboxRun) -> SandboxRun:
        """Execute a proposal against a new database copy and return its outcome.

        Invalid bindings are rejected before filesystem work begins. Runtime or
        handler failures are returned as a failed ``SandboxRun`` so the approval
        flow can record the terminal tool outcome.
        """

        self._validate_binding(proposal, run)
        target = self.sandbox_path_for(run)
        run_directory = target.parent
        if run_directory.exists():
            raise SandboxRepairError("sandbox run directory already exists")

        started_at = datetime.now(UTC)
        try:
            run_directory.mkdir(parents=True, exist_ok=False)
            shutil.copy2(self._source_database_path, target)
            self._execute_handler(proposal, target)
        except Exception as exc:  # noqa: BLE001 - convert executor failures to a run record.
            return self._finished_run(
                run,
                status=SandboxRunStatus.FAILED,
                started_at=started_at,
                error=f"{type(exc).__name__}: {exc}",
                sandbox_path=target,
            )
        return self._finished_run(
            run,
            status=SandboxRunStatus.SUCCEEDED,
            started_at=started_at,
            sandbox_path=target,
        )

    def _execute_handler(self, proposal: RepairProposal, sandbox_path: Path) -> None:
        handler = self._handlers.get(proposal.action)
        if handler is None:
            raise SandboxRepairError(
                f"no deterministic sandbox handler is implemented for {proposal.action.value}"
            )
        parameters = dict(proposal.parameters)
        with duckdb.connect(str(sandbox_path)) as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                handler(connection, parameters)
            except Exception:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")

    def _rerun_partition(
        self, connection: duckdb.DuckDBPyConnection, parameters: dict[str, object]
    ) -> None:
        """Restore one partition from the configured trusted repair database."""

        if self._repair_source_database_path is None:
            raise SandboxRepairError(
                "rerun_partition requires configured repair_source_database_path"
            )
        required_keys = {
            "table",
            "source_table",
            "partition_column",
            "partition_value",
        }
        if set(parameters) != required_keys:
            raise SandboxRepairError(
                "rerun_partition parameters must be exactly: "
                + ", ".join(sorted(required_keys))
            )
        table_name = self._identifier(parameters["table"], "table")
        source_table = self._identifier(parameters["source_table"], "source_table")
        partition_column = self._identifier(
            parameters["partition_column"], "partition_column"
        )
        partition_value = parameters["partition_value"]
        if not isinstance(partition_value, (str, int, float)) or isinstance(
            partition_value, bool
        ):
            raise SandboxRepairError(
                "partition_value must be a string, integer, or floating-point value"
            )

        source_literal = self._sql_literal(str(self._repair_source_database_path))
        connection.execute(f"ATTACH {source_literal} AS repair_source (READ_ONLY)")
        connection.execute(
            f"DELETE FROM {table_name} WHERE {partition_column} = ?",
            [partition_value],
        )
        connection.execute(
            "INSERT INTO "
            f"{table_name} SELECT * FROM repair_source.{source_table} "
            f"WHERE {partition_column} = ?",
            [partition_value],
        )
        # The connection context closes after the transaction commits, which
        # releases the attached read-only database without a mid-transaction DETACH.

    @staticmethod
    def _resolve_existing_file(value: str | Path, field_name: str) -> Path:
        path = Path(value).resolve()
        if not path.is_file():
            raise SandboxRepairError(f"{field_name} must be an existing file")
        return path

    @staticmethod
    def _safe_segment(value: str, field_name: str) -> str:
        if not _PATH_SEGMENT_PATTERN.fullmatch(value):
            raise SandboxRepairError(
                f"{field_name} may contain only letters, numbers, dots, underscores, and hyphens"
            )
        return value

    @staticmethod
    def _identifier(value: object, field_name: str) -> str:
        if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
            raise SandboxRepairError(
                f"{field_name} must contain only letters, numbers, and underscores"
            )
        return f'"{value}"'

    @staticmethod
    def _sql_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    @staticmethod
    def _validate_binding(proposal: RepairProposal, run: SandboxRun) -> None:
        if run.status is not SandboxRunStatus.PENDING:
            raise SandboxRepairError("only pending sandbox runs may execute")
        if proposal.content_hash() != proposal.proposal_hash:
            raise SandboxRepairError("repair proposal content hash is invalid")
        if (
            run.incident_id != proposal.incident_id
            or run.proposal_id != proposal.proposal_id
            or run.action is not proposal.action
            or not hmac.compare_digest(run.proposal_hash, proposal.proposal_hash)
        ):
            raise SandboxRepairError("sandbox run does not bind to repair proposal")

    @staticmethod
    def _finished_run(
        run: SandboxRun,
        *,
        status: SandboxRunStatus,
        started_at: datetime,
        sandbox_path: Path,
        error: str | None = None,
    ) -> SandboxRun:
        return SandboxRun.model_validate(
            {
                **run.model_dump(),
                "sandbox_path": str(sandbox_path),
                "status": status,
                "started_at": started_at,
                "finished_at": datetime.now(UTC),
                "error": error,
            }
        )


__all__ = ["SandboxRepairError", "SandboxRepairExecutor"]
