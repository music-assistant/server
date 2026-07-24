"""
Opt-in simplified tool discovery ("meta-tool" mode).

When the ``meta_tool_discovery`` config flag is on, the tool listing
collapses to three meta-tools: ``search_tools`` (BM25-ranked lookup over the
catalog, returning lightweight name/description entries), ``get_tool_schema``
(full schema for exactly one tool, on demand) and ``call_tool`` (proxy that
executes any catalogued tool). Hosts that load every tool schema up-front
save the ~20k-token catalog cost per session; hosts that defer schemas
(e.g. Claude) should leave the flag off.

RBAC holds by construction rather than by re-implementation: the search
catalog is fetched through ``list_tools(run_middleware=True)`` so
``TagFilterMiddleware`` filters it, the ``call_tool`` proxy re-enters
``FastMCP.call_tool`` with middleware enabled so disabled tools stay
blocked, and ``get_tool_schema`` re-checks tag visibility before
serializing. Catalogued tools remain directly callable while hidden
(FastMCP's search-transform design) — stale clients keep working and the
middleware still gates them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import NotFoundError
from fastmcp.server.transforms.search import BM25SearchTransform
from mcp.types import ToolAnnotations

from .middleware import tags_visible

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from fastmcp import FastMCP
    from fastmcp.server.transforms import GetToolNext
    from fastmcp.tools.base import Tool
    from fastmcp.utilities.versions import VersionSpec

    from .middleware import TagsLookup

GET_TOOL_SCHEMA_NAME = "get_tool_schema"
SEARCH_MAX_RESULTS = 10


def _light_results(tools: Sequence[Tool]) -> list[dict[str, str]]:
    """Serialize search hits as name + description only — schemas stay on demand."""
    return [{"name": t.name, "description": (t.description or "").strip()} for t in tools]


class MetaDiscoveryTransform(BM25SearchTransform):  # type: ignore[misc, unused-ignore]
    """
    BM25 search transform gated by a hot-swappable enabled flag.

    When disabled the transform is a pass-through (minus ``get_tool_schema``,
    which only makes sense alongside the other meta-tools), so flipping the
    config flag changes behaviour on the next request without rebuilding the
    FastMCP server.
    """

    def __init__(self, enabled: Callable[[], bool]) -> None:
        """
        Initialise the transform.

        :param enabled: Zero-arg callable returning the *current* state of the
            ``meta_tool_discovery`` flag, so config hot-swaps apply instantly.
        """
        super().__init__(
            max_results=SEARCH_MAX_RESULTS,
            always_visible=[GET_TOOL_SCHEMA_NAME],
            search_result_serializer=_light_results,
        )
        self._enabled = enabled

    async def transform_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        """Collapse the listing to the meta-tools, or pass through when disabled."""
        if not self._enabled():
            return [t for t in tools if t.name != GET_TOOL_SCHEMA_NAME]
        return await super().transform_tools(tools)

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        """Resolve synthetic tool names only while the mode is enabled."""
        if not self._enabled():
            if name == GET_TOOL_SCHEMA_NAME:
                return None
            return await call_next(name, version=version)
        return await super().get_tool(name, call_next, version=version)


def register_meta_discovery(
    mcp: FastMCP,
    *,
    enabled: Callable[[], bool],
    allowed_tags_provider: Callable[[], set[str]],
    lookup_component_tags: TagsLookup,
) -> None:
    """
    Register the ``get_tool_schema`` tool and install the discovery transform.

    :param mcp: Root FastMCP server (after all sub-servers are mounted).
    :param enabled: Zero-arg callable returning the current flag state.
    :param allowed_tags_provider: Zero-arg callable returning the currently
        allowed permission tags (same closure the tag-filter middleware uses).
    :param lookup_component_tags: Async ``(kind, key) -> tags | None`` resolver
        (see :func:`provider.server.build_tag_lookup`).
    """

    @mcp.tool(
        name=GET_TOOL_SCHEMA_NAME,
        annotations=ToolAnnotations(
            title="Get tool schema",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def get_tool_schema(tool_name: str) -> dict[str, Any]:
        """
        Return the full schema for one catalogued tool.

        Use ``search_tools`` first to find candidate tool names, then fetch
        the schema of the one you intend to invoke via ``call_tool``.
        Returns ``name``, ``description`` and ``inputSchema``, plus
        ``outputSchema`` and ``annotations`` when the tool declares them.

        :param tool_name: Exact tool name as returned by ``search_tools``.
        """
        tags = await lookup_component_tags("tool", tool_name)
        if not tags_visible(tags, allowed_tags_provider()):
            raise NotFoundError(f"Tool {tool_name!r} not found")
        tool = await mcp.get_tool(tool_name)
        if tool is None:
            raise NotFoundError(f"Tool {tool_name!r} not found")
        schema: dict[str, Any] = {
            "name": tool.name,
            "description": (tool.description or "").strip(),
            "inputSchema": tool.parameters,
        }
        if tool.output_schema is not None:
            schema["outputSchema"] = tool.output_schema
        if tool.annotations is not None:
            schema["annotations"] = tool.annotations.model_dump(exclude_none=True)
        return schema

    mcp.add_transform(MetaDiscoveryTransform(enabled=enabled))
