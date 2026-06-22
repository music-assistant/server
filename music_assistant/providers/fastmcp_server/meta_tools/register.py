"""Register search_tools and invoke_tool on the FastMCP root."""

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
        name = tool_name.strip()
        if not name:
            raise ToolError("tool_name is required")
        if name in META_TOOL_NAMES:
            raise ToolError(f"Cannot get schema for meta tool {name!r}")

        if not await is_tool_visible(name):
            msg = f"Tool {name!r} is currently disabled by configuration"
            raise ToolError(msg)

        tool = await get_tool(name)
        if tool is None:
            msg = f"Tool {name!r} not found"
            raise NotFoundError(msg)

        return json.dumps(build_tool_schema(tool), separators=(",", ":"))

    @mcp.tool(name=INVOKE_TOOL_NAME, description=INVOKE_TOOL_DESCRIPTION)  # type: ignore[untyped-decorator, unused-ignore]
    async def invoke_tool(
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Proxy a tools/call to any catalog tool (RBAC-checked)."""
        name = tool_name.strip()
        if not name:
            raise ToolError("tool_name is required")
        if name in META_TOOL_NAMES:
            raise ToolError(f"Cannot invoke meta tool {name!r} through invoke_tool")

        if not await is_tool_visible(name):
            msg = f"Tool {name!r} is currently disabled by configuration"
            raise ToolError(msg)

        tool = await get_tool(name)
        if tool is None:
            msg = f"Tool {name!r} not found"
            raise NotFoundError(msg)

        return await call_tool(name, arguments or {}, run_middleware=False)
