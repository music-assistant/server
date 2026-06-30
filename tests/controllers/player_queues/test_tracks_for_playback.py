"""Tests for the queue controller's get_tracks_for_playback umbrella dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Track

from music_assistant.controllers.player_queues import PlayerQueuesController

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType


def _trk(item_id: str) -> Track:
    return Track(item_id=item_id, provider="library", name=item_id, provider_mappings=set())


def _controller() -> PlayerQueuesController:
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.get_artist_tracks = AsyncMock(return_value=[_trk("artist")])  # type: ignore[method-assign]
    ctrl.get_album_tracks = AsyncMock(return_value=[_trk("album")])  # type: ignore[method-assign]
    ctrl.get_genre_tracks = AsyncMock(return_value=[_trk("genre")])  # type: ignore[method-assign]
    ctrl.get_playlist_tracks = AsyncMock(return_value=[])  # type: ignore[method-assign]
    return ctrl


def _item(media_type: MediaType) -> MediaItemType:
    item = MagicMock()
    item.media_type = media_type
    return cast("MediaItemType", item)


@pytest.mark.asyncio
async def test_track_resolves_to_itself() -> None:
    """A track resolves to itself without touching the per-type resolvers."""
    ctrl = _controller()
    track = _trk("solo")
    assert await ctrl.get_tracks_for_playback(track) == [track]


@pytest.mark.asyncio
async def test_album_routes_to_get_album_tracks() -> None:
    """An album is resolved through the config-driven get_album_tracks."""
    ctrl = _controller()
    result = await ctrl.get_tracks_for_playback(_item(MediaType.ALBUM))
    assert [t.item_id for t in result] == ["album"]


@pytest.mark.asyncio
async def test_artist_routes_to_get_artist_tracks() -> None:
    """An artist is resolved through the config-driven get_artist_tracks."""
    ctrl = _controller()
    result = await ctrl.get_tracks_for_playback(_item(MediaType.ARTIST))
    assert [t.item_id for t in result] == ["artist"]


@pytest.mark.asyncio
async def test_genre_routes_to_get_genre_tracks() -> None:
    """A genre is resolved through get_genre_tracks."""
    ctrl = _controller()
    result = await ctrl.get_tracks_for_playback(_item(MediaType.GENRE))
    assert [t.item_id for t in result] == ["genre"]


@pytest.mark.asyncio
async def test_playlist_filters_to_tracks() -> None:
    """A playlist resolves to its tracks, dropping any non-track playable items."""
    ctrl = _controller()
    track = _trk("p1")
    ctrl.get_playlist_tracks = AsyncMock(return_value=[track, MagicMock()])  # type: ignore[method-assign]
    assert await ctrl.get_tracks_for_playback(_item(MediaType.PLAYLIST)) == [track]


@pytest.mark.asyncio
async def test_unsupported_type_is_empty() -> None:
    """A type with no playable-track resolution yields an empty list."""
    ctrl = _controller()
    assert await ctrl.get_tracks_for_playback(_item(MediaType.AUDIOBOOK)) == []
