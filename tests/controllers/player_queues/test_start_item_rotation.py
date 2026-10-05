"""
Tests for the ``keep_preceding_items`` handling of ``start_item`` in the media resolver.

Playing a playlist/album from a chosen track normally drops everything before that track
("play from here onwards"). With shuffle on that has no meaning, so the preceding tracks are
moved behind the rest instead of being dropped and the chosen track is returned first.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.media_items import Album, Playlist, ProviderMapping, Track

from music_assistant.controllers.player_queues.media_resolver import MediaResolver


def _track(item_id: str) -> Track:
    """Build an available Track on the 'test' provider."""
    return Track(
        item_id=item_id,
        provider="test",
        name=f"Track {item_id}",
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def _resolver() -> tuple[MediaResolver, MagicMock]:
    """Create a bare resolver, plus the mock standing in for the `mass` instance."""
    resolver = MediaResolver.__new__(MediaResolver)
    mass = MagicMock()
    mass.config.get_raw_core_config_value = MagicMock(return_value="all_tracks")
    resolver.mass = mass
    resolver.queues = MagicMock()
    resolver.logger = MagicMock()
    return resolver, mass


def _album_resolver(tracks: list[Track]) -> MediaResolver:
    """Create a resolver whose album track listing returns the given tracks."""
    resolver, mass = _resolver()
    mass.music.albums.tracks = AsyncMock(return_value=tracks)
    return resolver


def _playlist_resolver(tracks: list[Track]) -> MediaResolver:
    """Create a resolver whose playlist track listing yields the given tracks."""
    resolver, mass = _resolver()

    async def _tracks(*_args: Any, **_kwargs: Any) -> AsyncIterator[Track]:
        for track in tracks:
            yield track

    mass.music.playlists.tracks = _tracks
    return resolver


def _album() -> Album:
    """Build an Album on the 'test' provider."""
    return Album(
        item_id="al",
        provider="test",
        name="Album",
        provider_mappings={
            ProviderMapping(item_id="al", provider_domain="test", provider_instance="test")
        },
    )


def _playlist() -> Playlist:
    """Build a non-dynamic Playlist on the 'test' provider."""
    return Playlist(
        item_id="pl",
        provider="test",
        name="Playlist",
        provider_mappings={
            ProviderMapping(item_id="pl", provider_domain="test", provider_instance="test")
        },
    )


async def test_album_start_item_drops_preceding_tracks_by_default() -> None:
    """Without the flag an album starts at the chosen track and the earlier ones are dropped."""
    tracks = [_track(x) for x in ("a", "b", "c", "d")]
    resolver = _album_resolver(tracks)

    result = await resolver.get_album_tracks(_album(), start_item="c")

    assert [track.item_id for track in result] == ["c", "d"]


async def test_album_start_item_keeps_preceding_tracks_behind_the_rest() -> None:
    """With the flag the whole album is returned, rotated so the chosen track comes first."""
    tracks = [_track(x) for x in ("a", "b", "c", "d")]
    resolver = _album_resolver(tracks)

    result = await resolver.get_album_tracks(_album(), start_item="c", keep_preceding_items=True)

    assert [track.item_id for track in result] == ["c", "d", "a", "b"]


async def test_playlist_start_item_drops_preceding_tracks_by_default() -> None:
    """Without the flag a playlist starts at the chosen track and the earlier ones are dropped."""
    tracks = [_track(x) for x in ("a", "b", "c", "d")]
    resolver = _playlist_resolver(tracks)

    result = await resolver.get_playlist_tracks(_playlist(), start_item="c")

    assert [track.item_id for track in result] == ["c", "d"]


async def test_playlist_start_item_keeps_preceding_tracks_behind_the_rest() -> None:
    """With the flag the whole playlist is returned, rotated so the chosen track comes first."""
    tracks = [_track(x) for x in ("a", "b", "c", "d")]
    resolver = _playlist_resolver(tracks)

    result = await resolver.get_playlist_tracks(
        _playlist(), start_item="c", keep_preceding_items=True
    )

    assert [track.item_id for track in result] == ["c", "d", "a", "b"]


async def test_playlist_without_start_item_keeps_full_order() -> None:
    """The flag is a no-op when no start item is given: the playlist keeps its own order."""
    tracks = [_track(x) for x in ("a", "b", "c")]
    resolver = _playlist_resolver(tracks)

    result = await resolver.get_playlist_tracks(
        _playlist(), start_item=None, keep_preceding_items=True
    )

    assert [track.item_id for track in result] == ["a", "b", "c"]


async def test_playlist_unknown_start_item_yields_nothing() -> None:
    """An unmatched start item still yields no tracks, so the caller can fall back."""
    tracks = [_track(x) for x in ("a", "b", "c")]
    resolver = _playlist_resolver(tracks)

    result = await resolver.get_playlist_tracks(
        _playlist(), start_item="zz", keep_preceding_items=True
    )

    assert result == []
