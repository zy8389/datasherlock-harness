import pytest

from tools.registry import (
    SQL_QUERY_TOOL,
    ToolArgumentsError,
    ToolDefinition,
    ToolRegistry,
    UnknownToolError,
    build_default_tool_registry,
)


def test_default_registry_contains_only_real_readonly_tool() -> None:
    registry = build_default_tool_registry()

    assert registry.names() == ("sql_query",)
    assert registry.get("sql_query") == SQL_QUERY_TOOL
    assert registry.get("sql_query").read_only is True


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
