"""Tests for the Plex provider's artist top tracks."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import plexapi.exceptions
import pytest

from music_assistant.providers.plex import PlexProvider
from music_assistant.providers.plex.constants import FAKE_ARTIST_PREFIX, MAX_TOP_TRACKS
from music_assistant.providers.plex.helpers import SUPPORTED_FEATURES

ARTIST_ID = "/library/metadata/12377"

# the cache decorator is bypassed so these tests target the lookup itself
get_toptracks: Any = PlexProvider.get_artist_toptracks.__wrapped__  # type: ignore[attr-defined]


class _FakePlexTrack:
    """Minimal PlexTrack stub carrying only what the ranking reads."""

    def __init__(self, key: str, title: str, rating_count: int | None = None) -> None:
        self.key = key
        self.title = title
        self.ratingCount = rating_count


class _FakePlexArtist:
    """
    Minimal PlexArtist stub.

    Deliberately has no station() attribute: a Plex artist station is a radio playlist,
    which plexapi never enumerates, so touching it here fails the test loudly.
    """

    def __init__(
        self,
        popular_tracks: list[_FakePlexTrack] | None = None,
        own_tracks: list[_FakePlexTrack] | None = None,
    ) -> None:
        self._popular_tracks = popular_tracks
        self._own_tracks = own_tracks or []

    def popularTracks(self) -> list[_FakePlexTrack]:  # noqa: N802
        """Return the canned popular tracks, or fail like a server without filter metadata."""
        if self._popular_tracks is None:
            raise plexapi.exceptions.NotFound('Unknown libtype "artist"')
        return self._popular_tracks

    def tracks(self) -> list[_FakePlexTrack]:
        """Return the artist's own tracks."""
        return self._own_tracks


def _make_provider(plex_artist: _FakePlexArtist) -> Any:
    """Create a PlexProvider that resolves any artist id to the given stub."""
    mock_config = MagicMock()
    mock_config.instance_id = "plex_instance_1"
    mock_config.get_value = lambda key: "INFO" if key == "log_level" else None
    mock_manifest = MagicMock()
    mock_manifest.domain = "plex"

    provider = PlexProvider(MagicMock(), mock_manifest, mock_config, SUPPORTED_FEATURES)
    provider._get_data = AsyncMock(return_value=plex_artist)  # type: ignore[method-assign]
    provider._run_async = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda call, *args, **kwargs: call(*args, **kwargs)
    )
    # parsing is covered elsewhere; return the key so ordering is easy to assert
    provider._parse_track = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda plex_track: plex_track.key
    )
    return provider


@pytest.mark.asyncio
async def test_top_tracks_come_from_popular_tracks() -> None:
    """Top tracks are Plex's popular tracks, in the order Plex ranked them."""
    artist = _FakePlexArtist(
        popular_tracks=[
            _FakePlexTrack("/1", "First"),
            _FakePlexTrack("/2", "Second"),
            _FakePlexTrack("/3", "Third"),
        ]
    )
    result = await get_toptracks(_make_provider(artist), ARTIST_ID)
    assert result == ["/1", "/2", "/3"]


@pytest.mark.asyncio
async def test_top_tracks_are_capped() -> None:
    """No more than MAX_TOP_TRACKS are returned."""
    artist = _FakePlexArtist(
        popular_tracks=[_FakePlexTrack(f"/{i}", f"Track {i}") for i in range(MAX_TOP_TRACKS + 5)]
    )
    result = await get_toptracks(_make_provider(artist), ARTIST_ID)
    assert len(result) == MAX_TOP_TRACKS


@pytest.mark.asyncio
async def test_fallback_ranks_own_tracks_when_filters_unavailable() -> None:
    """
    Servers without filter metadata fall back to ranking the artist's own tracks.

    The highest ranked version of each title wins, and unranked tracks are dropped.
    """
    artist = _FakePlexArtist(
        popular_tracks=None,
        own_tracks=[
            _FakePlexTrack("/1", "Hit", 500),
            _FakePlexTrack("/2", "hit", 900),
            _FakePlexTrack("/3", "Deep Cut", 10),
            _FakePlexTrack("/4", "No Data", None),
            _FakePlexTrack("/5", "Never Scrobbled", 0),
        ],
    )
    result = await get_toptracks(_make_provider(artist), ARTIST_ID)
    assert result == ["/2", "/3"]


@pytest.mark.asyncio
async def test_fake_artist_returns_empty() -> None:
    """A placeholder artist is never looked up on the server."""
    provider = _make_provider(_FakePlexArtist())
    result = await get_toptracks(provider, f"{FAKE_ARTIST_PREFIX}Some Artist")
    assert result == []
    provider._get_data.assert_not_called()
