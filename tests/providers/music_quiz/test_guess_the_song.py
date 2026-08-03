"""Tests for the guess-the-song quiz type distractor sourcing."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.music.recency import RecencySnapshot, RecencyWindows
from music_assistant.models.plugin import AIEngine, PluginProvider
from music_assistant.providers.music_quiz.models import (
    MultipleChoiceRoundState,
    MusicQuizConfig,
)
from music_assistant.providers.music_quiz.quiz_types.base import QUIZ_TRACK_RECENCY_SECONDS
from music_assistant.providers.music_quiz.quiz_types.guess_the_song import (
    GuessTheSongQuizType,
    _track_to_candidate,
)
from music_assistant.providers.music_quiz.suggestions import SuggestionCandidate

CORRECT_LABEL = "Daft Punk - Around the World"


def test_config_normalization_preserves_similar_music() -> None:
    """Keep the shared source-expansion setting for Guess the Song."""
    config = MusicQuizConfig(
        source_uris=["prov://playlist/1"],
        include_similar_music=True,
    )

    normalized = GuessTheSongQuizType.normalize_config(config)

    assert normalized.include_similar_music is True


def _track(item_id: str, name: str, artist: str | None, provider: str = "prov") -> Track:
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
            if artist
            else []
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
    return track, _track_to_candidate(track)


def _quiz_type(
    difficulty: str = "normal",
    use_ai: bool = False,
    suggestion_count: int = 4,
) -> tuple[GuessTheSongQuizType, MagicMock]:
    """Return a quiz type with a mock MusicAssistant and empty music lookups."""
    mass = MagicMock()
    mass.music.get_item = AsyncMock()
    mass.music.search = AsyncMock(return_value=SimpleNamespace(tracks=[]))
    mass.music.tracks.get_provider_item = AsyncMock()
    mass.music.tracks.similar_tracks = AsyncMock(return_value=[])
    mass.music.artists.similar_artists = AsyncMock(return_value=[])
    mass.music.artists.top_tracks = AsyncMock(return_value=[])
    mass.music.recency.snapshot = AsyncMock(return_value=RecencySnapshot(now=0))
    mass.metadata.get_image_url_for_item = AsyncMock(return_value=None)
    mass.get_providers_supporting_feature = MagicMock(return_value=[])
    config = MusicQuizConfig(
        suggestion_count=suggestion_count,
        source_uris=["prov://playlist/1"],
        difficulty=difficulty,
        use_ai_distractors=use_ai,
    )
    quiz_type = GuessTheSongQuizType(mass, config)
    quiz_type._source_track_pool = {}
    return quiz_type, mass


def _ai_provider(response: object = None, error: Exception | None = None) -> MagicMock:
    """Return a mock AI_QUERY-capable plugin provider exposing a single engine."""
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = "ai--1"
    provider.ai_query = AsyncMock(return_value=response, side_effect=error)
    provider.get_ai_engines = AsyncMock(
        return_value=[AIEngine(id="engine", name="ai--1", provider=provider)]
    )
    return provider


def _ai_response(
    ranked_ids: list[str],
    synthetic: list[tuple[str, str]],
    **extra: object,
) -> str:
    """Return a structured AI distractor response."""
    return json.dumps(
        {
            "ranked_ids": ranked_ids,
            "synthetic": [{"kind": kind, "label": label} for kind, label in synthetic],
            **extra,
        }
    )


def test_reject_track_removes_it_from_the_source_pool() -> None:
    """Exclude failed playback tracks from later Guess rounds."""
    quiz_type, _ = _quiz_type()
    failed = _track("failed", "Unavailable", "Artist")
    available = _track("available", "Playable", "Artist")
    quiz_type._source_track_pool = _pool([failed, available])
    assert failed.uri is not None

    quiz_type.reject_track(failed.uri)

    assert quiz_type._source_track_pool == _pool([available])


@pytest.mark.asyncio
async def test_initialize_reads_partial_play_recency() -> None:
    """Load interrupted playback history for quiz track selection."""
    quiz_type, mass = _quiz_type()
    snapshot = RecencySnapshot(now=100)
    mass.music.recency.snapshot.return_value = snapshot

    await quiz_type.initialize()

    assert quiz_type._recency_snapshot is snapshot
    mass.music.recency.snapshot.assert_awaited_once_with(
        RecencyWindows(song_seconds=QUIZ_TRACK_RECENCY_SECONDS),
        include_partially_played=True,
    )


@pytest.mark.asyncio
async def test_source_selection_prefers_tracks_not_played_recently() -> None:
    """Choose a fresh source track while retaining recent tracks as fallback candidates."""
    quiz_type, _ = _quiz_type()
    recent = _track("recent", "Teardrop", "Massive Attack")
    fresh = _track("fresh", "Genesis", "Justice")
    quiz_type._source_track_pool = _pool([recent, fresh])
    quiz_type._recency_snapshot = RecencySnapshot(
        now=100,
        song_ts={(recent.provider, recent.item_id): 100},
    )

    with patch(
        "music_assistant.providers.music_quiz.quiz_types.guess_the_song.secrets.choice",
        side_effect=lambda candidates: candidates[0],
    ) as choose:
        selected = await quiz_type._get_next_source_track(set())
        assert quiz_type._recency_snapshot is not None
        quiz_type._recency_snapshot.song_ts[(fresh.provider, fresh.item_id)] = 100
        fallback = await quiz_type._get_next_source_track(set())

    assert selected is fresh
    assert fallback is recent
    assert choose.call_args_list[0].args[0] == [fresh]
    assert choose.call_args_list[1].args[0] == [recent, fresh]


@pytest.mark.asyncio
async def test_normal_difficulty_prefers_search_before_source_pool() -> None:
    """Normal difficulty keeps catalog results ahead of the source-pool fallback."""
    quiz_type, mass = _quiz_type("normal", use_ai=True)
    mass.music.search.return_value = SimpleNamespace(
        tracks=[_track("s1", "One More Time", "Daft Punk"), _track("s2", "Genesis", "Justice")]
    )
    correct_track, correct = _correct()
    quiz_type._source_track_pool = _pool([correct_track, _track("pool", "Lisztomania", "Phoenix")])

    result = list(await quiz_type._gather_distractors(correct_track, correct))

    assert [item.label for item in result] == [
        "Daft Punk - One More Time",
        "Justice - Genesis",
        "Phoenix - Lisztomania",
    ]
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
    pool_fallback = _track("pool", "Lisztomania", "Phoenix")
    quiz_type._source_track_pool = _pool([correct_track, pool_fallback])

    result = list(await quiz_type._gather_distractors(correct_track, correct))

    labels = [item.label for item in result]
    assert labels[:3] == [
        "Daft Punk - Digital Love",
        "Justice - D.A.N.C.E.",
        "Air - Sexy Boy",
    ]
    assert labels[-2:] == ["Someone - Fallback", "Phoenix - Lisztomania"]
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

    result = list(await quiz_type._gather_distractors(correct_track, correct))

    labels = {item.label for item in result}
    assert "Justice - Genesis" in labels
    assert "Air - Sexy Boy" in labels
    mass.music.artists.similar_artists.assert_awaited_once()


@pytest.mark.asyncio
async def test_hard_filters_unusable_similar_tracks_before_artist_enrichment() -> None:
    """Enrich through similar artists when raw track results are only answer variants."""
    quiz_type, mass = _quiz_type("hard", use_ai=True)
    correct_track, correct = _correct()
    mass.music.tracks.similar_tracks.return_value = [
        correct_track,
        _track("remix", "Around the World (Remix)", "Daft Punk"),
        _track("radio", "Around the World [Radio Edit]", "Daft Punk"),
    ]
    mass.music.artists.similar_artists.return_value = [
        _artist("justice", "Justice"),
        _artist("air", "Air"),
        _artist("phoenix", "Phoenix"),
    ]
    top_tracks = {
        "justice": [_track("j1", "Genesis", "Justice")],
        "air": [_track("a1", "Sexy Boy", "Air")],
        "phoenix": [_track("p1", "Lisztomania", "Phoenix")],
    }
    mass.music.artists.top_tracks.side_effect = lambda item_id, **_kwargs: top_tracks[item_id]
    provider = _ai_provider(
        _ai_response(
            ["candidate_0", "candidate_1", "candidate_2"],
            [
                ("same_artist_title", "Daft Punk - Neon Horizon"),
                ("context_track", "Lunar Circuit - Chrome Reverie"),
            ],
        )
    )
    mass.get_providers_supporting_feature.return_value = [provider]

    result = list(await quiz_type._gather_distractors(correct_track, correct))

    assert [item.label for item in result[:3]] == [
        "Justice - Genesis",
        "Daft Punk - Neon Horizon",
        "Lunar Circuit - Chrome Reverie",
    ]
    mass.music.artists.similar_artists.assert_awaited_once()
    provider.ai_query.assert_awaited_once()


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

    result = list(await quiz_type._gather_distractors(correct_track, correct))

    assert "Daft Punk - One More Time" in {item.label for item in result}


@pytest.mark.asyncio
async def test_easy_difficulty_uses_source_pool() -> None:
    """Easy difficulty traverses the source pool once before search fallback."""
    quiz_type, mass = _quiz_type("easy", use_ai=True)
    correct_track, correct = _correct()
    pool_tracks = [
        _track("p2", "Genesis", "Justice"),
        _track("p3", "Sexy Boy", "Air"),
        _track("p4", "1901", "Phoenix"),
    ]
    quiz_type._source_track_pool = _pool([correct_track, *pool_tracks])
    mass.music.search.return_value = SimpleNamespace(
        tracks=[_track("search", "Fallback", "Someone")]
    )

    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.guess_the_song.SYSTEM_RANDOM.shuffle"
        ) as shuffle_pool,
        patch(
            "music_assistant.providers.music_quiz.quiz_types.guess_the_song._track_to_candidate",
            wraps=_track_to_candidate,
        ) as convert_candidate,
    ):
        result = list(await quiz_type._gather_distractors(correct_track, correct))

    assert [item.label for item in result] == [
        "Justice - Genesis",
        "Air - Sexy Boy",
        "Phoenix - 1901",
        "Someone - Fallback",
    ]
    assert correct_track.uri not in {item.uri for item in result}
    shuffle_pool.assert_called_once()
    assert convert_candidate.call_count == 4
    mass.music.search.assert_awaited_once()
    mass.music.tracks.similar_tracks.assert_not_awaited()
    mass.get_providers_supporting_feature.assert_not_called()


@pytest.mark.asyncio
async def test_hard_ai_distractors_mix_real_and_synthetic_context() -> None:
    """Build the default hard mix from one real and two contextual synthetic choices."""
    quiz_type, mass = _quiz_type("hard", use_ai=True)
    mass.music.tracks.similar_tracks.return_value = [
        _track("st1", "Digital Love", "Daft Punk"),
        _track("st2", "D.A.N.C.E.", "Justice"),
        _track("st3", "Sexy Boy", "Air"),
    ]
    provider = _ai_provider(
        _ai_response(
            ["candidate_2", "candidate_0", "candidate_1"],
            [
                ("same_artist_title", "Daft Punk - Neon Horizon"),
                ("context_track", "Lunar Circuit - Chrome Reverie"),
            ],
        )
    )
    mass.get_providers_supporting_feature.return_value = [provider]
    correct_track, correct = _correct()

    result = list(await quiz_type._gather_distractors(correct_track, correct))

    assert [(item.label, item.uri) for item in result[:3]] == [
        ("Air - Sexy Boy", mass.music.tracks.similar_tracks.return_value[2].uri),
        ("Daft Punk - Neon Horizon", None),
        ("Lunar Circuit - Chrome Reverie", None),
    ]
    assert [item.artist_names for item in result[1:3]] == [
        ("Daft Punk",),
        ("Lunar Circuit",),
    ]
    provider.ai_query.assert_awaited_once()
    prompt = provider.ai_query.await_args.args[0]
    assert CORRECT_LABEL in prompt
    assert "Daft Punk - Digital Love" in prompt
    assert "Justice - D.A.N.C.E." in prompt


@pytest.mark.asyncio
async def test_hard_ai_composition_scales_with_real_catalog_dominance() -> None:
    """Retain two bounded synthetic roles while larger option sets add real tracks."""
    quiz_type, mass = _quiz_type("hard", use_ai=True, suggestion_count=6)
    similar_tracks = [
        _track("st0", "Genesis", "Justice"),
        _track("st1", "Sexy Boy", "Air"),
        _track("st2", "Lisztomania", "Phoenix"),
        _track("st3", "Teardrop", "Massive Attack"),
        _track("st4", "Midnight City", "M83"),
    ]
    mass.music.tracks.similar_tracks.return_value = similar_tracks
    provider = _ai_provider(
        _ai_response(
            [f"candidate_{index}" for index in reversed(range(5))],
            [
                ("same_artist_title", "Daft Punk - Neon Horizon"),
                ("context_track", "Lunar Circuit - Chrome Reverie"),
            ],
        )
    )
    mass.get_providers_supporting_feature.return_value = [provider]
    correct_track, correct = _correct()

    result = list(await quiz_type._gather_distractors(correct_track, correct))

    selected = result[:5]
    assert sum(item.uri is not None for item in selected) == 3
    assert sum(item.uri is None for item in selected) == 2
    assert {item.label for item in selected if item.uri is None} == {
        "Daft Punk - Neon Horizon",
        "Lunar Circuit - Chrome Reverie",
    }
    provider.ai_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_hard_ai_context_preserves_non_english_grounding() -> None:
    """Pass non-English source/catalog data through the bounded untrusted context."""
    quiz_type, mass = _quiz_type("hard", use_ai=True)
    source = _track("source", "Zoutelande", "BLØF")
    correct = _track_to_candidate(source)
    mass.music.tracks.similar_tracks.return_value = [
        _track("st1", "Het Is Een Nacht", "Guus Meeuwis"),
        _track("st2", "Rood", "Marco Borsato"),
        _track("st3", "Iedereen Is Van De Wereld", "The Scene"),
    ]
    provider = _ai_provider(
        _ai_response(
            ["candidate_0", "candidate_1", "candidate_2"],
            [
                ("same_artist_title", "BLØF - Mooie Dagen"),
                ("context_track", "Noorderlicht - Aan Zee"),
            ],
        )
    )
    mass.get_providers_supporting_feature.return_value = [provider]

    result = list(await quiz_type._gather_distractors(source, correct))

    assert {item.label for item in result[:3]} == {
        "Guus Meeuwis - Het Is Een Nacht",
        "BLØF - Mooie Dagen",
        "Noorderlicht - Aan Zee",
    }
    prompt = provider.ai_query.await_args.args[0]
    assert "BLØF" in prompt
    assert "Zoutelande" in prompt
    assert "Guus Meeuwis - Het Is Een Nacht" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "synthetic",
    [
        [
            ("same_artist_title", "Daft Punk - Around the World (Remix)"),
            ("context_track", "Lunar Circuit - Chrome Reverie"),
        ],
        [
            ("same_artist_title", "Neon Horizon"),
            ("context_track", "Lunar Circuit - Chrome Reverie"),
        ],
        [
            ("same_artist_title", "Daft Punk - Neon Horizon"),
            ("context_track", "Daft Punk - Chrome Reverie"),
        ],
        [
            ("same_artist_title", "Daft Punk - Neon Horizon"),
            ("context_track", "Justice - Chrome Reverie"),
        ],
    ],
)
async def test_invalid_hard_ai_semantics_fall_back_to_real_catalog(
    synthetic: list[tuple[str, str]],
) -> None:
    """Reject correct-title leakage and malformed synthetic track roles."""
    quiz_type, mass = _quiz_type("hard", use_ai=True)
    real_tracks = [
        _track("st1", "Digital Love", "Daft Punk"),
        _track("st2", "D.A.N.C.E.", "Justice"),
        _track("st3", "Sexy Boy", "Air"),
    ]
    mass.music.tracks.similar_tracks.return_value = real_tracks
    provider = _ai_provider(
        _ai_response(
            ["candidate_0", "candidate_1", "candidate_2"],
            synthetic,
        )
    )
    mass.get_providers_supporting_feature.return_value = [provider]
    correct_track, correct = _correct()

    result = list(await quiz_type._gather_distractors(correct_track, correct))

    assert [item.label for item in result[:3]] == [
        "Daft Punk - Digital Love",
        "Justice - D.A.N.C.E.",
        "Air - Sexy Boy",
    ]
    assert all(item.uri is not None for item in result[:3])
    provider.ai_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_context_track_cannot_reuse_an_individual_source_contributor() -> None:
    """Reject a context artist that is already a source-track contributor."""
    quiz_type, mass = _quiz_type("hard", use_ai=True)
    correct_track, _ = _correct()
    correct_track.artists.append(_artist("pharrell", "Pharrell Williams"))
    correct = _track_to_candidate(correct_track)
    real_tracks = [
        _track("st1", "Digital Love", "Daft Punk"),
        _track("st2", "D.A.N.C.E.", "Justice"),
        _track("st3", "Sexy Boy", "Air"),
    ]
    mass.music.tracks.similar_tracks.return_value = real_tracks
    provider = _ai_provider(
        _ai_response(
            ["candidate_0", "candidate_1", "candidate_2"],
            [
                (
                    "same_artist_title",
                    f"{correct_track.artist_str} - Neon Horizon",
                ),
                ("context_track", "Pharrell Williams - Chrome Reverie"),
            ],
        )
    )
    mass.get_providers_supporting_feature.return_value = [provider]

    result = list(await quiz_type._gather_distractors(correct_track, correct))

    assert [item.label for item in result[:3]] == [
        "Daft Punk - Digital Love",
        "Justice - D.A.N.C.E.",
        "Air - Sexy Boy",
    ]
    provider.ai_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_primary_ai_response_does_not_try_another_provider() -> None:
    """Use one deterministic provider request before falling back to real tracks."""
    quiz_type, mass = _quiz_type("hard", use_ai=True)
    real_tracks = [
        _track("st1", "Digital Love", "Daft Punk"),
        _track("st2", "D.A.N.C.E.", "Justice"),
        _track("st3", "Sexy Boy", "Air"),
    ]
    mass.music.tracks.similar_tracks.return_value = real_tracks
    invalid = _ai_provider(
        _ai_response(
            ["candidate_0", "candidate_1", "candidate_2"],
            [
                ("same_artist_title", "Daft Punk - Neon Horizon"),
                ("context_track", "Lunar Circuit - Chrome Reverie"),
            ],
            extra=True,
        )
    )
    invalid.instance_id = "ai--a"
    later = _ai_provider()
    later.instance_id = "ai--b"
    mass.get_providers_supporting_feature.return_value = [invalid, later]
    correct_track, correct = _correct()

    result = list(await quiz_type._gather_distractors(correct_track, correct))

    assert [item.label for item in result[:3]] == [
        "Daft Punk - Digital Love",
        "Justice - D.A.N.C.E.",
        "Air - Sexy Boy",
    ]
    invalid.ai_query.assert_awaited_once()
    later.ai_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_hard_ai_timeout_falls_back_to_real_catalog() -> None:
    """Use only real candidates when the bounded AI request times out."""
    quiz_type, mass = _quiz_type("hard", use_ai=True)
    real_tracks = [
        _track("st1", "Digital Love", "Daft Punk"),
        _track("st2", "D.A.N.C.E.", "Justice"),
        _track("st3", "Sexy Boy", "Air"),
    ]
    mass.music.tracks.similar_tracks.return_value = real_tracks
    provider = _ai_provider()

    async def _stall(_prompt: str) -> str:
        await asyncio.sleep(1)
        return ""

    provider.ai_query.side_effect = _stall
    mass.get_providers_supporting_feature.return_value = [provider]
    correct_track, correct = _correct()

    with patch(
        "music_assistant.providers.music_quiz.quiz_types.guess_the_song.AI_QUERY_TIMEOUT_SECONDS",
        0.001,
    ):
        result = list(await quiz_type._gather_distractors(correct_track, correct))

    assert [item.label for item in result[:3]] == [
        "Daft Punk - Digital Love",
        "Justice - D.A.N.C.E.",
        "Air - Sexy Boy",
    ]
    provider.ai_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_hard_ai_provider_failure_falls_back_to_real_catalog() -> None:
    """Use only real candidates when the selected AI provider fails."""
    quiz_type, mass = _quiz_type("hard", use_ai=True)
    real_tracks = [
        _track("st1", "Digital Love", "Daft Punk"),
        _track("st2", "D.A.N.C.E.", "Justice"),
        _track("st3", "Sexy Boy", "Air"),
    ]
    mass.music.tracks.similar_tracks.return_value = real_tracks
    provider = _ai_provider(error=RuntimeError("provider unavailable"))
    mass.get_providers_supporting_feature.return_value = [provider]
    correct_track, correct = _correct()

    result = list(await quiz_type._gather_distractors(correct_track, correct))

    assert [item.label for item in result[:3]] == [
        "Daft Punk - Digital Love",
        "Justice - D.A.N.C.E.",
        "Air - Sexy Boy",
    ]
    provider.ai_query.assert_awaited_once()


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

    with patch(
        "music_assistant.providers.music_quiz.quiz_types.guess_the_song.SYSTEM_RANDOM.shuffle"
    ) as shuffle_pool:
        game_round = await quiz_type.prepare_round(0, [])

    assert game_round.track_uri == correct_track.uri
    assert isinstance(game_round.answer_state, MultipleChoiceRoundState)
    suggestions = game_round.answer_state.suggestions
    assert len(suggestions) == 4
    assert sum(item.is_correct for item in suggestions) == 1
    assert [item.label for item in suggestions if item.is_correct] == [CORRECT_LABEL]
    assert all(" - " in item.label for item in suggestions)
    shuffle_pool.assert_not_called()
    mass.music.tracks.get_provider_item.assert_not_awaited()


@pytest.mark.parametrize("difficulty", ["easy", "normal"])
@pytest.mark.asyncio
async def test_artist_round_excludes_title_only_distractors(difficulty: str) -> None:
    """Keep every option artist-title when the correct track has an artist."""
    quiz_type, mass = _quiz_type(difficulty)
    correct_track, _ = _correct()
    missing_artist = _track("missing", "Hey There Delilah", None)
    pool_distractor = _track("pool", "Digital Love", "Daft Punk")
    quiz_type._source_track_pool = _pool([correct_track, missing_artist, pool_distractor])
    mass.music.search.return_value = SimpleNamespace(
        tracks=[
            missing_artist,
            _track("s1", "Genesis", "Justice"),
            _track("s2", "Sexy Boy", "Air"),
            _track("s3", "Lisztomania", "Phoenix"),
        ]
    )

    with patch(
        "music_assistant.providers.music_quiz.quiz_types.guess_the_song.secrets.choice",
        return_value=correct_track,
    ):
        game_round = await quiz_type.prepare_round(0, [])

    assert isinstance(game_round.answer_state, MultipleChoiceRoundState)
    suggestions = game_round.answer_state.suggestions
    assert len(suggestions) == 4
    assert all(" - " in suggestion.label for suggestion in suggestions)
    assert missing_artist.uri not in {suggestion.uri for suggestion in suggestions}
    mass.music.tracks.get_provider_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_artist_round_projects_every_option_to_title_only() -> None:
    """Prevent the screenshot-like title-only correct answer from standing out."""
    quiz_type, mass = _quiz_type()
    correct_track = _track("source", "Hey There Delilah", None)
    quiz_type._source_track_pool = _pool([correct_track])
    mass.music.tracks.get_provider_item.return_value = correct_track
    mass.music.search.return_value = SimpleNamespace(
        tracks=[
            _track("s1", "Use Somebody", "Kings of Leon"),
            _track("s2", "Apologize", "Timbaland"),
            _track("s3", "Chasing Cars", "Snow Patrol"),
        ]
    )

    with patch(
        "music_assistant.providers.music_quiz.suggestions.secrets.token_hex",
        side_effect=["id-a", "id-b", "id-c", "id-d"],
    ):
        game_round = await quiz_type.prepare_round(0, [])

    assert isinstance(game_round.answer_state, MultipleChoiceRoundState)
    suggestions = game_round.answer_state.suggestions
    assert {suggestion.label for suggestion in suggestions} == {
        "Hey There Delilah",
        "Use Somebody",
        "Apologize",
        "Chasing Cars",
    }
    assert all(" - " not in suggestion.label for suggestion in suggestions)
    assert {suggestion.suggestion_id for suggestion in suggestions} == {
        "id-a",
        "id-b",
        "id-c",
        "id-d",
    }
    correct = next(suggestion for suggestion in suggestions if suggestion.is_correct)
    assert correct.uri == correct_track.uri
    assert correct.label == game_round.answer_label == "Hey There Delilah"
    mass.music.tracks.get_provider_item.assert_awaited_once_with(
        correct_track.item_id,
        correct_track.provider,
    )


@pytest.mark.asyncio
async def test_selected_track_recovers_artist_with_one_full_item_lookup() -> None:
    """Use recovered artist metadata without changing the selected source identity."""
    quiz_type, mass = _quiz_type()
    correct_track = _track("source", "Hey There Delilah", None)
    full_track = _track("source", "Hey There Delilah", "Plain White T's")
    quiz_type._source_track_pool = _pool([correct_track])
    mass.music.tracks.get_provider_item.return_value = full_track
    mass.music.search.return_value = SimpleNamespace(
        tracks=[
            _track("s1", "Use Somebody", "Kings of Leon"),
            _track("s2", "Apologize", "Timbaland"),
            _track("s3", "Chasing Cars", "Snow Patrol"),
        ]
    )

    game_round = await quiz_type.prepare_round(0, [])

    assert isinstance(game_round.answer_state, MultipleChoiceRoundState)
    assert game_round.track_uri == correct_track.uri
    assert game_round.answer_label == "Plain White T's - Hey There Delilah"
    assert all(" - " in suggestion.label for suggestion in game_round.answer_state.suggestions)
    correct = next(
        suggestion for suggestion in game_round.answer_state.suggestions if suggestion.is_correct
    )
    assert correct.uri == correct_track.uri
    mass.music.tracks.get_provider_item.assert_awaited_once_with(
        correct_track.item_id,
        correct_track.provider,
    )


@pytest.mark.asyncio
async def test_selected_track_artist_lookup_error_falls_back_to_title_only() -> None:
    """Keep a playable source when its optional artist lookup fails."""
    quiz_type, mass = _quiz_type(suggestion_count=2)
    correct_track = _track("source", "Hey There Delilah", None)
    quiz_type._source_track_pool = _pool([correct_track])
    mass.music.tracks.get_provider_item.side_effect = RuntimeError("unavailable")
    mass.music.search.return_value = SimpleNamespace(
        tracks=[_track("s1", "Use Somebody", "Kings of Leon")]
    )

    game_round = await quiz_type.prepare_round(0, [])

    assert isinstance(game_round.answer_state, MultipleChoiceRoundState)
    assert {suggestion.label for suggestion in game_round.answer_state.suggestions} == {
        "Hey There Delilah",
        "Use Somebody",
    }
    mass.music.tracks.get_provider_item.assert_awaited_once_with(
        correct_track.item_id,
        correct_track.provider,
    )


@pytest.mark.asyncio
async def test_normal_pool_fallback_scans_large_cached_pool_across_rounds() -> None:
    """Fill collapsed search results lazily from one cached large source pool."""
    quiz_type, mass = _quiz_type()
    first_title = "Zij W\u0069l Mij"
    first_correct = _track("first", first_title, "FLEMMING")
    second_correct = _track("second", "Teardrop", "Massive Attack")
    alternatives = [
        _track("a1", "Genesis", "Justice"),
        _track("a2", "Lisztomania", "Phoenix"),
        _track("a3", "Chasing Cars", "Snow Patrol"),
        _track("a4", "Midnight City", "M83"),
    ]
    source_pool = _pool(
        [
            first_correct,
            second_correct,
            *alternatives,
            *[
                _track(f"bulk-{index}", f"Bulk Song {index}", f"Bulk Artist {index}")
                for index in range(1000)
            ],
        ]
    )
    quiz_type._source_track_pool = source_pool
    mass.music.search.side_effect = [
        SimpleNamespace(
            tracks=[
                first_correct,
                _track("first-copy", first_title, "FLEMMING"),
                _track("first-remix", f"{first_title} (Remix)", "FLEMMING"),
            ]
        ),
        SimpleNamespace(
            tracks=[
                second_correct,
                _track("second-copy", "Teardrop", "Massive Attack"),
                _track("second-remix", "Teardrop (Remastered)", "Massive Attack"),
            ]
        ),
    ]

    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.guess_the_song.secrets.choice",
            side_effect=[first_correct, second_correct],
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.guess_the_song.SYSTEM_RANDOM.shuffle"
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.guess_the_song._track_to_candidate",
            wraps=_track_to_candidate,
        ) as convert_candidate,
    ):
        first_round = await quiz_type.prepare_round(0, [])
        second_round = await quiz_type.prepare_round(1, [first_round])

    for game_round in (first_round, second_round):
        assert isinstance(game_round.answer_state, MultipleChoiceRoundState)
        assert len(game_round.answer_state.suggestions) == 4
        assert all(" - " in item.label for item in game_round.answer_state.suggestions)
    assert first_round.answer_label == f"FLEMMING - {first_title}"
    assert second_round.answer_label == "Massive Attack - Teardrop"
    assert convert_candidate.call_count == 14
    assert mass.music.search.await_count == 2
    assert all(item.kwargs["limit"] == 32 for item in mass.music.search.await_args_list)
    mass.music.get_item.assert_not_awaited()
    mass.music.tracks.get_provider_item.assert_not_awaited()
    mass.music.tracks.similar_tracks.assert_not_awaited()
    assert quiz_type._source_track_pool is source_pool


@pytest.mark.asyncio
async def test_title_only_projection_refilters_duplicate_and_close_titles() -> None:
    """Continue through pool collisions until enough projected titles survive."""
    quiz_type, mass = _quiz_type()
    correct_track = _track("source", "Hey There Delilah", None)
    quiz_type._source_track_pool = _pool(
        [
            correct_track,
            _track("s1", "Electric Feel", "Artist One"),
            _track("s2", "Electric Feel", "Artist Two"),
            _track("s3", "Electric Feel (Radio Edit)", "Artist Three"),
            _track("s4", "Genesis", "Justice"),
            _track("s5", "Lisztomania", "Phoenix"),
            _track("unused", "Teardrop", "Massive Attack"),
        ]
    )
    mass.music.tracks.get_provider_item.return_value = correct_track

    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.guess_the_song.secrets.choice",
            return_value=correct_track,
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.guess_the_song.SYSTEM_RANDOM.shuffle"
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.guess_the_song._track_to_candidate",
            wraps=_track_to_candidate,
        ) as convert_candidate,
    ):
        game_round = await quiz_type.prepare_round(0, [])

    assert isinstance(game_round.answer_state, MultipleChoiceRoundState)
    assert {suggestion.label for suggestion in game_round.answer_state.suggestions} == {
        "Hey There Delilah",
        "Electric Feel",
        "Genesis",
        "Lisztomania",
    }
    assert convert_candidate.call_count == 6
    mass.music.search.assert_awaited_once()


@pytest.mark.asyncio
async def test_title_only_projection_reports_insufficient_candidates() -> None:
    """Return the localized distractor error when projected titles all overlap."""
    quiz_type, mass = _quiz_type(suggestion_count=3)
    correct_track = _track("source", "Hey There Delilah", None)
    quiz_type._source_track_pool = _pool(
        [
            correct_track,
            _track("s1", "Electric Feel", "Artist One"),
            _track("s2", "Electric Feel", "Artist Two"),
            _track("s3", "Electric Feel (Radio Edit)", "Artist Three"),
        ]
    )
    mass.music.tracks.get_provider_item.return_value = correct_track

    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.guess_the_song.secrets.choice",
            return_value=correct_track,
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.guess_the_song.SYSTEM_RANDOM.shuffle"
        ),
        pytest.raises(InvalidDataError) as err,
    ):
        await quiz_type.prepare_round(0, [])

    assert err.value.translation_key == "music_quiz_not_enough_distractors"
    mass.music.search.assert_awaited_once()


@pytest.mark.asyncio
async def test_hard_ai_uses_pool_to_fill_real_candidate_slots() -> None:
    """Keep the hard AI composition when preferred real candidates are sparse."""
    quiz_type, mass = _quiz_type("hard", use_ai=True, suggestion_count=6)
    correct_track, _ = _correct()
    pool_tracks = [
        _track("p1", "Genesis", "Justice"),
        _track("p2", "Lisztomania", "Phoenix"),
        _track("p3", "Chasing Cars", "Snow Patrol"),
    ]
    quiz_type._source_track_pool = _pool([correct_track, *pool_tracks])
    similar = _track("similar", "Digital Love", "Daft Punk")
    mass.music.tracks.similar_tracks.return_value = [similar]
    mass.music.search.return_value = SimpleNamespace(
        tracks=[
            correct_track,
            _track("copy", "Around the World", "Daft Punk"),
            _track("remix", "Around the World (Remix)", "Daft Punk"),
        ]
    )
    provider = _ai_provider(
        _ai_response(
            ["candidate_0", "candidate_1", "candidate_2"],
            [
                ("same_artist_title", "Daft Punk - Neon Horizon"),
                ("context_track", "Lunar Circuit - Chrome Reverie"),
            ],
        )
    )
    mass.get_providers_supporting_feature.return_value = [provider]

    with (
        patch(
            "music_assistant.providers.music_quiz.quiz_types.guess_the_song.secrets.choice",
            return_value=correct_track,
        ),
        patch(
            "music_assistant.providers.music_quiz.quiz_types.guess_the_song.SYSTEM_RANDOM.shuffle"
        ),
    ):
        game_round = await quiz_type.prepare_round(0, [])

    assert isinstance(game_round.answer_state, MultipleChoiceRoundState)
    suggestions = game_round.answer_state.suggestions
    assert len(suggestions) == 6
    assert sum(item.is_correct for item in suggestions) == 1
    assert sum(item.uri is None for item in suggestions) == 2
    assert len({item.uri for item in suggestions} & {item.uri for item in pool_tracks}) == 2
    assert all(" - " in item.label for item in suggestions)
    provider.ai_query.assert_awaited_once()
    mass.music.tracks.similar_tracks.assert_awaited_once_with(
        item_id=correct_track.item_id,
        provider_instance_id_or_domain=correct_track.provider,
        limit=24,
    )
    assert mass.music.search.await_args.kwargs["limit"] == 48
    mass.music.get_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_round_keeps_hard_ai_artist_title_shape() -> None:
    """Retain artist-title formatting for real and synthetic hard-mode options."""
    quiz_type, mass = _quiz_type("hard", use_ai=True)
    correct_track = _track("c1", "Around the World", None)
    quiz_type._source_track_pool = _pool([correct_track])
    mass.music.tracks.get_provider_item.return_value = _track(
        "c1",
        "Around the World",
        "Daft Punk",
    )
    mass.music.tracks.similar_tracks.return_value = [
        _track("st1", "Digital Love", "Daft Punk"),
        _track("st2", "D.A.N.C.E.", "Justice"),
        _track("st3", "Sexy Boy", "Air"),
    ]
    provider = _ai_provider(
        _ai_response(
            ["candidate_2", "candidate_0", "candidate_1"],
            [
                ("same_artist_title", "Daft Punk - Neon Horizon"),
                ("context_track", "Lunar Circuit - Chrome Reverie"),
            ],
        )
    )
    mass.get_providers_supporting_feature.return_value = [provider]

    game_round = await quiz_type.prepare_round(0, [])

    assert isinstance(game_round.answer_state, MultipleChoiceRoundState)
    suggestions = game_round.answer_state.suggestions
    assert len(suggestions) == 4
    assert all(" - " in suggestion.label for suggestion in suggestions)
    assert game_round.answer_label == CORRECT_LABEL
    assert sum(suggestion.is_correct for suggestion in suggestions) == 1
    provider.ai_query.assert_awaited_once()
    mass.music.tracks.get_provider_item.assert_awaited_once_with(
        correct_track.item_id,
        correct_track.provider,
    )
