from pathlib import Path

import duckdb
import pytest

from api.health import REQUIRED_DUCKDB_TABLES, _check_duckdb


def test_duckdb_health_check_rejects_missing_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "missing.duckdb"
    monkeypatch.setenv("DUCKDB_PATH", str(database_path))

    with pytest.raises(FileNotFoundError, match="does not exist"):
        _check_duckdb()

    assert not database_path.exists()


def test_duckdb_health_check_rejects_database_without_required_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.duckdb"
    with duckdb.connect(str(database_path)):
        pass
    monkeypatch.setenv("DUCKDB_PATH", str(database_path))

    with pytest.raises(RuntimeError, match="missing required tables"):
        _check_duckdb()


def test_duckdb_health_check_accepts_complete_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "complete.duckdb"
    with duckdb.connect(str(database_path)) as connection:
        for table_name in REQUIRED_DUCKDB_TABLES:
            connection.execute(f"CREATE TABLE {table_name} (id INTEGER)")
    monkeypatch.setenv("DUCKDB_PATH", str(database_path))

    assert _check_duckdb() == "ok"


def test_duckdb_health_check_uses_runner_compatible_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "complete.duckdb"
    with duckdb.connect(str(database_path)) as connection:
        for table_name in REQUIRED_DUCKDB_TABLES:
            connection.execute(f"CREATE TABLE {table_name} (id INTEGER)")
    monkeypatch.setenv("DUCKDB_PATH", str(database_path))

    with duckdb.connect(
        str(database_path),
        read_only=True,
        config={"enable_external_access": "false"},
    ):
        assert _check_duckdb() == "ok"
