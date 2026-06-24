"""
Middleware for MCP meta-tool discovery.

When meta-tool discovery is enabled:

* ``tools/list`` returns only ``search_tools``, ``get_tool_schema``, and ``invoke_tool``.
* Direct ``tools/call`` to any other tool is blocked with a hint to use ``invoke_tool``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

from .constants import (
    DIRECT_CALL_BLOCKED,
    DIRECT_CALL_BLOCKED_MESSAGE,
    INVOKE_TOOL_NAME,
    META_TOOL_NAMES,
    META_TOOLS_DISABLED_MESSAGE,
)

if TYPE_CHECKING:
    from fastmcp.server.middleware.middleware import CallNext, MiddlewareContext


class MetaToolMiddleware(Middleware):  # type: ignore[misc, unused-ignore]
    """Expose only meta tools in listings; route execution through invoke_tool."""

    def __init__(self, enabled_provider: Callable[[], bool]) -> None:
        """
        Store the hot-swappable discovery toggle.

        :param enabled_provider: Callable returning the live toggle state so
            the value can be hot-swapped without rebuilding the middleware.
        """
        super().__init__()
        self._enabled = enabled_provider

    async def on_list_tools(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Sequence[Any]],
    ) -> Sequence[Any]:
        """Show only meta tools when enabled, otherwise hide them from listings."""
        items = await call_next(context)
        if self._enabled():
            return [t for t in items if _tool_name(t) in META_TOOL_NAMES]
        return [t for t in items if _tool_name(t) not in META_TOOL_NAMES]

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        """Route catalog calls through invoke_tool when enabled; block direct calls."""
        name = str(getattr(context.message, "name", "") or "")
        if self._enabled():
            if name not in META_TOOL_NAMES:
                raise ToolError(_direct_call_blocked_error(name))
            return await call_next(context)
        if name in META_TOOL_NAMES:
            raise ToolError(META_TOOLS_DISABLED_MESSAGE)
        return await call_next(context)


def _tool_name(component: Any) -> str:
    return str(getattr(component, "name", "") or "")


def _direct_call_blocked_error(tool_name: str) -> str:
    return json.dumps(
        {
            "error": DIRECT_CALL_BLOCKED,
            "message": DIRECT_CALL_BLOCKED_MESSAGE.format(tool_name=tool_name),
            "tool": tool_name,
            "hint": {
                "use": INVOKE_TOOL_NAME,
                "example": {
                    "tool_name": tool_name,
                    "arguments": {},
                },
            },
        }
    )


SEARCH_TOOLS_DESCRIPTION = """\
Search the Music Assistant MCP tool catalog by keyword or natural phrase.

Queries use token matching — spaces and underscores are equivalent, so
"library search albums" matches ``library_search_albums``.

Results are always lightweight (name, description, score) — to keep them cheap there
is no option to inline schemas. Fetch a tool's arguments with get_tool_schema instead.

WORKFLOW:
1. Call search_tools (e.g. query="library search albums", "playback play media", "players list").
   Prefer ``recommended`` when present and follow ``workflow`` for multi-step tasks.
2. Call get_tool_schema with the chosen tool_name to fetch its arguments before invoking.
3. Call invoke_tool with tool_name and arguments matching that schema.

Playing music on a speaker is always multi-step: search URI → list players → playback_play_media."""


GET_TOOL_SCHEMA_DESCRIPTION = """\
Fetch the full schema for a single Music Assistant MCP tool.

Pass the exact tool_name from search_tools (e.g. library_search_tracks) to get its
name, description, inputSchema (and outputSchema/annotations when present). Use this
after search_tools and before invoke_tool to learn a tool's arguments without paying
for every search match's schema. RBAC permissions still apply."""


INVOKE_TOOL_DESCRIPTION = """\
Invoke any Music Assistant MCP tool by name.

Pass the exact tool_name from search_tools (e.g. library_search_tracks) and a JSON
arguments object matching that tool's inputSchema (from get_tool_schema). RBAC
permissions still apply — tools disabled in MCP Server settings cannot be invoked."""
