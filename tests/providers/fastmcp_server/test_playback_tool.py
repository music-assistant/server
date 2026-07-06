"""Tests for the ``playback`` sub-server tools."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import BrowseFolder

from music_assistant.providers.fastmcp_server.tools import build_playback_server


@pytest.fixture
def mounted_playback(mock_mass: Any) -> Iterator[FastMCP]:
    """Build a root FastMCP with only the playback sub-server mounted."""
    mcp: FastMCP = FastMCP(name="test")
    mcp.mount(build_playback_server(mock_mass), namespace="playback")
    try:
        yield mcp
    finally:
        close = getattr(mcp, "close", None) or getattr(mcp, "shutdown", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


async def test_pause_calls_player_queues_pause(mock_mass: Any, mounted_playback: FastMCP) -> None:
    """``playback_pause`` always pauses via the non-toggling queue API."""
    async with Client(mounted_playback) as client:
        await client.call_tool("playback_pause", {"queue_id": "player1"})
    mock_mass.player_queues.pause.assert_awaited_once_with("player1")


async def test_resume_calls_player_queues_resume(mock_mass: Any, mounted_playback: FastMCP) -> None:
    """``playback_resume`` always resumes via the non-toggling queue API."""
    async with Client(mounted_playback) as client:
        await client.call_tool("playback_resume", {"queue_id": "player1"})
    mock_mass.player_queues.resume.assert_awaited_once_with("player1")


async def test_play_pause_calls_player_queues_play_pause(
    mock_mass: Any, mounted_playback: FastMCP
) -> None:
    """``playback_play_pause`` forwards to the toggling queue API."""
    async with Client(mounted_playback) as client:
        await client.call_tool("playback_play_pause", {"queue_id": "player1"})
    mock_mass.player_queues.play_pause.assert_awaited_once_with("player1")


async def test_play_media_enqueues_uri_directly(mock_mass: Any, mounted_playback: FastMCP) -> None:
    """``playback_play_media`` plays the given URI as-is (no radio)."""
    async with Client(mounted_playback) as client:
        await client.call_tool(
            "playback_play_media",
            {"queue_id": "player1", "uri": "spotify://track/abc"},
        )
    mock_mass.music.get_item_by_uri.assert_not_awaited()
    mock_mass.player_queues.play_media.assert_awaited_once_with("player1", "spotify://track/abc")


async def test_play_media_radio_enqueues_radio_playlist_uri(
    mock_mass: Any, mounted_playback: FastMCP
) -> None:
    """``radio=True`` resolves the seed and enqueues its radio_playlist:// dynamic playlist."""
    seed = MagicMock()
    seed.uri = "spotify://artist/abc"
    mock_mass.music.get_item_by_uri.return_value = seed
    async with Client(mounted_playback) as client:
        await client.call_tool(
            "playback_play_media",
            {"queue_id": "player1", "uri": "spotify://artist/abc", "radio": True},
        )
    mock_mass.music.get_item_by_uri.assert_awaited_once_with("spotify://artist/abc")
    mock_mass.player_queues.play_media.assert_awaited_once_with(
        "player1", "radio_playlist://playlist/spotify://artist/abc"
    )


async def test_play_media_radio_rejects_browse_folder(
    mock_mass: Any, mounted_playback: FastMCP
) -> None:
    """A browse folder is not a playable radio seed; the tool rejects it cleanly."""
    mock_mass.music.get_item_by_uri.return_value = MagicMock(spec=BrowseFolder)
    async with Client(mounted_playback) as client:
        with pytest.raises(ToolError, match="browse folder"):
            await client.call_tool(
                "playback_play_media",
                {"queue_id": "player1", "uri": "library://browse/x", "radio": True},
            )
    mock_mass.player_queues.play_media.assert_not_awaited()


async def test_play_media_radio_resolution_failure_raises_tool_error(
    mock_mass: Any, mounted_playback: FastMCP
) -> None:
    """A failed seed lookup surfaces as a clean ToolError, not a raw MA error."""
    mock_mass.music.get_item_by_uri.side_effect = MediaNotFoundError("nope")
    async with Client(mounted_playback) as client:
        with pytest.raises(ToolError, match="Could not resolve URI for radio"):
            await client.call_tool(
                "playback_play_media",
                {"queue_id": "player1", "uri": "spotify://track/missing", "radio": True},
            )
    mock_mass.player_queues.play_media.assert_not_awaited()
