"""Tests for ToolAnnotations sweep across all sub-server tools (C5)."""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc"

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP

from music_assistant.providers.fastmcp_server.tools import (
    build_library_server,
    build_media_server,
    build_metadata_server,
    build_playback_server,
    build_players_server,
    build_playlists_server,
    build_queue_server,
    build_volume_server,
)

_BUILDERS = [
    ("library", build_library_server),
    ("queue", build_queue_server),
    ("playback", build_playback_server),
    ("players", build_players_server),
    ("playlists", build_playlists_server),
    ("volume", build_volume_server),
    ("media", build_media_server),
    ("metadata", build_metadata_server),
]


# Spec-mandated destructive tools per the C5 mapping table.
_DESTRUCTIVE_NAMES = {
    "queue_add_to_queue",
    "queue_clear_queue",
    "playlists_remove_tracks",
    "media_remove_from_favorites",
    "media_remove_from_library",
}

# Strictly read-only categories (every tool in these sub-servers).
_READONLY_NAMESPACES = {"library", "metadata"}
# Plus a couple of inspector tools elsewhere.
_READONLY_TOOL_NAMES = {
    "queue_get_active_queue",
    "players_list_players",
    "players_get_player",
}


@pytest.fixture
def mounted_server(mock_mass: Any) -> FastMCP:
    """Build a root FastMCP with all 8 sub-servers mounted, like MCPServerRuntime."""
    mcp: FastMCP = FastMCP(name="test")
    for ns, builder in _BUILDERS:
        mcp.mount(builder(mock_mass), namespace=ns)
    return mcp


async def test_every_tool_has_title_and_annotations(mounted_server: FastMCP) -> None:
    """Every exposed tool carries a non-empty title and a ToolAnnotations object."""
    async with Client(mounted_server) as client:
        tools = await client.list_tools()
    assert tools, "expected at least one tool"
    for tool in tools:
        ann = tool.annotations
        assert ann is not None, f"{tool.name}: missing annotations"
        assert ann.title, f"{tool.name}: missing title"
        # openWorldHint must be explicitly False — none of our tools touch the open web.
        assert ann.openWorldHint is False, f"{tool.name}: unexpected openWorldHint=True"


async def test_destructive_tools_are_marked(mounted_server: FastMCP) -> None:
    """Destructive operations are flagged so clients can prompt for confirmation."""
    async with Client(mounted_server) as client:
        tools = {t.name: t for t in await client.list_tools()}
    for name in _DESTRUCTIVE_NAMES:
        assert name in tools, f"missing tool {name}"
        assert tools[name].annotations.destructiveHint is True, name


async def test_readonly_tools_are_marked(mounted_server: FastMCP) -> None:
    """Read-only sub-servers (library, metadata) and a few inspectors carry readOnlyHint=True."""
    async with Client(mounted_server) as client:
        tools = await client.list_tools()
    for tool in tools:
        ns = tool.name.split("_", 1)[0]
        if ns in _READONLY_NAMESPACES or tool.name in _READONLY_TOOL_NAMES:
            assert tool.annotations.readOnlyHint is True, tool.name
            assert tool.annotations.destructiveHint is False, tool.name
