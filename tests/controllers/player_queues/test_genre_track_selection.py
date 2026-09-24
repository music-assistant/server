"""Tests for the player-queue genre track selection (``get_genre_tracks``) with failing sources."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album, Artist, Genre, ProviderMapping, Track

from music_assistant.controllers.player_queues.media_resolver import MediaResolver


def _track(name: str) -> Track:
    # a mapping is needed for the track to count as available
    mapping = ProviderMapping(item_id=name, provider_domain="test", provider_instance="test")
    return Track(item_id=name, provider="test", name=name, provider_mappings={mapping})


def _artist(name: str) -> Artist:
    return Artist(item_id=name, provider="test", name=name, provider_mappings=set())


def _album(name: str) -> Album:
    return Album(item_id=name, provider="test", name=name, provider_mappings=set())


def _genre() -> Genre:
    return Genre(item_id="1", provider="library", name="Jazz", provider_mappings=set())


def _fake_resolver(albums: list[Album], artists: list[Artist] | None = None) -> MagicMock:
    """Create a mock resolver whose genre maps to the given albums and artists only."""
    fake = MagicMock()
    fake.mass.music.genres.mapped_media = AsyncMock(return_value=([], albums, artists or []))
    return fake


async def test_genre_tracks_skip_failing_artist() -> None:
    """One artist whose tracks cannot be fetched is skipped; the other artists still contribute."""
    fake = _fake_resolver([], [_artist("Broken"), _artist("Fine")])

    async def _top_tracks(item_id: str, _provider: str) -> list[Track]:
        if item_id == "Broken":
            raise MediaNotFoundError("provider returned garbage")
        return [_track("Song")]

    fake.mass.music.artists.top_tracks = AsyncMock(side_effect=_top_tracks)

    result = await MediaResolver.get_genre_tracks(cast("MediaResolver", fake), _genre(), None)

    assert [track.name for track in result] == ["Song"]
    fake.logger.warning.assert_called_once()


async def test_genre_tracks_skip_failing_album() -> None:
    """One album whose tracks cannot be fetched is skipped; the other albums still contribute."""
    albums = [_album("Broken"), _album("Fine")]
    fake = _fake_resolver(albums)

    async def _album_tracks(album: Album, _start_item: str | None) -> list[Track]:
        if album.name == "Broken":
            raise MediaNotFoundError("provider returned garbage")
        return [_track("Song")]

    fake.get_album_tracks = AsyncMock(side_effect=_album_tracks)

    result = await MediaResolver.get_genre_tracks(cast("MediaResolver", fake), _genre(), None)

    assert [track.name for track in result] == ["Song"]
    fake.logger.warning.assert_called_once()


async def test_genre_tracks_raise_when_nothing_playable() -> None:
    """With every source failing and nothing to play, the provider error still surfaces."""
    fake = _fake_resolver([_album("Broken")])
    fake.get_album_tracks = AsyncMock(side_effect=MediaNotFoundError("provider returned garbage"))

    with pytest.raises(MediaNotFoundError):
        await MediaResolver.get_genre_tracks(cast("MediaResolver", fake), _genre(), None)
