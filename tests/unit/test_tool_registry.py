import pytest

from tools.registry import (
    CHECK_DUPLICATE_RATE_TOOL,
    CHECK_FRESHNESS_TOOL,
    CHECK_NULL_RATE_TOOL,
    DATA_QUALITY_TOOLS,
    DETECT_DISTRIBUTION_DRIFT_TOOL,
    DETECT_SCHEMA_DRIFT_TOOL,
    SQL_QUERY_TOOL,
    ToolArgumentsError,
    ToolDefinition,
    ToolRegistry,
    UnknownToolError,
    build_default_tool_registry,
)


def test_default_registry_contains_all_real_readonly_tools() -> None:
    registry = build_default_tool_registry()

    assert registry.names() == (
        "sql_query",
        "check_null_rate",
        "check_duplicate_rate",
        "check_freshness",
        "detect_schema_drift",
        "detect_distribution_drift",
    )
    assert registry.get("sql_query") == SQL_QUERY_TOOL
    assert all(registry.get(name).read_only for name in registry.names())


def test_data_quality_definitions_match_planner_argument_contracts() -> None:
    registry = build_default_tool_registry()

    expected = {
        tool.name: tool.argument_schema
        for tool in (
            CHECK_NULL_RATE_TOOL,
            CHECK_DUPLICATE_RATE_TOOL,
            CHECK_FRESHNESS_TOOL,
            DETECT_SCHEMA_DRIFT_TOOL,
            DETECT_DISTRIBUTION_DRIFT_TOOL,
        )
    }
    assert tuple(tool.name for tool in DATA_QUALITY_TOOLS) == tuple(expected)
    for name, schema in expected.items():
        assert registry.get(name).argument_schema == schema

    registry.validate_arguments(
        "check_null_rate",
        {"table": "events", "column": "user_id", "threshold": 0.01},
    )
    registry.validate_arguments(
        "check_duplicate_rate", {"table": "events", "keys": ["event_id"]}
    )
    registry.validate_arguments(
        "check_freshness",
        {
            "table": "events",
            "timestamp_column": "event_time",
            "reference_time": "2026-01-30T12:00:00+00:00",
            "max_age": 3600,
        },
    )
    registry.validate_arguments("detect_schema_drift", {"table": "events"})
    registry.validate_arguments(
        "detect_distribution_drift",
        {
            "table": "events",
            "column": "event_name",
            "time_column": "event_time",
            "baseline_start": "2026-01-29T00:00:00+00:00",
            "baseline_end": "2026-01-30T00:00:00+00:00",
            "current_start": "2026-01-30T00:00:00+00:00",
            "current_end": "2026-01-31T00:00:00+00:00",
        },
    )


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("check_null_rate", {"table": "events"}),
        ("check_duplicate_rate", {"table": "events"}),
        ("check_freshness", {"table": "events", "timestamp_column": "event_time"}),
        ("detect_schema_drift", {}),
        (
            "detect_distribution_drift",
            {"table": "events", "column": "event_name"},
        ),
    ],
)
def test_data_quality_registry_rejects_missing_arguments(
    tool: str, arguments: dict[str, object]
) -> None:
    with pytest.raises(ToolArgumentsError, match="missing required"):
        build_default_tool_registry().validate_arguments(tool, arguments)


def test_data_quality_registry_rejects_wrong_types_and_unknown_arguments() -> None:
    registry = build_default_tool_registry()

    with pytest.raises(ToolArgumentsError, match="must be a number"):
        registry.validate_arguments(
            "check_null_rate", {"table": "events", "column": "user_id", "threshold": "0.1"}
        )
    with pytest.raises(ToolArgumentsError, match="unknown field"):
        registry.validate_arguments(
            "detect_schema_drift", {"table": "events", "database_path": "x"}
        )
    with pytest.raises(ToolArgumentsError, match="less than or equal to 1"):
        registry.validate_arguments(
            "check_null_rate", {"table": "events", "column": "user_id", "threshold": 1.1}
        )
    with pytest.raises(ToolArgumentsError, match="greater than 0"):
        registry.validate_arguments(
            "check_freshness",
            {
                "table": "events",
                "timestamp_column": "event_time",
                "reference_time": "2026-01-30T12:00:00+00:00",
                "max_age": 0,
            },
        )
    with pytest.raises(ToolArgumentsError, match="unique"):
        registry.validate_arguments(
            "check_duplicate_rate", {"table": "events", "keys": ["event_id", "event_id"]}
        )


@pytest.mark.parametrize(
    "keys",
    [
        [["event_id"]],
        [{"column": "event_id"}],
    ],
)
def test_duplicate_rate_rejects_unhashable_items_as_tool_arguments(
    keys: list[object],
) -> None:
    registry = build_default_tool_registry()

    with pytest.raises(ToolArgumentsError, match=r"arguments\.keys\[0\]"):
        registry.validate_arguments(
            "check_duplicate_rate",
            {"table": "events", "keys": keys},
        )


def test_duplicate_rate_rejects_duplicate_string_keys_as_tool_arguments() -> None:
    with pytest.raises(ToolArgumentsError, match="unique"):
        build_default_tool_registry().validate_arguments(
            "check_duplicate_rate",
            {"table": "events", "keys": ["event_id", "event_id"]},
        )


def test_registry_validates_required_and_unknown_arguments() -> None:
    registry = build_default_tool_registry()

    registry.validate_arguments("sql_query", {"sql": "SELECT 1"})

    with pytest.raises(ToolArgumentsError, match="missing required"):
        registry.validate_arguments("sql_query", {})
    with pytest.raises(ToolArgumentsError, match="unknown field"):
        registry.validate_arguments("sql_query", {"query": "SELECT 1"})


def test_registry_rejects_unknown_tool_and_duplicate_registration() -> None:
    registry = build_default_tool_registry()

    with pytest.raises(UnknownToolError, match="unknown tool"):
        registry.get("magic_tool")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(SQL_QUERY_TOOL)


def test_registry_keeps_definition_metadata_independent_from_callers() -> None:
    registry = ToolRegistry((SQL_QUERY_TOOL,))
    definition = registry.get("sql_query")
    definition.argument_schema["properties"]["sql"]["description"] = "changed"

    assert registry.get("sql_query").argument_schema["properties"]["sql"][
        "description"
    ] != "changed"


def test_tool_definition_forbids_unexpected_metadata() -> None:
    with pytest.raises(ValueError):
        ToolDefinition(
            name="custom",
            description="custom tool",
            argument_schema={"type": "object"},
            execute=lambda: None,
        )
