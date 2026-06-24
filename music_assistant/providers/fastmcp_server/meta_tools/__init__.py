"""MCP meta-tool discovery (search_tools, get_tool_schema, invoke_tool)."""

from __future__ import annotations

from .constants import (
    DIRECT_CALL_BLOCKED,
    GET_TOOL_SCHEMA_NAME,
    INVOKE_TOOL_NAME,
    META_TOOL_NAMES,
    SEARCH_TOOLS_NAME,
)
from .middleware import MetaToolMiddleware
from .register import register_meta_tools
from .schema import build_tool_schema

__all__ = [
    "DIRECT_CALL_BLOCKED",
    "GET_TOOL_SCHEMA_NAME",
    "INVOKE_TOOL_NAME",
    "META_TOOL_NAMES",
    "SEARCH_TOOLS_NAME",
    "MetaToolMiddleware",
    "build_tool_schema",
    "register_meta_tools",
]
