"""Rules for the tools for AI assistants (MCP) a provider declares in its manifest."""

from __future__ import annotations

import re
from typing import Any, Final

from music_assistant_models.auth import Scope
from music_assistant_models.provider import ProviderMCPTool, is_menu_action_label

# the number of MCP tools a provider may declare
MAX_MCP_TOOLS: Final = 10
MAX_MCP_TOOL_ID_LENGTH: Final = 32
# the longest tool name ("<domain>__<id>") that MCP clients commonly accept
MAX_MCP_TOOL_NAME_LENGTH: Final = 64
MAX_MCP_TOOL_DESCRIPTION_LENGTH: Final = 300
MAX_MCP_TOOL_PROPERTIES: Final = 10
MAX_MCP_TOOL_PROPERTY_NAME_LENGTH: Final = 32
MAX_MCP_TOOL_PROPERTY_DESCRIPTION_LENGTH: Final = 200
MAX_MCP_TOOL_ENUM_VALUES: Final = 50
MAX_MCP_TOOL_ENUM_VALUE_LENGTH: Final = 64
# single underscores only, so that no two providers can make the same name "<domain>__<id>"
MCP_TOOL_ID_PATTERN: Final = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
MCP_TOOL_PROPERTY_NAME_PATTERN: Final = re.compile(
    rf"[a-z][a-z0-9_]{{0,{MAX_MCP_TOOL_PROPERTY_NAME_LENGTH - 1}}}"
)
# the arguments the MCP server adds to a provider tool itself
RESERVED_MCP_TOOL_PROPERTIES: Final = frozenset({"instance", "player"})
MCP_TOOL_SCALAR_TYPES: Final = ("boolean", "integer", "number", "string")
MCP_TOOL_PROPERTY_TYPES: Final = ("array", *MCP_TOOL_SCALAR_TYPES)
_SCHEMA_KEYS: Final = frozenset({"type", "properties", "required"})
_PROPERTY_KEYS: Final = frozenset({"type", "description", "enum", "items", "default"})
_ITEMS_KEYS: Final = frozenset({"type", "enum"})


def read_mcp_tools(raw: object, domain: str) -> tuple[list[ProviderMCPTool], list[str]]:
    """
    Return the MCP tools of a provider that follow the rules, and why each other one is left out.

    Takes ``mcp_tools`` as it is in the manifest file. A tool is left out when a field has the
    wrong type, its id is malformed or makes the tool name ``<domain>__<id>`` longer than
    ``MAX_MCP_TOOL_NAME_LENGTH``, its description is not one readable line of at most
    ``MAX_MCP_TOOL_DESCRIPTION_LENGTH`` characters, its scope is unknown, its input schema
    breaks the rules, or its id is already taken. A tool without input schema takes no
    arguments. Only the first ``MAX_MCP_TOOLS`` valid tools are kept. Never raises.

    :param raw: The value of ``mcp_tools`` in the manifest of the provider.
    :param domain: The domain of the provider.
    """
    if not isinstance(raw, list):
        return [], ["'mcp_tools' must be a list"]
    kept: list[ProviderMCPTool] = []
    problems: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not _has_field_types(item):
            problems.append(
                f"MCP tool {index}: needs string id, description and scope, and optionally an "
                "object input_schema and a boolean requires_player"
            )
            continue
        tool = ProviderMCPTool.from_dict(item)
        if problem := _find_problem(tool, kept, domain):
            problems.append(f"MCP tool '{tool.id}': {problem}")
        else:
            kept.append(tool)
    return kept, problems


def matches_mcp_tool_type(value: object, spec: dict[str, Any]) -> bool:
    """
    Return True when a value matches the type (and enum) of a property or items schema.

    :param value: The value to check.
    :param spec: The schema of an MCP tool argument, or of the items of an array argument.
    """
    match spec["type"]:
        case "string":
            return isinstance(value, str) and value in spec.get("enum", [value])
        case "boolean":
            return isinstance(value, bool)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
        case "array":
            return isinstance(value, list) and all(
                matches_mcp_tool_type(entry, spec["items"]) for entry in value
            )
    return False


def _has_field_types(item: dict[str, object]) -> bool:
    """Return True when a declared MCP tool has the types its fields need."""
    return (
        all(isinstance(item.get(key), str) for key in ("id", "description", "scope"))
        and isinstance(item.get("input_schema", {}), dict)
        and isinstance(item.get("requires_player", False), bool)
    )


def _find_problem(tool: ProviderMCPTool, kept: list[ProviderMCPTool], domain: str) -> str | None:
    """
    Return what is wrong with a single MCP tool, None when it may be kept.

    :param tool: The MCP tool to check.
    :param kept: The tools of the provider that were kept so far.
    :param domain: The domain of the provider.
    """
    if len(tool.id) > MAX_MCP_TOOL_ID_LENGTH or not MCP_TOOL_ID_PATTERN.fullmatch(tool.id):
        return (
            "needs an id of lower case letters and digits, in groups joined by single "
            f"underscores (at most {MAX_MCP_TOOL_ID_LENGTH})"
        )
    if any(other.id == tool.id for other in kept):
        return "another MCP tool of this provider has the same id"
    if len(name := f"{domain}__{tool.id}") > MAX_MCP_TOOL_NAME_LENGTH:
        return (
            f"needs a shorter id: the tool name '{name}' has more than "
            f"{MAX_MCP_TOOL_NAME_LENGTH} characters"
        )
    if not is_menu_action_label(tool.description, MAX_MCP_TOOL_DESCRIPTION_LENGTH):
        return (
            f"needs a description of at most {MAX_MCP_TOOL_DESCRIPTION_LENGTH} characters on "
            "one line, that a reader can see"
        )
    if tool.scope == Scope.UNKNOWN:
        return "needs a scope that Music Assistant knows, such as 'library.read'"
    if tool.input_schema and (problem := _find_schema_problem(tool.input_schema)):
        return problem
    if len(kept) >= MAX_MCP_TOOLS:
        return f"a provider declares at most {MAX_MCP_TOOLS} MCP tools"
    return None


def _find_schema_problem(schema: dict[str, Any]) -> str | None:
    """
    Return what is wrong with the input schema of an MCP tool, None when it follows the rules.

    :param schema: The declared input schema.
    """
    properties = schema.get("properties", {})
    if schema.get("type") != "object" or not set(schema) <= _SCHEMA_KEYS:
        return "needs an input_schema of type 'object', with only properties and required"
    if not isinstance(properties, dict) or len(properties) > MAX_MCP_TOOL_PROPERTIES:
        return f"needs input_schema properties as an object of at most {MAX_MCP_TOOL_PROPERTIES}"
    for name, prop in properties.items():
        if name in RESERVED_MCP_TOOL_PROPERTIES:
            return f"input_schema property '{name}' is added by the server"
        if not MCP_TOOL_PROPERTY_NAME_PATTERN.fullmatch(name):
            return (
                f"input_schema property '{name}' needs a name of lower case letters, digits and "
                f"underscores that starts with a letter (at most {MAX_MCP_TOOL_PROPERTY_NAME_LENGTH})"
            )
        if problem := _find_property_problem(prop):
            return f"input_schema property '{name}' {problem}"
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(
        isinstance(key, str) and key in properties for key in required
    ):
        return "needs input_schema required to list only declared properties"
    return None


def _find_property_problem(prop: object) -> str | None:
    """
    Return what is wrong with a property of an input schema, None when it follows the rules.

    :param prop: The declared schema of the property.
    """
    if not isinstance(prop, dict) or not set(prop) <= _PROPERTY_KEYS:
        return f"needs an object with only the keys: {', '.join(sorted(_PROPERTY_KEYS))}"
    if prop.get("type") not in MCP_TOOL_PROPERTY_TYPES:
        return f"needs a type from: {', '.join(MCP_TOOL_PROPERTY_TYPES)}"
    description = prop.get("description")
    if not isinstance(description, str) or not is_menu_action_label(
        description, MAX_MCP_TOOL_PROPERTY_DESCRIPTION_LENGTH
    ):
        return (
            f"needs a description of at most {MAX_MCP_TOOL_PROPERTY_DESCRIPTION_LENGTH} "
            "characters on one line, that a reader can see"
        )
    if (prop["type"] == "array") != ("items" in prop):
        return "needs items when, and only when, its type is 'array'"
    items = prop.get("items", {})
    if prop["type"] == "array" and not (
        isinstance(items, dict)
        and set(items) <= _ITEMS_KEYS
        and items.get("type") in MCP_TOOL_SCALAR_TYPES
    ):
        return (
            f"needs items with a type from: {', '.join(MCP_TOOL_SCALAR_TYPES)}, "
            "and optionally an enum"
        )
    if not (_has_valid_enum(prop) and _has_valid_enum(items)):
        return (
            f"needs an enum only with type 'string', of 1 to {MAX_MCP_TOOL_ENUM_VALUES} strings "
            f"of at most {MAX_MCP_TOOL_ENUM_VALUE_LENGTH} characters"
        )
    if "default" in prop and not matches_mcp_tool_type(prop["default"], prop):
        return "needs a default of its type (and in its enum)"
    return None


def _has_valid_enum(spec: dict[str, Any]) -> bool:
    """Return True when a (property or items) schema has no enum, or an enum of strings."""
    if "enum" not in spec:
        return True
    enum = spec["enum"]
    return (
        spec["type"] == "string"
        and isinstance(enum, list)
        and 1 <= len(enum) <= MAX_MCP_TOOL_ENUM_VALUES
        and all(
            isinstance(value, str) and len(value) <= MAX_MCP_TOOL_ENUM_VALUE_LENGTH
            for value in enum
        )
    )
