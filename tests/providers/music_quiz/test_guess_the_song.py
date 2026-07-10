"""Tests for the guess-the-song quiz type distractor sourcing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.music_quiz.models import MusicQuizConfig
from music_assistant.providers.music_quiz.quiz_types.guess_the_song import GuessTheSongQuizType
from music_assistant.providers.music_quiz.suggestions import SuggestionCandidate

CORRECT_LABEL = "Daft Punk - Around the World"


def _track(item_id: str, name: str, artist: str, provider: str = "prov") -> Track:
    """Return a minimal Track with a single artist mapping."""
    return Track(
        item_id=item_id,
        provider=provider,
        name=name,
        artists=UniqueList(
            [
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=f"a_{item_id}",
                    provider=provider,
                    name=artist,
                )
            ]
        ),
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain=provider, provider_instance=provider)
        },
    )


def _artist(item_id: str, name: str, provider: str = "prov") -> ItemMapping:
    """Return a minimal artist mapping."""
    return ItemMapping(media_type=MediaType.ARTIST, item_id=item_id, provider=provider, name=name)


def _pool(tracks: list[Track]) -> dict[str, Track]:
    """Return a source-track pool keyed by URI."""
    pool: dict[str, Track] = {}
    for track in tracks:
        assert track.uri is not None
        pool[track.uri] = track
    return pool


def _correct() -> tuple[Track, SuggestionCandidate]:
    """Return the correct source track and its answer candidate."""
    track = _track("c1", "Around the World", "Daft Punk")
    return track, SuggestionCandidate(CORRECT_LABEL, track.uri, title="Around the World")


def _quiz_type(
    difficulty: str = "normal",
    use_ai: bool = False,
    suggestion_count: int = 4,
) -> tuple[GuessTheSongQuizType, MagicMock]:
    """Return a quiz type with a mock MusicAssistant and empty music lookups."""
    mass = MagicMock()
    mass.music.search = AsyncMock(return_value=SimpleNamespace(tracks=[]))
    mass.music.tracks.similar_tracks = AsyncMock(return_value=[])
    mass.music.artists.similar_artists = AsyncMock(return_value=[])
    mass.music.artists.top_tracks = AsyncMock(return_value=[])
    mass.get_providers_supporting_feature = MagicMock(return_value=[])
    config = MusicQuizConfig(
        suggestion_count=suggestion_count,
        source_uris=["prov://playlist/1"],
        difficulty=difficulty,
        use_ai_distractors=use_ai,
    )
    return GuessTheSongQuizType(mass, config), mass


def _ai_provider(response: str | None = None, error: Exception | None = None) -> MagicMock:
    """Return a mock AI_QUERY-capable plugin provider."""
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = "ai--1"
    provider.ai_query = AsyncMock(return_value=response, side_effect=error)
    return provider


@pytest.mark.asyncio
async def test_normal_difficulty_uses_search_only() -> None:
    """Normal difficulty draws distractors from a catalog search and nothing else."""
    quiz_type, mass = _quiz_type("normal")
    mass.music.search.return_value = SimpleNamespace(
        tracks=[_track("s1", "One More Time", "Daft Punk"), _track("s2", "Genesis", "Justice")]
    )
    correct_track, correct = _correct()

    result = await quiz_type._gather_distractors(correct_track, correct)

    assert [item.label for item in result] == ["Daft Punk - One More Time", "Justice - Genesis"]
    mass.music.tracks.similar_tracks.assert_not_awaited()
    mass.get_providers_supporting_feature.assert_not_called()


@pytest.mark.asyncio
async def test_hard_difficulty_prefers_similar_tracks() -> None:
    """Hard difficulty offers similar tracks first, with the search kept as a fallback tail."""
    quiz_type, mass = _quiz_type("hard")
    mass.music.tracks.similar_tracks.return_value = [
        _track("st1", "Digital Love", "Daft Punk"),
        _track("st2", "D.A.N.C.E.", "Justice"),
        _track("st3", "Sexy Boy", "Air"),
    ]
    mass.music.search.return_value = SimpleNamespace(tracks=[_track("s1", "Fallback", "Someone")])
    correct_track, correct = _correct()

    result = await quiz_type._gather_distractors(correct_track, correct)

    labels = [item.label for item in result]
    assert labels[:3] == [
        "Daft Punk - Digital Love",
        "Justice - D.A.N.C.E.",
        "Air - Sexy Boy",
    ]
    assert "Someone - Fallback" in labels
    mass.music.tracks.similar_tracks.assert_awaited_once()


@pytest.mark.asyncio
async def test_hard_difficulty_enriches_with_similar_artists_when_tracks_sparse() -> None:
    """When similar tracks are sparse, top tracks of similar artists are added."""
    quiz_type, mass = _quiz_type("hard")
    mass.music.artists.similar_artists.return_value = [
        _artist("a2", "Justice"),
        _artist("a3", "Air"),
    ]

    async def _top_tracks(item_id: str, **_kwargs: str) -> list[Track]:
        return {
            "a2": [_track("j1", "Genesis", "Justice")],
            "a3": [_track("air1", "Sexy Boy", "Air")],
        }.get(item_id, [])

    mass.music.artists.top_tracks = AsyncMock(side_effect=_top_tracks)
    correct_track, correct = _correct()

    result = await quiz_type._gather_distractors(correct_track, correct)

    labels = {item.label for item in result}
    assert "Justice - Genesis" in labels
    assert "Air - Sexy Boy" in labels
    mass.music.artists.similar_artists.assert_awaited_once()


@pytest.mark.asyncio
async def test_hard_difficulty_falls_back_to_search_on_error() -> None:
    """A failing similar-tracks/artists lookup falls through to the search distractors."""
    quiz_type, mass = _quiz_type("hard")
    mass.music.tracks.similar_tracks.side_effect = Exception("boom")
    mass.music.artists.similar_artists.side_effect = Exception("boom")
    mass.music.search.return_value = SimpleNamespace(
        tracks=[
            _track("s1", "One More Time", "Daft Punk"),
            _track("s2", "Genesis", "Justice"),
            _track("s3", "Sexy Boy", "Air"),
        ]
    )
    correct_track, correct = _correct()

    result = await quiz_type._gather_distractors(correct_track, correct)

    assert "Daft Punk - One More Time" in {item.label for item in result}


@pytest.mark.asyncio
async def test_easy_difficulty_uses_source_pool() -> None:
    """Easy difficulty offers other tracks from the configured source pool."""
    quiz_type, mass = _quiz_type("easy")
    correct_track, correct = _correct()
    quiz_type._source_track_pool = _pool(
        [
            correct_track,
            _track("p2", "Genesis", "Justice"),
            _track("p3", "Sexy Boy", "Air"),
            _track("p4", "1901", "Phoenix"),
        ]
    )

    result = await quiz_type._gather_distractors(correct_track, correct)

    labels = {item.label for item in result}
    assert {"Justice - Genesis", "Air - Sexy Boy", "Phoenix - 1901"} <= labels
    assert correct_track.uri not in {item.uri for item in result}
    mass.music.tracks.similar_tracks.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_distractors_used_when_enabled() -> None:
    """AI-generated distractors are used first when the toggle is on and a provider responds."""
    quiz_type, mass = _quiz_type("normal", use_ai=True)
    mass.get_providers_supporting_feature.return_value = [
        _ai_provider("Justice - Genesis\nAir - Sexy Boy\nPhoenix - 1901")
    ]
    mass.music.search.return_value = SimpleNamespace(tracks=[_track("s1", "Fallback", "Someone")])
    correct_track, correct = _correct()

    result = await quiz_type._gather_distractors(correct_track, correct)

    assert [item.label for item in result][:3] == [
        "Justice - Genesis",
        "Air - Sexy Boy",
        "Phoenix - 1901",
    ]


@pytest.mark.asyncio
async def test_ai_falls_back_when_no_provider_available() -> None:
    """With no AI provider, distractors come from the difficulty/search source."""
    quiz_type, mass = _quiz_type("normal", use_ai=True)
    mass.get_providers_supporting_feature.return_value = []
    mass.music.search.return_value = SimpleNamespace(
        tracks=[_track("s1", "One More Time", "Daft Punk")]
    )
    correct_track, correct = _correct()

    result = await quiz_type._gather_distractors(correct_track, correct)

    assert [item.label for item in result] == ["Daft Punk - One More Time"]


@pytest.mark.asyncio
async def test_ai_falls_back_when_query_errors() -> None:
    """A failing AI query falls back to the difficulty/search source."""
    quiz_type, mass = _quiz_type("normal", use_ai=True)
    mass.get_providers_supporting_feature.return_value = [_ai_provider(error=Exception("boom"))]
    mass.music.search.return_value = SimpleNamespace(
        tracks=[_track("s1", "One More Time", "Daft Punk")]
    )
    correct_track, correct = _correct()

    result = await quiz_type._gather_distractors(correct_track, correct)

    assert [item.label for item in result] == ["Daft Punk - One More Time"]


@pytest.mark.asyncio
async def test_ai_falls_back_when_output_unusable() -> None:
    """Unusable AI output (no parseable answers) falls back to the difficulty/search source."""
    quiz_type, mass = _quiz_type("normal", use_ai=True)
    mass.get_providers_supporting_feature.return_value = [
        _ai_provider("Sorry, I cannot help with that.")
    ]
    mass.music.search.return_value = SimpleNamespace(
        tracks=[_track("s1", "One More Time", "Daft Punk")]
    )
    correct_track, correct = _correct()

    result = await quiz_type._gather_distractors(correct_track, correct)

    assert [item.label for item in result] == ["Daft Punk - One More Time"]


@pytest.mark.asyncio
async def test_prepare_round_builds_suggestions_from_similar_tracks() -> None:
    """A hard-mode round is assembled with exactly one correct answer from similar tracks."""
    quiz_type, mass = _quiz_type("hard")
    correct_track, _ = _correct()
    quiz_type._source_track_pool = _pool([correct_track])
    mass.music.tracks.similar_tracks.return_value = [
        _track("st1", "Digital Love", "Daft Punk"),
        _track("st2", "Genesis", "Justice"),
        _track("st3", "Sexy Boy", "Air"),
    ]
    mass.metadata.get_image_url_for_item = AsyncMock(return_value="http://img/1")

    game_round = await quiz_type.prepare_round(0, [])

    assert game_round.track_uri == correct_track.uri
    assert len(game_round.suggestions) == 4
    assert sum(item.is_correct for item in game_round.suggestions) == 1
    assert [item.label for item in game_round.suggestions if item.is_correct] == [CORRECT_LABEL]
