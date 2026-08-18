from __future__ import annotations

import os
from pathlib import Path

import duckdb

REQUIRED_DUCKDB_TABLES = {
    "users",
    "events",
    "subscriptions",
    "experiment_assignments",
    "daily_metrics",
}


def _check_duckdb() -> str:
    database_path = Path(
        os.getenv("DUCKDB_PATH", "/workspace/data/processed/datasherlock.duckdb")
    )
    if not database_path.is_file():
        raise FileNotFoundError(f"DuckDB database does not exist: {database_path}")

    connection = duckdb.connect(
        str(database_path),
        read_only=True,
        config={"enable_external_access": "false"},
    )
    try:
        table_names = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        missing_tables = REQUIRED_DUCKDB_TABLES - table_names
        if missing_tables:
            missing = ", ".join(sorted(missing_tables))
            raise RuntimeError(f"DuckDB database is missing required tables: {missing}")
    finally:
        connection.close()
    return "ok"
