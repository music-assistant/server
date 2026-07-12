"""Tests for the guess-the-song quiz type distractor sourcing."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.music_quiz.models import (
    MultipleChoiceRoundState,
    MusicQuizConfig,
)
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


def _ai_provider(response: object = None, error: Exception | None = None) -> MagicMock:
    """Return a mock AI_QUERY-capable plugin provider."""
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = "ai--1"
    provider.ai_query = AsyncMock(return_value=response, side_effect=error)
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
async def test_normal_difficulty_uses_search_only() -> None:
    """Normal difficulty draws distractors from a catalog search and nothing else."""
    quiz_type, mass = _quiz_type("normal", use_ai=True)
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

    result = await quiz_type._gather_distractors(correct_track, correct)

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

    result = await quiz_type._gather_distractors(correct_track, correct)

    assert "Daft Punk - One More Time" in {item.label for item in result}


@pytest.mark.asyncio
async def test_easy_difficulty_uses_source_pool() -> None:
    """Easy difficulty offers other tracks from the configured source pool."""
    quiz_type, mass = _quiz_type("easy", use_ai=True)
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

    result = await quiz_type._gather_distractors(correct_track, correct)

    assert [(item.label, item.uri) for item in result[:3]] == [
        ("Air - Sexy Boy", mass.music.tracks.similar_tracks.return_value[2].uri),
        ("Daft Punk - Neon Horizon", None),
        ("Lunar Circuit - Chrome Reverie", None),
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

    result = await quiz_type._gather_distractors(correct_track, correct)

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
    correct = SuggestionCandidate(
        "BLØF - Zoutelande",
        source.uri,
        title="Zoutelande",
    )
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

    result = await quiz_type._gather_distractors(source, correct)

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

    result = await quiz_type._gather_distractors(correct_track, correct)

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
    correct = SuggestionCandidate(
        f"{correct_track.artist_str} - {correct_track.name}",
        correct_track.uri,
        title=correct_track.name,
    )
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

    result = await quiz_type._gather_distractors(correct_track, correct)

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
    mass.get_providers_supporting_feature.return_value = [later, invalid]
    correct_track, correct = _correct()

    result = await quiz_type._gather_distractors(correct_track, correct)

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
        result = await quiz_type._gather_distractors(correct_track, correct)

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

    result = await quiz_type._gather_distractors(correct_track, correct)

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

    game_round = await quiz_type.prepare_round(0, [])

    assert game_round.track_uri == correct_track.uri
    assert isinstance(game_round.answer_state, MultipleChoiceRoundState)
    suggestions = game_round.answer_state.suggestions
    assert len(suggestions) == 4
    assert sum(item.is_correct for item in suggestions) == 1
    assert [item.label for item in suggestions if item.is_correct] == [CORRECT_LABEL]
