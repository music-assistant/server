"""Tests for library_get_album_tracks and library_get_artist_albums."""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc, union-attr"

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from music_assistant_models.enums import MediaType

from music_assistant.providers.fastmcp_server.tools._common import (
    album_tracks_from_uri,
    artist_albums_from_uri,
)
from music_assistant.providers.fastmcp_server.tools.library import build_library_server

from .media_fakes import (
    fake_album,
    fake_artist,
    fake_media_item,
    fake_track,
)


@pytest.fixture
def library_server(mock_mass: Any) -> FastMCP:
    """Mount only the library sub-server."""
    mcp: FastMCP = FastMCP(name="t")
    mcp.mount(build_library_server(mock_mass), namespace="library")
    return mcp


class TestAlbumTracksFromUri:
    """Unit tests for the shared album-tracks helper."""

    async def test_returns_sorted_available_tracks(self, mock_mass: Any) -> None:
        """Tracks are sorted by disc/track and unavailable entries are dropped."""
        album = fake_album()
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=album)
        mock_mass.music.albums.tracks = AsyncMock(
            return_value=[
                fake_track(uri="library://track/2", name="B", disc_number=1, track_number=2),
                fake_track(uri="library://track/3", name="C", available=False),
                fake_track(uri="library://track/1", name="A", disc_number=1, track_number=1),
            ]
        )
        result = await album_tracks_from_uri(mock_mass, album.uri)
        assert result.album.name == "Black Sands"
        assert [t.uri for t in result.tracks] == [
            "library://track/1",
            "library://track/2",
        ]
        mock_mass.music.albums.tracks.assert_awaited_once_with("7", "library")

    async def test_rejects_non_album_uri(self, mock_mass: Any) -> None:
        """A track URI raises rather than returning an empty listing."""
        mock_mass.music.get_item_by_uri = AsyncMock(
            return_value=fake_media_item(MediaType.TRACK, uri="library://track/1")
        )
        with pytest.raises(ToolError, match="is not an album"):
            await album_tracks_from_uri(mock_mass, "library://track/1")


class TestGetAlbumTracksTool:
    """Integration tests via the mounted library server."""

    async def test_happy_path(self, library_server: FastMCP, mock_mass: Any) -> None:
        """Tool returns album header and track briefs."""
        album = fake_album(uri="spotify://album/abc")
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=album)
        mock_mass.music.albums.tracks = AsyncMock(
            return_value=[fake_track(uri="spotify://track/1", track_number=1)]
        )
        async with Client(library_server) as client:
            result = await client.call_tool(
                "library_get_album_tracks",
                {"uri": "spotify://album/abc"},
            )
        text_blocks = [c.text for c in result.content if hasattr(c, "text")]
        assert any("Black Sands" in t for t in text_blocks)
        assert any("spotify://track/1" in t for t in text_blocks)


class TestArtistAlbumsFromUri:
    """Unit tests for the shared artist-albums helper."""

    async def test_returns_albums_newest_first(self, mock_mass: Any) -> None:
        """Albums are sorted by year descending and unavailable entries are dropped."""
        artist = fake_artist()
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=artist)
        mock_mass.music.artists.albums = AsyncMock(
            return_value=[
                fake_album(uri="library://album/1", name="Old", year=2010),
                fake_album(uri="library://album/2", name="New", year=2020),
                fake_album(uri="library://album/3", name="Hidden", year=2015, available=False),
            ]
        )

        result = await artist_albums_from_uri(mock_mass, artist.uri)
        assert result.artist.name == "deadmau5"
        assert [a.uri for a in result.albums] == [
            "library://album/2",
            "library://album/1",
        ]
        mock_mass.music.artists.albums.assert_awaited_once_with("27", "library")

    async def test_rejects_non_artist_uri(self, mock_mass: Any) -> None:
        """An album URI raises rather than returning an empty listing."""
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=fake_album())
        with pytest.raises(ToolError, match="is not an artist"):
            await artist_albums_from_uri(mock_mass, "library://album/1")


class TestGetArtistAlbumsTool:
    """Integration tests via the mounted library server."""

    async def test_happy_path(self, library_server: FastMCP, mock_mass: Any) -> None:
        """Tool returns artist header and album briefs."""
        artist = fake_artist(uri="spotify://artist/abc")
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=artist)
        mock_mass.music.artists.albums = AsyncMock(
            return_value=[fake_album(uri="spotify://album/1", name="Album One", year=2012)]
        )
        async with Client(library_server) as client:
            result = await client.call_tool(
                "library_get_artist_albums",
                {"uri": "spotify://artist/abc"},
            )
        text_blocks = [c.text for c in result.content if hasattr(c, "text")]
        assert any("deadmau5" in t for t in text_blocks)
        assert any("spotify://album/1" in t for t in text_blocks)
