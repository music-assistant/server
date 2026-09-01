"""Tests for the media resolver's get_tracks_for_playback umbrella dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Audiobook, MediaCollection, Track, UniqueList

from music_assistant.controllers.player_queues.media_resolver import MediaResolver

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType


def _trk(item_id: str) -> Track:
    return Track(item_id=item_id, provider="library", name=item_id, provider_mappings=set())


def _resolver() -> MediaResolver:
    resolver = MediaResolver.__new__(MediaResolver)
    resolver.get_artist_tracks = AsyncMock(return_value=[_trk("artist")])  # type: ignore[method-assign]
    resolver.get_album_tracks = AsyncMock(return_value=[_trk("album")])  # type: ignore[method-assign]
    resolver.get_genre_tracks = AsyncMock(return_value=[_trk("genre")])  # type: ignore[method-assign]
    resolver.get_playlist_tracks = AsyncMock(return_value=[])  # type: ignore[method-assign]
    return resolver


def _item(media_type: MediaType) -> MediaItemType:
    item = MagicMock()
    item.media_type = media_type
    return cast("MediaItemType", item)


@pytest.mark.asyncio
async def test_track_resolves_to_itself() -> None:
    """A track resolves to itself without touching the per-type resolvers."""
    resolver = _resolver()
    track = _trk("solo")
    assert await resolver.get_tracks_for_playback(track) == [track]


@pytest.mark.asyncio
async def test_album_routes_to_get_album_tracks() -> None:
    """An album is resolved through the config-driven get_album_tracks."""
    resolver = _resolver()
    result = await resolver.get_tracks_for_playback(_item(MediaType.ALBUM))
    assert [t.item_id for t in result] == ["album"]


@pytest.mark.asyncio
async def test_artist_routes_to_get_artist_tracks() -> None:
    """An artist is resolved through the config-driven get_artist_tracks."""
    resolver = _resolver()
    result = await resolver.get_tracks_for_playback(_item(MediaType.ARTIST))
    assert [t.item_id for t in result] == ["artist"]


@pytest.mark.asyncio
async def test_genre_routes_to_get_genre_tracks() -> None:
    """A genre is resolved through get_genre_tracks."""
    resolver = _resolver()
    result = await resolver.get_tracks_for_playback(_item(MediaType.GENRE))
    assert [t.item_id for t in result] == ["genre"]


@pytest.mark.asyncio
async def test_genre_samples_artists_via_top_tracks() -> None:
    """Genre resolution samples artists' top tracks, never the full artist-tracks resolution."""
    resolver = MediaResolver.__new__(MediaResolver)
    resolver.mass = MagicMock()
    resolver.logger = MagicMock()
    resolver.get_artist_tracks = AsyncMock()  # type: ignore[method-assign]
    artist = MagicMock()
    artist.item_id = "a1"
    artist.provider = "test"
    resolver.mass.music.genres.mapped_media = AsyncMock(return_value=([], [], [artist]))
    resolver.mass.music.artists.top_tracks = AsyncMock(return_value=[_trk("top1"), _trk("top2")])
    genre = MagicMock()
    genre.name = "Rock"

    result = await resolver.get_genre_tracks(genre, None)

    resolver.mass.music.artists.top_tracks.assert_awaited_once_with("a1", "test")
    resolver.get_artist_tracks.assert_not_awaited()
    assert {t.item_id for t in result} == {"top1", "top2"}


@pytest.mark.asyncio
async def test_playlist_filters_to_tracks() -> None:
    """A playlist resolves to its tracks, dropping any non-track playable items."""
    resolver = _resolver()
    track = _trk("p1")
    resolver.get_playlist_tracks = AsyncMock(return_value=[track, MagicMock()])  # type: ignore[method-assign]
    assert await resolver.get_tracks_for_playback(_item(MediaType.PLAYLIST)) == [track]


@pytest.mark.asyncio
async def test_unsupported_type_is_empty() -> None:
    """A type with no playable-track resolution yields an empty list."""
    resolver = _resolver()
    assert await resolver.get_tracks_for_playback(_item(MediaType.AUDIOBOOK)) == []


@pytest.mark.asyncio
async def test_audiobook_collection_resolves_existing_items() -> None:
    """An audiobook collection resolves its model items without deserializing them again."""
    resolver = _resolver()
    resolver.mass = MagicMock()
    resolver.mass.music.get_resume_position = AsyncMock(side_effect=[(True, 0), (False, 12_000)])
    first = Audiobook(
        item_id="book-1",
        provider="library",
        name="Book 1",
        provider_mappings=set(),
    )
    second = Audiobook(
        item_id="book-2",
        provider="library",
        name="Book 2",
        provider_mappings=set(),
    )
    collection = MediaCollection[Audiobook](
        item_id="audiobook___Series",
        provider="library",
        name="Series",
        provider_mappings=set(),
        items=UniqueList([first, second]),
    )

    result = await resolver._resolve_media_items(collection, userid="user")

    assert result == [second]
    assert result[0] is second
    assert second.resume_position_ms == 12_000
