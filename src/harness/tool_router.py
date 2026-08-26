"""Execute registered investigation tools through their required guardrails."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from harness.sql_investigation import ValidatedSqlExecution, execute_validated_sql
from harness.state import IncidentState
from tools.registry import ToolRegistry, build_default_tool_registry
from validators.sql_result import SqlResultExpectation


class InvestigationToolRouter:
    """Route implemented investigation tools without bypassing validation."""

    def __init__(self, tool_registry: ToolRegistry | None = None) -> None:
        self._tool_registry = tool_registry or build_default_tool_registry()

    def execute(
        self,
        state: IncidentState,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        database_path: str | Path,
        metric_id: str | None = None,
        expectation: SqlResultExpectation | None = None,
        finding: str | None = None,
        incident_id: str | None = None,
        trace_id: str | None = None,
        audit_path: str | Path | None = None,
        timeout_seconds: float = 10.0,
        max_rows: int = 1000,
    ) -> ValidatedSqlExecution:
        """Validate registered arguments and execute a SQL tool via the harness.

        ``sql_query`` is the only executable tool registered today. Direct use of
        the SQL Runner remains available for lower-level components and tests,
        but all registered investigation execution is routed through result
        validation and incident trace/evidence binding.
        """

        self._tool_registry.validate_arguments(tool_name, arguments)
        if tool_name != "sql_query":
            raise ValueError(f"tool has no execution implementation: {tool_name}")
        sql = arguments["sql"]
        if not isinstance(sql, str):
            raise TypeError("sql_query.sql must be a string")
        return execute_validated_sql(
            state,
            database_path,
            sql,
            metric_id=metric_id,
            expectation=expectation,
            finding=finding,
            incident_id=incident_id,
            trace_id=trace_id,
            audit_path=audit_path,
            timeout_seconds=timeout_seconds,
            max_rows=max_rows,
        )


def execute_investigation_tool(
    state: IncidentState,
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    database_path: str | Path,
    **kwargs: Any,
) -> ValidatedSqlExecution:
    """Execute one default-registered investigation tool through its router."""

    return InvestigationToolRouter().execute(
        state,
        tool_name,
        arguments,
        database_path=database_path,
        **kwargs,
    )


__all__ = ["InvestigationToolRouter", "execute_investigation_tool"]
