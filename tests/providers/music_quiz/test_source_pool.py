"""Tests for resolving configured source URIs into the round track pool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Genre, ProviderMapping, Track

from music_assistant.providers.music_quiz.models import MusicQuizConfig
from music_assistant.providers.music_quiz.quiz_types.guess_the_song import GuessTheSongQuizType


def _track(item_id: str) -> Track:
    """Return a minimal track."""
    return Track(
        item_id=item_id,
        provider="prov",
        name=f"Track {item_id}",
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="prov", provider_instance="prov")
        },
    )


def _quiz_type(source_uris: list[str]) -> tuple[GuessTheSongQuizType, MagicMock]:
    """Return a quiz type with a mock MusicAssistant for the given sources."""
    mass = MagicMock()
    config = MusicQuizConfig(source_uris=source_uris)
    return GuessTheSongQuizType(mass, config), mass


async def test_source_pool_resolves_any_media_item_via_playback_resolver() -> None:
    """Any URI that resolves to playable tracks fills the pool (genre, playlist, ...)."""
    quiz_type, mass = _quiz_type(["library://genre/1", "library://playlist/2"])
    genre = Genre(
        item_id="1",
        provider="library",
        name="Rock",
        provider_mappings={
            ProviderMapping(item_id="1", provider_domain="library", provider_instance="library")
        },
    )
    playlist_tracks = [_track("p1"), _track("p2")]
    genre_tracks = [_track("g1"), _track("p1")]  # overlapping track deduped by uri
    mass.music.get_item_by_uri = AsyncMock(side_effect=[genre, MagicMock()])
    mass.player_queues.get_tracks_for_playback = AsyncMock(
        side_effect=[genre_tracks, playlist_tracks]
    )

    pool = await quiz_type._get_source_track_pool()

    assert mass.player_queues.get_tracks_for_playback.await_count == 2
    assert sorted(pool) == sorted({t.uri for t in [*genre_tracks, *playlist_tracks] if t.uri})
    assert genre.media_type == MediaType.GENRE


async def test_source_pool_skips_failing_sources() -> None:
    """One unresolvable source does not abort the pool as long as another works."""
    quiz_type, mass = _quiz_type(["library://genre/broken", "library://track/3"])
    mass.music.get_item_by_uri = AsyncMock(side_effect=[RuntimeError("gone"), _track("3")])
    mass.player_queues.get_tracks_for_playback = AsyncMock(return_value=[_track("3")])

    pool = await quiz_type._get_source_track_pool()

    assert list(pool.values()) == [_track("3")]


async def test_source_pool_raises_when_nothing_resolves() -> None:
    """A pool without any playable tracks raises for a clear early error."""
    quiz_type, mass = _quiz_type(["library://genre/empty"])
    mass.music.get_item_by_uri = AsyncMock(return_value=MagicMock())
    mass.player_queues.get_tracks_for_playback = AsyncMock(return_value=[])

    with pytest.raises(InvalidDataError):
        await quiz_type._get_source_track_pool()
