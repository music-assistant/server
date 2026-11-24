"""Tests for Open Subsonic similar tracks, with and without the sonicSimilarity extension."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientResponseError
from libopensonic.errors import DataNotFoundError, SonicError

from music_assistant.providers.opensubsonic.sonic_provider import OpenSonicProvider

SEED_ID = "seed"
get_similar_tracks: Any = OpenSonicProvider.get_similar_tracks.__wrapped__  # type: ignore[attr-defined]


def _song(song_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=song_id)


def _match(song_id: str, similarity: float) -> SimpleNamespace:
    return SimpleNamespace(entry=_song(song_id), similarity=similarity)


def _provider(sonic_similarity: bool) -> Any:
    provider: Any = OpenSonicProvider.__new__(OpenSonicProvider)
    provider._sonic_similarity = sonic_similarity
    provider.logger = logging.getLogger(__name__)
    provider.conn = MagicMock()
    provider.conn.get_sonic_similar_tracks = AsyncMock()
    provider.conn.get_similar_songs = AsyncMock(return_value=[_song("meta1"), _song("meta2")])

    async def _parse(songs: list[SimpleNamespace]) -> list[str]:
        return [song.id for song in songs]

    provider._parse_similar_songs = _parse
    return provider


async def test_sonic_matches_are_used_without_seed_track() -> None:
    """Sonic matches are returned in order, and the seed track is left out."""
    provider = _provider(sonic_similarity=True)
    provider.conn.get_sonic_similar_tracks.return_value = [
        _match(SEED_ID, 1.0),
        _match("sonic1", 0.9),
        _match("sonic2", 0.8),
    ]

    result = await get_similar_tracks(provider, SEED_ID, 10)

    assert result == ["sonic1", "sonic2"]
    provider.conn.get_sonic_similar_tracks.assert_awaited_once_with(song_id=SEED_ID, count=10)
    provider.conn.get_similar_songs.assert_not_awaited()


async def test_falls_back_when_extension_not_advertised() -> None:
    """Servers without sonicSimilarity only get the metadata based lookup."""
    provider = _provider(sonic_similarity=False)

    result = await get_similar_tracks(provider, SEED_ID, 10)

    assert result == ["meta1", "meta2"]
    provider.conn.get_sonic_similar_tracks.assert_not_awaited()
    provider.conn.get_similar_songs.assert_awaited_once_with(iid=SEED_ID, count=10)


@pytest.mark.parametrize(
    "error",
    [
        SonicError("analysis backend unavailable"),
        ClientResponseError(MagicMock(), (), status=500),
        TimeoutError(),
    ],
)
async def test_falls_back_when_sonic_call_fails(error: Exception) -> None:
    """A failing sonic lookup falls back to getSimilarSongs instead of raising."""
    provider = _provider(sonic_similarity=True)
    provider.conn.get_sonic_similar_tracks.side_effect = error

    result = await get_similar_tracks(provider, SEED_ID, 10)

    assert result == ["meta1", "meta2"]
    provider.conn.get_similar_songs.assert_awaited_once_with(iid=SEED_ID, count=10)


async def test_falls_back_when_sonic_returns_no_matches() -> None:
    """An empty sonic result falls back to getSimilarSongs."""
    provider = _provider(sonic_similarity=True)
    provider.conn.get_sonic_similar_tracks.return_value = []

    result = await get_similar_tracks(provider, SEED_ID, 10)

    assert result == ["meta1", "meta2"]
    provider.conn.get_similar_songs.assert_awaited_once_with(iid=SEED_ID, count=10)


async def test_metadata_lookup_without_results_returns_empty_list() -> None:
    """An empty list is returned when getSimilarSongs finds nothing."""
    provider = _provider(sonic_similarity=False)
    provider.conn.get_similar_songs.side_effect = DataNotFoundError("nothing similar")

    assert await get_similar_tracks(provider, SEED_ID, 10) == []
