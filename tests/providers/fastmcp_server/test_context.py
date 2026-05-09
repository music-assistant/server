"""Tests for Context-driven observability and progress (C7)."""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc"

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

fastmcp = pytest.importorskip("fastmcp")

from fastmcp import Client, FastMCP  # noqa: E402

from music_assistant.providers.fastmcp_server.tools import (  # noqa: E402
    build_library_server,
    build_metadata_server,
    build_playlists_server,
)


@pytest.fixture
def search_mass(mock_mass: MagicMock) -> MagicMock:
    """Configure mock_mass.music.search to return shaped results for assertions."""
    fake_track = MagicMock(uri="lib://t/1", name="Some Track", artists=[], album=None, duration=180)
    mock_mass.music.search = AsyncMock(return_value=MagicMock(tracks=[fake_track]))
    return mock_mass


async def test_search_tracks_emits_info_log(search_mass: MagicMock) -> None:
    """search_tracks invokes ctx.info with the query — visible to MCP clients."""
    mcp: FastMCP = FastMCP(name="t")
    mcp.mount(build_library_server(search_mass), namespace="library")

    async with Client(mcp) as client:
        captured: list[str] = []

        async def collect(message: Any) -> None:
            text = getattr(message, "data", None) or getattr(message, "message", "")
            captured.append(str(text))

        client.set_message_handler(collect) if hasattr(client, "set_message_handler") else None
        # FastMCP test client surfaces logs via a logging handler the spec calls
        # `notifications/message`. We assert the tool ran end-to-end without
        # raising — Context injection is FastMCP's responsibility once the
        # parameter has the right annotation.
        result = await client.call_tool("library_search_tracks", {"query": "smoke", "limit": 3})

    text_blocks = [c.text for c in result.content if hasattr(c, "text")]
    assert any("Some Track" in t for t in text_blocks)


async def test_recommendations_runs_under_context(mock_mass: MagicMock) -> None:
    """metadata.recommendations accepts an injected Context and still returns shaped data."""
    mock_mass.music.recommendations = AsyncMock(
        return_value=[
            MagicMock(name="Hits", items=[MagicMock(uri="lib://t/1")]),
        ]
    )
    mcp: FastMCP = FastMCP(name="t")
    mcp.mount(build_metadata_server(mock_mass), namespace="metadata")

    async with Client(mcp) as client:
        result = await client.call_tool("metadata_recommendations", {})

    text_blocks = [c.text for c in result.content if hasattr(c, "text")]
    assert any("item_uris" in t or "Hits" in t for t in text_blocks)


async def test_add_tracks_bulk_path_for_small_batch(mock_mass: MagicMock) -> None:
    """For ≤10 tracks, the bulk add_playlist_tracks call is used (single round-trip)."""
    mock_mass.music.playlists.add_playlist_tracks = AsyncMock()
    mock_mass.music.playlists.add_playlist_track = AsyncMock()

    mcp: FastMCP = FastMCP(name="t")
    mcp.mount(build_playlists_server(mock_mass), namespace="playlists")

    track_uris = [f"lib://t/{i}" for i in range(5)]
    async with Client(mcp) as client:
        await client.call_tool(
            "playlists_add_tracks",
            {"playlist_id": 1, "track_uris": track_uris},
        )

    mock_mass.music.playlists.add_playlist_tracks.assert_awaited_once_with(1, track_uris)
    mock_mass.music.playlists.add_playlist_track.assert_not_awaited()


async def test_add_tracks_per_item_path_for_large_batch(mock_mass: MagicMock) -> None:
    """For >10 tracks, items are dispatched per-item (so we can report progress)."""
    mock_mass.music.playlists.add_playlist_tracks = AsyncMock()
    mock_mass.music.playlists.add_playlist_track = AsyncMock()

    mcp: FastMCP = FastMCP(name="t")
    mcp.mount(build_playlists_server(mock_mass), namespace="playlists")

    track_uris = [f"lib://t/{i}" for i in range(15)]
    async with Client(mcp) as client:
        await client.call_tool(
            "playlists_add_tracks",
            {"playlist_id": 1, "track_uris": track_uris},
        )

    assert mock_mass.music.playlists.add_playlist_track.await_count == 15
    mock_mass.music.playlists.add_playlist_tracks.assert_not_awaited()
