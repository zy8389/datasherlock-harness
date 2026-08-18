from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

import duckdb
import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import Tokenizer, TokenType

StatementType = Literal["SELECT", "DESCRIBE", "EXPLAIN"]


class SqlRunnerError(RuntimeError):
    """Base error carrying the query id used by the audit trail."""

    def __init__(self, message: str, query_id: str) -> None:
        super().__init__(message)
        self.query_id = query_id


class SqlValidationError(SqlRunnerError):
    """Raised before execution when SQL is not a single read-only statement."""


class SqlExecutionError(SqlRunnerError):
    """Raised when DuckDB cannot execute an otherwise permitted query."""


class SqlTimeoutError(SqlRunnerError):
    """Raised after DuckDB has been interrupted at the configured deadline."""


class SqlQueryResult(BaseModel):
    """Bounded, traceable output from one read-only SQL query."""

    query_id: str
    statement_type: StatementType
    columns: list[str]
    rows: list[list[Any]]
    row_count: int = Field(ge=0)
    truncated: bool
    duration_ms: float = Field(ge=0)


_FORBIDDEN_QUERY_NODES = (
    exp.DDL,
    exp.DML,
    exp.Into,
    exp.Command,
    exp.Execute,
    exp.Transaction,
    exp.Set,
    exp.Pragma,
    exp.Attach,
    exp.Detach,
    exp.LoadData,
    exp.Export,
    exp.Use,
    exp.Alter,
    exp.Drop,
    exp.Grant,
    exp.Revoke,
)


def _raise_validation(message: str, query_id: str) -> None:
    raise SqlValidationError(message, query_id)


def _parse_expressions(sql: str, query_id: str) -> list[exp.Expr]:
    try:
        parsed = sqlglot.parse(sql, read="duckdb")
    except SqlglotError as exc:
        raise SqlValidationError(f"invalid DuckDB SQL: {exc}", query_id) from exc

    expressions = [expression for expression in parsed if expression is not None]
    if len(expressions) != 1:
        _raise_validation("exactly one SQL statement is required", query_id)
    return expressions


def _ensure_query_is_read_only(expression: exp.Expr, query_id: str) -> None:
    if not isinstance(expression, exp.Query):
        _raise_validation("only SELECT/WITH queries are permitted here", query_id)

    forbidden = next(
        (
            node
            for node in expression.walk()
            if isinstance(node, _FORBIDDEN_QUERY_NODES)
        ),
        None,
    )
    if forbidden is not None:
        _raise_validation(
            f"query contains forbidden operation: {type(forbidden).__name__}",
            query_id,
        )


def _explain_target_sql(sql: str, query_id: str) -> str | None:
    try:
        outer_tokens = Tokenizer(dialect="duckdb").tokenize(sql)
    except SqlglotError as exc:
        raise SqlValidationError(f"invalid DuckDB SQL: {exc}", query_id) from exc

    if not outer_tokens:
        _raise_validation("SQL must not be empty", query_id)
    first = outer_tokens[0]
    if first.token_type != TokenType.COMMAND or first.text.upper() != "EXPLAIN":
        return None
    if len(outer_tokens) != 2 or outer_tokens[1].token_type != TokenType.STRING:
        _raise_validation("EXPLAIN must contain exactly one query", query_id)

    remainder = outer_tokens[1].text.strip()
    try:
        tokens = Tokenizer(dialect="duckdb").tokenize(remainder)
    except SqlglotError as exc:
        raise SqlValidationError(f"invalid EXPLAIN query: {exc}", query_id) from exc
    if not tokens:
        _raise_validation("EXPLAIN must contain a query", query_id)

    target_index = 0
    if tokens[0].token_type == TokenType.ANALYZE:
        target_index = 1
    elif tokens[0].token_type == TokenType.L_PAREN:
        depth = 0
        closing_index = None
        for index, token in enumerate(tokens):
            if token.token_type == TokenType.L_PAREN:
                depth += 1
            elif token.token_type == TokenType.R_PAREN:
                depth -= 1
                if depth == 0:
                    closing_index = index
                    break
        if closing_index is None:
            _raise_validation("EXPLAIN options contain unmatched parentheses", query_id)
        target_index = closing_index + 1

    if target_index >= len(tokens):
        _raise_validation("EXPLAIN must contain a query", query_id)
    return remainder[tokens[target_index].start :]


def validate_readonly_sql(sql: str, query_id: str | None = None) -> StatementType:
    """Validate one statement with SQLGlot and DuckDB's native parser."""
    current_query_id = query_id or str(uuid4())
    if not isinstance(sql, str) or not sql.strip():
        _raise_validation("SQL must be a non-empty string", current_query_id)

    explain_target = _explain_target_sql(sql, current_query_id)
    if explain_target is not None:
        expression = _parse_expressions(explain_target, current_query_id)[0]
        _ensure_query_is_read_only(expression, current_query_id)
        statement_type: StatementType = "EXPLAIN"
        expected_native_type = "EXPLAIN"
    else:
        expression = _parse_expressions(sql, current_query_id)[0]
        if isinstance(expression, exp.Describe):
            target = expression.this
            if not isinstance(target, (exp.Query, exp.Table)):
                _raise_validation(
                    "DESCRIBE is limited to a table or SELECT/WITH query",
                    current_query_id,
                )
            if isinstance(target, exp.Query):
                _ensure_query_is_read_only(target, current_query_id)
            statement_type = "DESCRIBE"
            expected_native_type = "SELECT"
        else:
            _ensure_query_is_read_only(expression, current_query_id)
            statement_type = "SELECT"
            expected_native_type = "SELECT"

    try:
        native_statements = duckdb.extract_statements(sql)
    except duckdb.Error as exc:
        raise SqlValidationError(
            f"invalid DuckDB SQL: {exc}", current_query_id
        ) from exc
    if len(native_statements) != 1:
        _raise_validation("exactly one SQL statement is required", current_query_id)
    if native_statements[0].type.name != expected_native_type:
        _raise_validation(
            f"DuckDB classified the statement as {native_statements[0].type.name}",
            current_query_id,
        )

    return statement_type


def run_readonly_sql(
    database_path: str | Path,
    sql: str,
    *,
    timeout_seconds: float = 10.0,
    max_rows: int = 1000,
) -> SqlQueryResult:
    """Execute one AST-validated query on a restricted DuckDB connection."""
    query_id = str(uuid4())
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    if max_rows <= 0:
        raise ValueError("max_rows must be greater than zero")

    statement_type = validate_readonly_sql(sql, query_id)
    resolved_path = Path(database_path).resolve()
    if not resolved_path.is_file():
        raise SqlExecutionError(
            f"DuckDB database does not exist: {resolved_path}", query_id
        )

    started_at = perf_counter()
    try:
        connection = duckdb.connect(
            str(resolved_path),
            read_only=True,
            config={"enable_external_access": "false"},
        )
    except duckdb.Error as exc:
        raise SqlExecutionError(
            f"could not open DuckDB database: {exc}", query_id
        ) from exc

    def execute() -> tuple[list[str], list[list[Any]], bool]:
        cursor = connection.execute(sql)
        columns = [description[0] for description in cursor.description or []]
        fetched = cursor.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        rows = [list(row) for row in fetched[:max_rows]]
        return columns, rows, truncated

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(execute)
            try:
                columns, rows, truncated = future.result(timeout=timeout_seconds)
            except FutureTimeoutError as exc:
                connection.interrupt()
                try:
                    future.result()
                except duckdb.Error:
                    pass
                raise SqlTimeoutError(
                    f"query exceeded the {timeout_seconds:g} second timeout",
                    query_id,
                ) from exc
            except duckdb.Error as exc:
                raise SqlExecutionError(
                    f"DuckDB query failed: {exc}", query_id
                ) from exc
    finally:
        connection.close()

    return SqlQueryResult(
        query_id=query_id,
        statement_type=statement_type,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        duration_ms=(perf_counter() - started_at) * 1000,
    )
