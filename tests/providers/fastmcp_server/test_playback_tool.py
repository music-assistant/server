"""Tests for the ``playback`` sub-server tools."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastmcp import Client, FastMCP

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
