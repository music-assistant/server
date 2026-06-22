"""Tests for MCP meta-tool discovery (search_tools + invoke_tool)."""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc"

from __future__ import annotations

import json

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from music_assistant.providers.fastmcp_server.meta_tools import (
    DIRECT_CALL_BLOCKED,
    INVOKE_TOOL_NAME,
    SEARCH_TOOLS_NAME,
    MetaToolConfig,
    MetaToolMiddleware,
    build_tool_schema,
    register_meta_tools,
)
from music_assistant.providers.fastmcp_server.middleware import TagFilterMiddleware
from music_assistant.providers.fastmcp_server.server import build_tag_lookup


def _meta_config(*, enabled: bool = True) -> MetaToolConfig:
    return MetaToolConfig(enabled_provider=lambda: enabled)


def _build_server(
    *,
    allowed: set[str] | None = None,
    meta_enabled: bool = True,
) -> FastMCP:
    mcp: FastMCP = FastMCP(name="meta-test")
    allowed_tags = allowed if allowed is not None else {"query", "control"}
    lookup = build_tag_lookup(mcp)

    async def is_tool_visible(name: str) -> bool:
        tags = await lookup("tool", name)
        if tags is None:
            return False
        if not tags:
            return True
        return any(t in allowed_tags for t in tags)

    @mcp.tool(tags={"query"})  # type: ignore[untyped-decorator, unused-ignore]
    async def library_search_albums(query: str, limit: int = 25) -> str:
        """
        Search for albums by free-text query across all enabled music providers.

        Returns AlbumBrief items with uri, name, artists and year.
        """
        return f"albums:{query}:{limit}"

    @mcp.tool(tags={"query"})  # type: ignore[untyped-decorator, unused-ignore]
    async def library_search_tracks(query: str, limit: int = 25) -> str:
        """
        Search for tracks by free-text query across all enabled music providers.

        Returns brief track items. Use list_library_tracks for saved library only.
        """
        return f"results:{query}:{limit}"

    @mcp.tool(tags={"control"})  # type: ignore[untyped-decorator, unused-ignore]
    async def playback_play_media(queue_id: str, uri: str) -> str:
        """Load and start playing media on the given queue. uri: album, track, playlist."""
        return f"playing:{queue_id}:{uri}"

    @mcp.tool(tags={"control"})  # type: ignore[untyped-decorator, unused-ignore]
    async def playback_play_pause(queue_id: str) -> str:
        """Toggle play/pause on the given queue."""
        return f"toggle:{queue_id}"

    @mcp.tool(tags={"query"})  # type: ignore[untyped-decorator, unused-ignore]
    async def players_list_players() -> str:
        """List all players registered in Music Assistant with their state."""
        return "[]"

    register_meta_tools(
        mcp,
        call_tool=mcp.call_tool,
        list_tools=mcp.list_tools,
        get_tool=mcp.get_tool,
        is_tool_visible=is_tool_visible,
    )

    mcp.add_middleware(MetaToolMiddleware(_meta_config(enabled=meta_enabled)))
    mcp.add_middleware(TagFilterMiddleware(lambda: allowed_tags, lookup))
    return mcp


async def test_meta_off_lists_all_tools_except_meta() -> None:
    """When meta discovery is disabled, catalog tools are listed; meta tools are hidden."""
    mcp = _build_server(meta_enabled=False)
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert SEARCH_TOOLS_NAME not in names
    assert INVOKE_TOOL_NAME not in names
    assert "library_search_tracks" in names
    assert "playback_play_media" in names


async def test_meta_on_lists_only_meta_tools() -> None:
    """When enabled, tools/list exposes only search_tools and invoke_tool."""
    mcp = _build_server(meta_enabled=True)
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {SEARCH_TOOLS_NAME, INVOKE_TOOL_NAME}


async def test_direct_call_blocked_when_meta_enabled() -> None:
    """Direct tools/call fails with DIRECT_TOOL_CALL_BLOCKED when meta mode is on."""
    mcp = _build_server(meta_enabled=True)
    async with Client(mcp) as client:
        with pytest.raises(ToolError) as exc:
            await client.call_tool("library_search_tracks", {"query": "jazz"})
    assert DIRECT_CALL_BLOCKED in str(exc.value)


async def test_search_tools_natural_language_recommends_albums() -> None:
    """Natural-language queries match underscored tool names and rank best fit."""
    mcp = _build_server(meta_enabled=True)
    async with Client(mcp) as client:
        result = await client.call_tool(
            SEARCH_TOOLS_NAME,
            {"query": "library search albums artist", "limit": 5},
        )
    text_blocks = [c for c in result.content if hasattr(c, "text")]
    payload = json.loads(text_blocks[0].text)
    assert payload["count"] >= 1
    assert payload.get("recommended") == "library_search_albums"
    assert payload["tools"][0]["name"] == "library_search_albums"


async def test_search_tools_finds_matches_with_schema() -> None:
    """search_tools returns matching tools with input schemas."""
    mcp = _build_server(meta_enabled=True)
    async with Client(mcp) as client:
        result = await client.call_tool(
            SEARCH_TOOLS_NAME,
            {"query": "tracks", "include_schema": True},
        )
    text_blocks = [c for c in result.content if hasattr(c, "text")]
    payload = json.loads(text_blocks[0].text)
    assert payload["count"] >= 1
    match = next(t for t in payload["tools"] if t["name"] == "library_search_tracks")
    assert "query" in match["inputSchema"]["properties"]


async def test_invoke_tool_runs_target_tool() -> None:
    """invoke_tool proxies to the real tool and returns its result."""
    mcp = _build_server(meta_enabled=True)
    async with Client(mcp) as client:
        result = await client.call_tool(
            INVOKE_TOOL_NAME,
            {
                "tool_name": "library_search_tracks",
                "arguments": {"query": "jazz", "limit": 5},
            },
        )
    text_blocks = [c for c in result.content if hasattr(c, "text")]
    assert any("results:jazz:5" in c.text for c in text_blocks)


async def test_invoke_tool_respects_rbac() -> None:
    """invoke_tool rejects tools hidden by the tag filter."""
    mcp = _build_server(allowed={"query"}, meta_enabled=True)
    async with Client(mcp) as client:
        with pytest.raises(ToolError) as exc:
            await client.call_tool(
                INVOKE_TOOL_NAME,
                {
                    "tool_name": "playback_play_media",
                    "arguments": {"queue_id": "x", "uri": "library://album/1"},
                },
            )
    assert "disabled by configuration" in str(exc.value)


async def test_search_tools_respects_rbac() -> None:
    """search_tools omits tools hidden by the tag filter."""
    mcp = _build_server(allowed={"query"}, meta_enabled=True)
    async with Client(mcp) as client:
        result = await client.call_tool(SEARCH_TOOLS_NAME, {"query": "play"})
    text_blocks = [c for c in result.content if hasattr(c, "text")]
    payload = json.loads(text_blocks[0].text)
    names = {t["name"] for t in payload["tools"]}
    assert "playback_play_media" not in names


async def test_meta_tools_blocked_when_disabled() -> None:
    """Meta tools cannot be called when meta discovery is off."""
    mcp = _build_server(meta_enabled=False)
    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool(SEARCH_TOOLS_NAME, {"query": "tracks"})


async def test_search_play_intent_recommends_play_media() -> None:
    """Play intent should recommend playback_play_media and include workflow."""
    mcp = _build_server(meta_enabled=True)
    async with Client(mcp) as client:
        result = await client.call_tool(
            SEARCH_TOOLS_NAME,
            {"query": "playback play", "limit": 5},
        )
    payload = json.loads(next(c.text for c in result.content if hasattr(c, "text")))
    assert payload.get("recommended") == "playback_play_media"
    assert payload.get("workflow", {}).get("task") == "play_media_on_player"


async def test_build_tool_schema_serializes_annotations() -> None:
    """Tool schemas must json.dumps cleanly (ToolAnnotations is not raw-serializable)."""
    mcp = _build_server(meta_enabled=True)
    tool = await mcp.get_tool("library_search_tracks")
    assert tool is not None
    tool.annotations = ToolAnnotations(title="Search tracks", readOnlyHint=True)

    payload = build_tool_schema(tool)
    json.dumps(payload)
    assert payload["annotations"]["title"] == "Search tracks"
