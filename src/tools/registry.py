"""Metadata-only registry for tools that Planner plans may reference.

The registry deliberately does not execute tools.  It is the small catalog
that keeps the Planner's available-tool prompt and semantic validation aligned
with the tool implementations that are actually present in the repository.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolRegistryError(ValueError):
    """Base error for invalid registry metadata or tool arguments."""


class UnknownToolError(ToolRegistryError):
    """Raised when a plan references a tool absent from the registry."""


class ToolArgumentsError(ToolRegistryError):
    """Raised when tool arguments do not match the registered JSON schema."""


class ToolDefinition(BaseModel):
    """Provider-neutral metadata describing one executable tool contract."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    argument_schema: dict[str, Any]
    read_only: bool = True


class ToolRegistry:
    """Small dependency-injected catalog of available tool definitions."""

    def __init__(self, definitions: Iterable[ToolDefinition] = ()) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, tool: ToolDefinition) -> None:
        """Register one definition, rejecting duplicate tool names."""

        if tool.name in self._definitions:
            raise ToolRegistryError(f"tool is already registered: {tool.name}")
        self._definitions[tool.name] = tool.model_copy(deep=True)

    def get(self, name: str) -> ToolDefinition:
        """Return a registered definition or raise a stable unknown-tool error."""

        try:
            return self._definitions[name].model_copy(deep=True)
        except KeyError as exc:
            raise UnknownToolError(f"unknown tool: {name}") from exc

    def contains(self, name: str) -> bool:
        """Return whether ``name`` is registered."""

        return name in self._definitions

    def names(self) -> tuple[str, ...]:
        """Return registered names in deterministic registration order."""

        return tuple(self._definitions)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """Return defensive copies of all definitions in registration order."""

        return tuple(definition.model_copy(deep=True) for definition in self._definitions.values())

    def validate_arguments(self, name: str, arguments: Mapping[str, Any]) -> None:
        """Validate one argument object against the registered JSON schema."""

        definition = self.get(name)
        _validate_json_schema(arguments, definition.argument_schema, path="arguments")


def _validate_json_schema(value: object, schema: Mapping[str, Any], *, path: str) -> None:
    """Validate the small JSON Schema subset needed by tool contracts.

    The project currently has one tool definition, so pulling in a general
    schema package would add dependency weight for little value.  The
    implementation supports the common object/string/number/boolean/array
    constraints used by future simple tool definitions as well.
    """

    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, Mapping):
            raise ToolArgumentsError(f"{path} must be an object")
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise ToolArgumentsError(f"{path} has an invalid properties schema")
        required = schema.get("required", [])
        if not isinstance(required, list):
            raise ToolArgumentsError(f"{path} has an invalid required schema")
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise ToolArgumentsError(
                    f"{path} contains unknown field(s): {', '.join(map(str, unknown))}"
                )
        missing = [name for name in required if name not in value]
        if missing:
            raise ToolArgumentsError(
                f"{path} is missing required field(s): {', '.join(map(str, missing))}"
            )
        for key, child in value.items():
            child_schema = properties.get(key)
            if child_schema is not None:
                if not isinstance(child_schema, Mapping):
                    raise ToolArgumentsError(f"{path}.{key} has an invalid schema")
                _validate_json_schema(child, child_schema, path=f"{path}.{key}")
        return

    if expected_type == "string":
        if not isinstance(value, str):
            raise ToolArgumentsError(f"{path} must be a string")
        minimum = schema.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ToolArgumentsError(f"{path} must contain at least {minimum} characters")
        return

    if expected_type == "array":
        if not isinstance(value, list):
            raise ToolArgumentsError(f"{path} must be an array")
        minimum = schema.get("minItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ToolArgumentsError(f"{path} must contain at least {minimum} item(s)")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate_json_schema(item, item_schema, path=f"{path}[{index}]")
        if schema.get("uniqueItems") is True:
            for index, item in enumerate(value):
                if any(item == previous for previous in value[:index]):
                    raise ToolArgumentsError(f"{path} must contain unique item(s)")
        return

    if expected_type == "boolean":
        if not isinstance(value, bool):
            raise ToolArgumentsError(f"{path} must be a boolean")
        return

    if expected_type == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ToolArgumentsError(f"{path} must be a number")
        _validate_numeric_constraints(value, schema, path=path)
        return

    if expected_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ToolArgumentsError(f"{path} must be an integer")
        _validate_numeric_constraints(value, schema, path=path)
        return

    if expected_type == "null" and value is not None:
        raise ToolArgumentsError(f"{path} must be null")


def _validate_numeric_constraints(
    value: float,
    schema: Mapping[str, Any],
    *,
    path: str,
) -> None:
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and value < minimum:
        raise ToolArgumentsError(f"{path} must be greater than or equal to {minimum}")
    maximum = schema.get("maximum")
    if isinstance(maximum, (int, float)) and value > maximum:
        raise ToolArgumentsError(f"{path} must be less than or equal to {maximum}")
    exclusive_minimum = schema.get("exclusiveMinimum")
    if isinstance(exclusive_minimum, (int, float)) and value <= exclusive_minimum:
        raise ToolArgumentsError(f"{path} must be greater than {exclusive_minimum}")


SQL_QUERY_TOOL = ToolDefinition(
    name="sql_query",
    description=(
        "Execute one read-only SQL query against the DataSherlock DuckDB. "
        "Only safe read-only statements are allowed."
    ),
    argument_schema={
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "minLength": 1,
                "description": "Read-only SQL statement to execute.",
            }
        },
        "required": ["sql"],
        "additionalProperties": False,
    },
    read_only=True,
)


_SCOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "equals": {
            "type": "object",
            "description": "Equality filters, with scalar or list values.",
        },
        "time_column": {
            "type": "string",
            "minLength": 1,
            "description": "Timestamp column for a half-open time window.",
        },
        "start": {
            "type": "string",
            "minLength": 1,
            "description": "Timezone-aware ISO-8601 start timestamp.",
        },
        "end": {
            "type": "string",
            "minLength": 1,
            "description": "Timezone-aware ISO-8601 end timestamp.",
        },
    },
    "additionalProperties": False,
}


CHECK_NULL_RATE_TOOL = ToolDefinition(
    name="check_null_rate",
    description=(
        "Measure the null rate of one read-only table column, optionally within "
        "a dimension and time scope."
    ),
    argument_schema={
        "type": "object",
        "properties": {
            "table": {"type": "string", "minLength": 1},
            "column": {"type": "string", "minLength": 1},
            "threshold": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Maximum allowed null proportion from 0 to 1.",
            },
            "scope": _SCOPE_SCHEMA,
        },
        "required": ["table", "column"],
        "additionalProperties": False,
    },
    read_only=True,
)


CHECK_DUPLICATE_RATE_TOOL = ToolDefinition(
    name="check_duplicate_rate",
    description=(
        "Measure the proportion of duplicate rows for one or more key columns "
        "through the read-only SQL Runner."
    ),
    argument_schema={
        "type": "object",
        "properties": {
            "table": {"type": "string", "minLength": 1},
            "keys": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
            "threshold": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Maximum allowed duplicate proportion from 0 to 1.",
            },
        },
        "required": ["table", "keys"],
        "additionalProperties": False,
    },
    read_only=True,
)


CHECK_FRESHNESS_TOOL = ToolDefinition(
    name="check_freshness",
    description=(
        "Check whether the latest timestamp in a table is within a fixed age "
        "allowance relative to an explicit UTC reference time."
    ),
    argument_schema={
        "type": "object",
        "properties": {
            "table": {"type": "string", "minLength": 1},
            "timestamp_column": {"type": "string", "minLength": 1},
            "reference_time": {
                "type": "string",
                "minLength": 1,
                "description": "Timezone-aware ISO-8601 reference timestamp.",
            },
            "max_age": {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": "Positive freshness allowance in seconds.",
            },
            "scope": _SCOPE_SCHEMA,
        },
        "required": ["table", "timestamp_column", "reference_time", "max_age"],
        "additionalProperties": False,
    },
    read_only=True,
)


DETECT_SCHEMA_DRIFT_TOOL = ToolDefinition(
    name="detect_schema_drift",
    description=(
        "Compare the two latest schema snapshots for a table and report added, "
        "removed, or type-changed fields."
    ),
    argument_schema={
        "type": "object",
        "properties": {"table": {"type": "string", "minLength": 1}},
        "required": ["table"],
        "additionalProperties": False,
    },
    read_only=True,
)


DETECT_DISTRIBUTION_DRIFT_TOOL = ToolDefinition(
    name="detect_distribution_drift",
    description=(
        "Compare categorical distributions in two explicit time windows using "
        "total variation distance."
    ),
    argument_schema={
        "type": "object",
        "properties": {
            "table": {"type": "string", "minLength": 1},
            "column": {"type": "string", "minLength": 1},
            "time_column": {"type": "string", "minLength": 1},
            "baseline_start": {"type": "string", "minLength": 1},
            "baseline_end": {"type": "string", "minLength": 1},
            "current_start": {"type": "string", "minLength": 1},
            "current_end": {"type": "string", "minLength": 1},
            "threshold": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Maximum allowed total variation distance from 0 to 1.",
            },
        },
        "required": [
            "table",
            "column",
            "time_column",
            "baseline_start",
            "baseline_end",
            "current_start",
            "current_end",
        ],
        "additionalProperties": False,
    },
    read_only=True,
)


DATA_QUALITY_TOOLS = (
    CHECK_NULL_RATE_TOOL,
    CHECK_DUPLICATE_RATE_TOOL,
    CHECK_FRESHNESS_TOOL,
    DETECT_SCHEMA_DRIFT_TOOL,
    DETECT_DISTRIBUTION_DRIFT_TOOL,
)


def build_default_tool_registry() -> ToolRegistry:
    """Create a fresh registry containing the tools implemented today."""

    return ToolRegistry((SQL_QUERY_TOOL, *DATA_QUALITY_TOOLS))


__all__ = [
    "CHECK_DUPLICATE_RATE_TOOL",
    "CHECK_FRESHNESS_TOOL",
    "CHECK_NULL_RATE_TOOL",
    "DATA_QUALITY_TOOLS",
    "DETECT_DISTRIBUTION_DRIFT_TOOL",
    "DETECT_SCHEMA_DRIFT_TOOL",
    "SQL_QUERY_TOOL",
    "ToolArgumentsError",
    "ToolDefinition",
    "ToolRegistry",
    "ToolRegistryError",
    "UnknownToolError",
    "build_default_tool_registry",
]
