"""Constants for MCP meta-tool discovery."""

from __future__ import annotations

SEARCH_TOOLS_NAME = "search_tools"
GET_TOOL_SCHEMA_NAME = "get_tool_schema"
INVOKE_TOOL_NAME = "invoke_tool"

META_TOOL_NAMES: frozenset[str] = frozenset(
    {SEARCH_TOOLS_NAME, GET_TOOL_SCHEMA_NAME, INVOKE_TOOL_NAME}
)

DIRECT_CALL_BLOCKED = "DIRECT_TOOL_CALL_BLOCKED"

DIRECT_CALL_BLOCKED_MESSAGE = (
    "Meta-tool discovery is enabled. Use invoke_tool with tool_name={tool_name!r} "
    "and arguments={{...}} instead of calling this tool directly. "
    "Use search_tools to discover tools, then get_tool_schema for the chosen "
    "tool's arguments."
)

META_TOOLS_DISABLED_MESSAGE = (
    "Simplified tool discovery is disabled. Enable it under Server in MCP Server "
    "settings, or call tools directly."
)
