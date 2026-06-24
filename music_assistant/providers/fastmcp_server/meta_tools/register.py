"""Register search_tools, get_tool_schema, and invoke_tool on the FastMCP root."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import NotFoundError, ToolError

from .catalog import search_tool_catalog
from .constants import (
    GET_TOOL_SCHEMA_NAME,
    INVOKE_TOOL_NAME,
    META_TOOL_NAMES,
    SEARCH_TOOLS_NAME,
)
from .middleware import (
    GET_TOOL_SCHEMA_DESCRIPTION,
    INVOKE_TOOL_DESCRIPTION,
    SEARCH_TOOLS_DESCRIPTION,
)
from .schema import build_tool_schema

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


async def _resolve_catalog_tool(
    raw_name: str,
    *,
    get_tool: Callable[[str], Awaitable[Any]],
    is_tool_visible: Callable[[str], Awaitable[bool]],
    reject_meta: str,
) -> tuple[str, Any]:
    """
    Validate *raw_name* and return the resolved catalog tool.

    :param reject_meta: ``ToolError`` message when *raw_name* is a meta tool.
    """
    name = raw_name.strip()
    if not name:
        raise ToolError("tool_name is required")
    if name in META_TOOL_NAMES:
        raise ToolError(reject_meta.format(name=name))
    if not await is_tool_visible(name):
        msg = f"Tool {name!r} is currently disabled by configuration"
        raise ToolError(msg)
    tool = await get_tool(name)
    if tool is None:
        msg = f"Tool {name!r} not found"
        raise NotFoundError(msg)
    return name, tool


def register_meta_tools(
    mcp: Any,
    *,
    call_tool: Callable[..., Awaitable[Any]],
    list_tools: Callable[..., Awaitable[Any]],
    get_tool: Callable[[str], Awaitable[Any]],
    is_tool_visible: Callable[[str], Awaitable[bool]],
) -> None:
    """
    Register meta-tool discovery helpers on the FastMCP root.

    Tools are always registered so toggles can hot-swap via
    :class:`MetaToolMiddleware`; listing and direct calls are gated when disabled.
    """

    @mcp.tool(name=SEARCH_TOOLS_NAME, description=SEARCH_TOOLS_DESCRIPTION)  # type: ignore[untyped-decorator, unused-ignore]
    async def search_tools(
        query: str,
        limit: int = 25,
    ) -> str:
        """Search tools by keyword; returns lightweight JSON matches (no schemas)."""
        payload = await search_tool_catalog(
            query,
            list_tools=list_tools,
            is_tool_visible=is_tool_visible,
            limit=limit,
        )
        return json.dumps(payload, separators=(",", ":"))

    @mcp.tool(name=GET_TOOL_SCHEMA_NAME, description=GET_TOOL_SCHEMA_DESCRIPTION)  # type: ignore[untyped-decorator, unused-ignore]
    async def get_tool_schema(tool_name: str) -> str:
        """Return the full schema for a single catalog tool (RBAC-checked)."""
        _, tool = await _resolve_catalog_tool(
            tool_name,
            get_tool=get_tool,
            is_tool_visible=is_tool_visible,
            reject_meta="Cannot get schema for meta tool {name!r}",
        )
        return json.dumps(build_tool_schema(tool), separators=(",", ":"))

    @mcp.tool(name=INVOKE_TOOL_NAME, description=INVOKE_TOOL_DESCRIPTION)  # type: ignore[untyped-decorator, unused-ignore]
    async def invoke_tool(
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Proxy a tools/call to any catalog tool (RBAC-checked)."""
        name, _ = await _resolve_catalog_tool(
            tool_name,
            get_tool=get_tool,
            is_tool_visible=is_tool_visible,
            reject_meta="Cannot invoke meta tool {name!r} through invoke_tool",
        )
        return await call_tool(name, arguments or {}, run_middleware=False)
