from __future__ import annotations

import os
from typing import Any

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from api.health import _check_duckdb

app = FastAPI(title="DataSherlock Harness API", version="0.1.0")


def _check_postgres() -> str:
    connection = psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        dbname=os.getenv("POSTGRES_DB", "datasherlock"),
        user=os.getenv("POSTGRES_USER", "datasherlock"),
        password=os.getenv("POSTGRES_PASSWORD", "change-me"),
        connect_timeout=2,
    )
    try:
        connection.execute("SELECT 1")
    finally:
        connection.close()
    return "ok"


def _dependency_status(check) -> str:
    try:
        return check()
    except Exception:
        return "unavailable"


@app.get("/health")
def health() -> JSONResponse:
    dependencies: dict[str, str] = {
        "duckdb": _dependency_status(_check_duckdb),
        "postgres": _dependency_status(_check_postgres),
    }
    healthy = all(status == "ok" for status in dependencies.values())
    payload: dict[str, Any] = {
        "status": "ok" if healthy else "degraded",
        "service": "api",
        "dependencies": dependencies,
    }
    return JSONResponse(status_code=200 if healthy else 503, content=payload)
