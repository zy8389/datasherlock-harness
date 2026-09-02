from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
APP_DIRECTORY = ROOT / "app"
FORBIDDEN_PREFIXES = (
    "harness",
    "benchmark.runner",
    "benchmark.case_generator",
    "duckdb",
)


def test_streamlit_frontend_imports_only_presentation_dependencies() -> None:
    violations: list[str] = []
    for path in sorted(APP_DIRECTORY.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if module.startswith(FORBIDDEN_PREFIXES):
                    violations.append(f"{path.name}:{node.lineno}: {module}")

    assert violations == []


def test_streamlit_uses_the_http_api_client_boundary() -> None:
    source = (APP_DIRECTORY / "streamlit_app.py").read_text(encoding="utf-8")

    assert "from app.api_client import" in source
    assert "DemoApiClient" in source
