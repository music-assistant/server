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
    GET_TOOL_SCHEMA_NAME,
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
                "fetch_args_with": GET_TOOL_SCHEMA_NAME,
                "example": {"tool_name": tool_name},
            },
        }
    )


SEARCH_TOOLS_DESCRIPTION = """\
Search the Music Assistant MCP tool catalog by keyword or natural phrase.

Queries use token matching — spaces and underscores are equivalent, so
"library search albums" matches ``library_search_albums``.

Results are lightweight (name, description, score) with no inlined schemas.
Use get_tool_schema for a tool's arguments, then invoke_tool to run it.
Responses may include ``recommended`` or a task-specific ``workflow`` payload.

Tool names use ``namespace_action`` (library_, players_, playback_, queue_, …).
``search_*`` finds items by text; ``list_*`` enumerates (often no required args);
``get_*`` resolves or drills down and usually needs a uri or id from a prior step —
pass outputs forward rather than re-invoking with ``{}``."""


GET_TOOL_SCHEMA_DESCRIPTION = """\
Fetch the full inputSchema for one catalog tool (name from search_tools).

Returns all argument properties and which are required — call before invoke_tool
when args are not obvious. RBAC permissions still apply."""


INVOKE_TOOL_DESCRIPTION = """\
Invoke any Music Assistant MCP tool by name.

Pass tool_name from search_tools and arguments matching its inputSchema
(check ``required``; include optional keys when useful). RBAC permissions still apply."""
