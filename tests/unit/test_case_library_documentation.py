import re
from pathlib import Path

from config.faults import load_fault_catalog

CASE_LIBRARY_PATH = (
    Path(__file__).parents[2] / "benchmark" / "cases" / "anomaly-cases-xlj.md"
)


def test_case_library_documents_every_canonical_fault_with_two_evidence_paths() -> None:
    document = CASE_LIBRARY_PATH.read_text(encoding="utf-8")

    assert "12 类故障 x 5 个可复现案例 = 60 个 manifest" in document
    for fault in load_fault_catalog().faults:
        match = re.search(
            rf"^## {fault.id} `(?P<root_cause>[^`]+)`\n(?P<body>.*?)(?=^## |\Z)",
            document,
            flags=re.MULTILINE | re.DOTALL,
        )
        assert match is not None, f"{fault.id} is missing from the case library"
        assert match.group("root_cause") == fault.root_cause_type
        assert f"`{fault.id}-001`, `{fault.affected_metrics[0]}`" in match.group("body")
        for required_section in ("业务查询", "业务证据", "独立证据", "标准根因"):
            assert required_section in match.group("body")
