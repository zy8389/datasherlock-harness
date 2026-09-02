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


def test_streamlit_user_interface_is_localized_to_chinese() -> None:
    source = (APP_DIRECTORY / "streamlit_app.py").read_text(encoding="utf-8")

    required_labels = (
        "事件诊断运行台",
        "异常案例选择",
        "开始诊断",
        "调查计划",
        "工具调用轨迹",
        "根因与证据",
        "人工审批",
        "沙箱修复",
        "修复后验证",
        "冻结的基准快照",
    )
    forbidden_labels = (
        'st.subheader("Investigation Plan")',
        'st.subheader("Tool Trace")',
        'st.subheader("Human Approval")',
        'st.button("Start Diagnosis"',
        'st.tabs(["Incident Demo", "Benchmark Snapshot"])',
    )

    assert all(label in source for label in required_labels)
    assert all(label not in source for label in forbidden_labels)
