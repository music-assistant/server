"""Tests for library_get_album_tracks."""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc, union-attr"

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from music_assistant_models.enums import MediaType

from music_assistant.providers.fastmcp_server.tools._common import (
    album_tracks_from_uri,
    artist_albums_from_uri,
)
from music_assistant.providers.fastmcp_server.tools.library import build_library_server


@pytest.fixture
def library_server(mock_mass: Any) -> FastMCP:
    """Mount only the library sub-server."""
    mcp: FastMCP = FastMCP(name="t")
    mcp.mount(build_library_server(mock_mass), namespace="library")
    return mcp


def _fake_album(**kwargs: Any) -> MagicMock:
    album = MagicMock(
        spec_set=[
            "media_type",
            "uri",
            "name",
            "artist",
            "artists",
            "year",
            "item_id",
            "provider",
            "available",
        ]
    )
    album.media_type = MediaType.ALBUM
    album.uri = kwargs.get("uri", "library://album/7")
    album.name = kwargs.get("name", "Black Sands")
    album.artist = kwargs.get("artist", "Bonobo")
    album.artists = kwargs.get("artists", [])
    album.year = kwargs.get("year", 2010)
    album.item_id = kwargs.get("item_id", "7")
    album.provider = kwargs.get("provider", "library")
    album.available = kwargs.get("available", True)
    return album


def _fake_track(**kwargs: Any) -> MagicMock:
    track = MagicMock(
        spec_set=[
            "uri",
            "name",
            "artists",
            "album",
            "duration",
            "disc_number",
            "track_number",
            "available",
        ]
    )
    track.uri = kwargs.get("uri", "library://track/1")
    track.name = kwargs.get("name", "Kiara")
    track.artists = kwargs.get("artists", [])
    track.album = kwargs.get("album", "Black Sands")
    track.duration = kwargs.get("duration", 230)
    track.disc_number = kwargs.get("disc_number", 1)
    track.track_number = kwargs.get("track_number", 1)
    track.available = kwargs.get("available", True)
    return track


def _fake_artist(**kwargs: Any) -> MagicMock:
    artist = MagicMock(spec_set=["media_type", "uri", "name", "item_id", "provider"])
    artist.media_type = MediaType.ARTIST
    artist.uri = kwargs.get("uri", "library://artist/27")
    artist.name = kwargs.get("name", "deadmau5")
    artist.item_id = kwargs.get("item_id", "27")
    artist.provider = kwargs.get("provider", "library")
    return artist


class TestAlbumTracksFromUri:
    """Unit tests for the shared album-tracks helper."""

    async def test_returns_sorted_available_tracks(self, mock_mass: Any) -> None:
        """Tracks are sorted by disc/track and unavailable entries are dropped."""
        album = _fake_album()
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=album)
        mock_mass.music.albums.tracks = AsyncMock(
            return_value=[
                _fake_track(uri="library://track/2", name="B", disc_number=1, track_number=2),
                _fake_track(uri="library://track/3", name="C", available=False),
                _fake_track(uri="library://track/1", name="A", disc_number=1, track_number=1),
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
        item = MagicMock(spec_set=["media_type"])
        item.media_type = MediaType.TRACK
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=item)
        with pytest.raises(ToolError, match="is not an album"):
            await album_tracks_from_uri(mock_mass, "library://track/1")


class TestGetAlbumTracksTool:
    """Integration tests via the mounted library server."""

    async def test_happy_path(self, library_server: FastMCP, mock_mass: Any) -> None:
        """Tool returns album header and track briefs."""
        album = _fake_album(uri="spotify://album/abc")
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=album)
        mock_mass.music.albums.tracks = AsyncMock(
            return_value=[_fake_track(uri="spotify://track/1", track_number=1)]
        )
        async with Client(library_server) as client:
            result = await client.call_tool(
                "library_get_album_tracks",
                {"album_uri": "spotify://album/abc"},
            )
        text_blocks = [c.text for c in result.content if hasattr(c, "text")]
        assert any("Black Sands" in t for t in text_blocks)
        assert any("spotify://track/1" in t for t in text_blocks)

    async def test_rejects_track_uri(self, library_server: FastMCP, mock_mass: Any) -> None:
        """Passing a track URI surfaces a clear ToolError."""
        item = MagicMock(spec_set=["media_type"])
        item.media_type = MediaType.TRACK
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=item)
        async with Client(library_server) as client:
            with pytest.raises(ToolError, match="is not an album"):
                await client.call_tool(
                    "library_get_album_tracks",
                    {"album_uri": "library://track/99"},
                )


class TestArtistAlbumsFromUri:
    """Unit tests for the shared artist-albums helper."""

    async def test_returns_albums_newest_first(self, mock_mass: Any) -> None:
        """Albums are sorted by year descending and unavailable entries are dropped."""
        artist = _fake_artist()
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=artist)
        mock_mass.music.artists.albums = AsyncMock(
            return_value=[
                _fake_album(uri="library://album/1", name="Old", year=2010),
                _fake_album(uri="library://album/2", name="New", year=2020),
                _fake_album(uri="library://album/3", name="Hidden", year=2015, available=False),
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
        item = MagicMock(spec_set=["media_type"])
        item.media_type = MediaType.ALBUM
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=item)
        with pytest.raises(ToolError, match="is not an artist"):
            await artist_albums_from_uri(mock_mass, "library://album/1")


class TestGetArtistAlbumsTool:
    """Integration tests via the mounted library server."""

    async def test_happy_path(self, library_server: FastMCP, mock_mass: Any) -> None:
        """Tool returns artist header and album briefs."""
        artist = _fake_artist(uri="spotify://artist/abc")
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=artist)
        mock_mass.music.artists.albums = AsyncMock(
            return_value=[_fake_album(uri="spotify://album/1", name="Album One", year=2012)]
        )
        async with Client(library_server) as client:
            result = await client.call_tool(
                "library_get_artist_albums",
                {"artist_uri": "spotify://artist/abc"},
            )
        text_blocks = [c.text for c in result.content if hasattr(c, "text")]
        assert any("deadmau5" in t for t in text_blocks)
        assert any("spotify://album/1" in t for t in text_blocks)

    async def test_rejects_album_uri(self, library_server: FastMCP, mock_mass: Any) -> None:
        """Passing an album URI surfaces a clear ToolError."""
        item = MagicMock(spec_set=["media_type"])
        item.media_type = MediaType.ALBUM
        mock_mass.music.get_item_by_uri = AsyncMock(return_value=item)
        async with Client(library_server) as client:
            with pytest.raises(ToolError, match="is not an artist"):
                await client.call_tool(
                    "library_get_artist_albums",
                    {"artist_uri": "library://album/99"},
                )
