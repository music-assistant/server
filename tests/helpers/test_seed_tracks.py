"""Tests for the seed_tracks dynamic-generation helper."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ArtistType, MediaType
from music_assistant_models.media_items import Track

from music_assistant.helpers.seed_tracks import seed_tracks

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


def _item(media_type: MediaType, **attrs: object) -> MagicMock:
    item = MagicMock()
    item.media_type = media_type
    item.item_id = "1"
    item.provider = "test"
    for key, value in attrs.items():
        setattr(item, key, value)
    return item


@pytest.mark.asyncio
async def test_track_seeds_itself() -> None:
    """A track is its own only seed."""
    item = _item(MediaType.TRACK)
    assert await seed_tracks(cast("MusicAssistant", MagicMock()), item) == [item]


@pytest.mark.asyncio
async def test_album_returns_its_tracks() -> None:
    """An album seeds from its own tracks (including non-library)."""
    mass = MagicMock()
    tracks = [MagicMock(spec=Track), MagicMock(spec=Track)]
    mass.music.albums.tracks = AsyncMock(return_value=tracks)
    item = _item(MediaType.ALBUM)
    assert await seed_tracks(cast("MusicAssistant", mass), item) == tracks
    mass.music.albums.tracks.assert_awaited_once_with("1", "test", in_library_only=False)


@pytest.mark.asyncio
async def test_singer_artist_prefers_top_tracks() -> None:
    """A singer artist seeds from its top tracks, not its full track list."""
    mass = MagicMock()
    top = [MagicMock(spec=Track)]
    mass.music.artists.top_tracks = AsyncMock(return_value=top)
    mass.music.artists.tracks = AsyncMock(return_value=[])
    item = _item(MediaType.ARTIST, artist_type=ArtistType.SINGER)
    assert await seed_tracks(cast("MusicAssistant", mass), item) == top
    mass.music.artists.tracks.assert_not_awaited()


@pytest.mark.asyncio
async def test_singer_artist_falls_back_to_all_tracks() -> None:
    """When an artist has no top tracks, fall back to all its tracks."""
    mass = MagicMock()
    all_tracks = [MagicMock(spec=Track)]
    mass.music.artists.top_tracks = AsyncMock(return_value=[])
    mass.music.artists.tracks = AsyncMock(return_value=all_tracks)
    item = _item(MediaType.ARTIST, artist_type=ArtistType.SINGER)
    assert await seed_tracks(cast("MusicAssistant", mass), item) == all_tracks


@pytest.mark.asyncio
async def test_non_singer_artist_yields_nothing() -> None:
    """An author/narrator artist cannot seed a dynamic playlist."""
    item = _item(MediaType.ARTIST, artist_type=ArtistType.AUTHOR)
    assert await seed_tracks(cast("MusicAssistant", MagicMock()), item) == []


@pytest.mark.asyncio
async def test_playlist_filters_to_available_tracks() -> None:
    """A playlist seeds only from its available tracks (other item types dropped)."""
    mass = MagicMock()
    available = MagicMock(spec=Track)
    available.available = True
    unavailable = MagicMock(spec=Track)
    unavailable.available = False
    not_a_track = MagicMock()  # no Track spec -> isinstance check drops it

    async def _tracks(*_args: object, **_kwargs: object) -> AsyncIterator[MagicMock]:
        for entry in (available, unavailable, not_a_track):
            yield entry

    mass.music.playlists.tracks = _tracks
    item = _item(MediaType.PLAYLIST)
    assert await seed_tracks(cast("MusicAssistant", mass), item) == [available]


@pytest.mark.asyncio
async def test_unsupported_type_yields_nothing() -> None:
    """A radio station (or audiobook/podcast) has no seed tracks."""
    item = _item(MediaType.RADIO)
    assert await seed_tracks(cast("MusicAssistant", MagicMock()), item) == []
