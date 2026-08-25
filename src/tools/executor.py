"""Dependency-injected execution for the tools exposed by the Planner.

The executor is intentionally small.  It validates a planned step against the
same registry used by the Planner, then delegates SQL safety and execution to
``tools.sql_runner``.  It does not decide whether a successful query is valid
root-cause evidence; that decision belongs to the hypothesis and validator
layers.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agents.planner import InvestigationStep
from tools.registry import (
    ToolArgumentsError,
    ToolRegistry,
    ToolRegistryError,
    build_default_tool_registry,
)
from tools.sql_runner import (
    SqlExecutionResponse,
    SqlRunnerError,
    execute_readonly_sql,
    validate_readonly_sql,
)


class SqlExecutionPort(Protocol):
    """Minimal adapter contract used to keep the executor easy to test."""

    def __call__(
        self,
        database_path: str | Path,
        sql: str,
        **kwargs: Any,
    ) -> SqlExecutionResponse: ...


class ToolExecutionResult(BaseModel):
    """Stable, JSON-serializable envelope for one planned tool step."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1)
    success: bool
    query_id: str | None = None
    result: JsonValue | None = None
    error: dict[str, str] | None = None
    # Evidence is opt-in.  A successful SQL response is a result, not proof
    # of a root cause, so the default adapter deliberately returns no entries.
    evidence: list[dict[str, JsonValue]] = Field(default_factory=list)


class ToolExecutor:
    """Execute registered read-only investigation tools."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        registry: ToolRegistry | None = None,
        sql_execution: SqlExecutionPort = execute_readonly_sql,
        audit_path: str | Path | None = None,
    ) -> None:
        self.database_path = database_path
        self.registry = registry or build_default_tool_registry()
        self.sql_execution = sql_execution
        self.audit_path = audit_path

    def execute_step(
        self,
        step: InvestigationStep | Mapping[str, Any],
        *,
        incident_id: str | None = None,
        trace_id: str | None = None,
    ) -> ToolExecutionResult:
        """Validate and execute one plan step.

        Registry and argument failures are returned as normalized failures and
        never reach a tool adapter.  The current registry has one executable
        tool, ``sql_query``; adding a new tool requires an explicit adapter
        here rather than silently accepting a registry entry.
        """

        try:
            normalized_step = (
                step
                if isinstance(step, InvestigationStep)
                else InvestigationStep.model_validate(step)
            )
        except (TypeError, ValueError) as exc:
            tool_name = _tool_name_from_payload(step)
            return self._failure(tool_name, "invalid_step", str(exc))

        tool_name = normalized_step.tool
        try:
            definition = self.registry.get(tool_name)
            self.registry.validate_arguments(tool_name, normalized_step.arguments)
        except (ToolRegistryError, ToolArgumentsError) as exc:
            return self._failure(tool_name, "tool_contract", str(exc))

        if not definition.read_only:
            return self._failure(
                tool_name,
                "unsafe_tool",
                f"tool is not read-only: {tool_name}",
            )

        if tool_name != "sql_query":
            return self._failure(
                tool_name,
                "unsupported_tool",
                f"no execution adapter is registered for tool: {tool_name}",
            )

        sql = normalized_step.arguments["sql"]
        if not isinstance(sql, str):
            # The registry normally catches this, but keep the adapter
            # boundary defensive if a custom registry is injected.
            return self._failure(tool_name, "tool_contract", "arguments.sql must be a string")
        try:
            # Reuse the canonical AST/native SQL guard before invoking an
            # injected adapter; no SQL safety rules are reimplemented here.
            validate_readonly_sql(sql)
        except (SqlRunnerError, TypeError, ValueError) as exc:
            return self._failure(tool_name, "tool_contract", str(exc))

        try:
            response = self.sql_execution(
                self.database_path,
                sql,
                incident_id=incident_id,
                trace_id=trace_id,
                audit_path=self.audit_path,
            )
            if not isinstance(response, SqlExecutionResponse):
                response = SqlExecutionResponse.model_validate(response)
        except Exception as exc:  # noqa: BLE001 - normalize adapter failures
            return self._failure(
                tool_name,
                "execution",
                str(exc),
                query_id=getattr(exc, "query_id", None),
            )

        payload = cast(dict[str, JsonValue], response.model_dump(mode="json"))
        if response.status == "success":
            return ToolExecutionResult(
                tool_name=tool_name,
                success=True,
                query_id=response.query_id,
                result=payload,
            )
        return ToolExecutionResult(
            tool_name=tool_name,
            success=False,
            query_id=response.query_id,
            result=payload,
            error=response.error
            or {"type": "execution", "message": "SQL execution failed"},
        )

    @staticmethod
    def _failure(
        tool_name: str,
        error_type: str,
        message: str,
        *,
        query_id: str | None = None,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            tool_name=tool_name or "unknown",
            success=False,
            query_id=query_id,
            error={"type": error_type, "message": message},
        )


def _tool_name_from_payload(step: object) -> str:
    if isinstance(step, Mapping):
        value = step.get("tool")
        if isinstance(value, str) and value.strip():
            return value
    return "invalid"


__all__ = ["SqlExecutionPort", "ToolExecutionResult", "ToolExecutor"]
