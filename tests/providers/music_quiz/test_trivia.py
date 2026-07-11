"""Tests for AI-grounded Music Trivia generation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.music_quiz.errors import MusicQuizAIUnavailableError
from music_assistant.providers.music_quiz.models import (
    MultipleChoiceRoundState,
    MusicQuizAnswerType,
    MusicQuizConfig,
    TimelineBonusMode,
)
from music_assistant.providers.music_quiz.quiz_types import get_quiz_type
from music_assistant.providers.music_quiz.quiz_types.trivia import (
    MAX_METADATA_VALUE_LENGTH,
    MAX_QUESTION_LENGTH,
    TriviaFact,
    TriviaMetadata,
    TriviaQuizType,
    parse_trivia_response,
)
from music_assistant.providers.music_quiz.suggestions import normalize_answer_label


def _track(
    item_id: str = "track-1",
    name: str = "Teardrop",
    artist: str = "Massive Attack",
    *,
    album: str | None = "Mezzanine",
    release_year: int | None = 1998,
    genres: set[str] | None = None,
    available: bool = True,
    is_playable: bool = True,
) -> Track:
    """Return a source track with configurable safe trivia metadata."""
    provider = "prov"
    track = Track(
        item_id=item_id,
        provider=provider,
        name=name,
        is_playable=is_playable,
        artists=UniqueList(
            [
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=f"artist-{item_id}",
                    provider=provider,
                    name=artist,
                )
            ]
        ),
        album=(
            ItemMapping(
                media_type=MediaType.ALBUM,
                item_id=f"album-{item_id}",
                provider=provider,
                name=album,
                year=release_year,
            )
            if album
            else None
        ),
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider,
                provider_instance=provider,
                available=available,
            )
        },
    )
    if release_year is not None and album is None:
        track.metadata.release_date = datetime(release_year, 1, 1, tzinfo=UTC)
    track.metadata.genres = genres
    return track


def _ai_provider(response: str | None = None) -> MagicMock:
    """Return an AI-query capable plugin provider."""
    provider = MagicMock(spec=PluginProvider)
    provider.instance_id = "ai--test"
    provider.ai_query = AsyncMock(return_value=response)
    return provider


def _quiz(
    tracks: list[Track],
    provider: MagicMock,
    *,
    round_count: int = 1,
    suggestion_count: int = 4,
    difficulty: str = "normal",
) -> tuple[TriviaQuizType, MagicMock]:
    """Return a Music Trivia strategy backed by deterministic source tracks."""
    mass = MagicMock()
    mass.get_providers_supporting_feature.return_value = [provider]
    config = MusicQuizConfig(
        round_count=round_count,
        suggestion_count=suggestion_count,
        source_uris=["prov://playlist/source"],
        difficulty=difficulty,
    )
    quiz = TriviaQuizType(mass, config)
    quiz._source_track_pool = {track.uri: track for track in tracks if track.uri}
    quiz._source_playlist_names = {track.uri: {"Night Mix"} for track in tracks if track.uri}
    return quiz, mass


def _response_for_prompt(prompt: str) -> str:
    """Return a valid strict response for the supplied fact prompt."""
    supplied = json.loads(prompt.split("Supplied metadata:\n", 1)[1])
    fact = supplied["target_fact"]
    distractors = {
        "artist": ["Johann Sebastian Bach", "Beyonce", "Radiohead"],
        "track": ["Blue Monday", "Hallelujah", "Paper Planes"],
        "album": ["Discovery", "Homogenic", "Rumours"],
        "release_year": ["1971", "2005", "2020"],
        "genre": ["Classical", "Country", "Electronic Dance"],
        "playlist": ["Morning Acoustic", "Workout Anthems", "Piano Focus"],
    }[fact["type"]]
    return json.dumps(
        {
            "question": (f"Which {fact['type'].replace('_', ' ')} matches {fact['subject']}?"),
            "correct_answer": fact["correct_answer"],
            "distractors": distractors,
        }
    )


def _fact() -> TriviaFact:
    """Return a simple retained source fact for parser tests."""
    metadata = TriviaMetadata(
        title="Teardrop",
        artist="Massive Attack",
        album="Mezzanine",
        release_year=1998,
        genres=("Trip-hop",),
        playlists=("Night Mix",),
    )
    return TriviaFact("artist", "Teardrop", "Massive Attack", metadata)


def test_registry_identity_and_capabilities() -> None:
    """Register Music Trivia as non-audio multiple choice."""
    trivia_type = get_quiz_type("trivia")

    assert trivia_type is TriviaQuizType
    assert trivia_type.answer_type is MusicQuizAnswerType.MULTIPLE_CHOICE
    assert trivia_type.uses_playback is False
    assert trivia_type.warm_up_lyrics is False
    assert trivia_type.supports_listen_in is False


def test_config_normalization_and_validation_are_trivia_specific() -> None:
    """Keep shared trivia controls while removing unrelated game settings."""
    config = MusicQuizConfig(
        round_count=3,
        suggestion_count=6,
        source_uris=["prov://playlist/source"],
        difficulty="hard",
        use_ai_distractors=True,
        artist_bonus_mode=TimelineBonusMode.FREE_TEXT,
        title_bonus_mode=TimelineBonusMode.MULTIPLE_CHOICE,
    )

    normalized = TriviaQuizType.normalize_config(config)
    TriviaQuizType.validate_config(normalized)

    assert normalized.suggestion_count == 6
    assert normalized.difficulty == "hard"
    assert normalized.use_ai_distractors is False
    assert normalized.artist_bonus_mode is TimelineBonusMode.OFF
    assert normalized.title_bonus_mode is TimelineBonusMode.OFF


def test_strict_response_parser_accepts_only_retained_source_truth() -> None:
    """Retain the server-owned answer while accepting a valid grounded response."""
    fact = _fact()
    response = json.dumps(
        {
            "question": "Which artist recorded Teardrop?",
            "correct_answer": "Massive Attack",
            "distractors": ["Beyonce", "Radiohead", "Johann Sebastian Bach"],
        }
    )

    parsed = parse_trivia_response(response, fact, 3)

    assert parsed.question == "Which artist recorded Teardrop?"
    assert parsed.distractors == ("Beyonce", "Radiohead", "Johann Sebastian Bach")
    assert fact.correct_answer == "Massive Attack"


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        json.dumps(
            {
                "question": "Which artist recorded Teardrop?",
                "correct_answer": "Portishead",
                "distractors": ["Beyonce", "Radiohead", "Bach"],
            }
        ),
        json.dumps(
            {
                "question": "Which artist recorded Angel?",
                "correct_answer": "Massive Attack",
                "distractors": ["Beyonce", "Radiohead", "Bach"],
            }
        ),
        json.dumps(
            {
                "question": "Did Massive Attack record Teardrop?",
                "correct_answer": "Massive Attack",
                "distractors": ["Beyonce", "Radiohead", "Bach"],
            }
        ),
        json.dumps(
            {
                "question": "Which artist recorded Teardrop?",
                "correct_answer": "Massive Attack",
                "distractors": ["Beyonce", "Beyonce", "Radiohead"],
            }
        ),
        json.dumps(
            {
                "question": "Which artist recorded Teardrop?",
                "correct_answer": "Massive Attack",
                "distractors": ["Beyonce", "Radiohead"],
            }
        ),
        json.dumps(
            {
                "question": "Which artist recorded Teardrop?",
                "correct_answer": "Massive Attack",
                "distractors": ["Beyonce", "Radiohead", "Bach"],
                "extra": True,
            }
        ),
    ],
)
def test_strict_response_parser_rejects_malformed_or_ungrounded_output(payload: str) -> None:
    """Reject schema drift, answer hallucination, leaks, duplicates and wrong counts."""
    with pytest.raises(ValueError, match="Music Trivia"):
        parse_trivia_response(payload, _fact(), 3)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("question", "Q" * (MAX_QUESTION_LENGTH + 1)),
        ("correct_answer", "A" * (MAX_METADATA_VALUE_LENGTH + 1)),
        ("distractor", "D" * (MAX_METADATA_VALUE_LENGTH + 1)),
    ],
)
def test_strict_response_parser_enforces_length_limits(field: str, value: str) -> None:
    """Reject questions and options outside the bounded wire contract."""
    payload: dict[str, Any] = {
        "question": "Which artist recorded Teardrop?",
        "correct_answer": "Massive Attack",
        "distractors": ["Beyonce", "Radiohead", "Bach"],
    }
    if field == "distractor":
        payload["distractors"] = [value, "Radiohead", "Bach"]
    else:
        payload[field] = value

    with pytest.raises(ValueError, match="Music Trivia"):
        parse_trivia_response(json.dumps(payload), _fact(), 3)


@pytest.mark.asyncio
async def test_generation_uses_only_safe_source_metadata_and_server_answers() -> None:
    """Generate unique rounds without exposing source identifiers to the AI."""
    provider = _ai_provider()
    captured_prompts: list[str] = []

    async def _respond(prompt: str) -> str:
        captured_prompts.append(prompt)
        return _response_for_prompt(prompt)

    provider.ai_query.side_effect = _respond
    track = _track(genres={"Trip-hop"})
    quiz, _ = _quiz([track], provider, round_count=4)

    await quiz.initialize()

    assert len(quiz._prepared_rounds) == 4
    assert len({game_round.question for game_round in quiz._prepared_rounds}) == 4
    answers = {game_round.answer_label for game_round in quiz._prepared_rounds}
    assert answers <= {
        "Teardrop",
        "Massive Attack",
        "Mezzanine",
        "1998",
        "Trip-hop",
        "Night Mix",
    }
    assert len(answers) == 4
    for game_round in quiz._prepared_rounds:
        assert game_round.track_uri is None
        assert game_round.duration is None
        assert isinstance(game_round.answer_state, MultipleChoiceRoundState)
        suggestions = game_round.answer_state.suggestions
        assert len(suggestions) == 4
        assert len({item.suggestion_id for item in suggestions}) == 4
        assert all(item.uri is None for item in suggestions)
        assert sum(item.is_correct for item in suggestions) == 1
        assert (
            next(item.label for item in suggestions if item.is_correct) == game_round.answer_label
        )
        assert all("correct" not in item.suggestion_id for item in suggestions)

    assert captured_prompts
    prompt_text = "\n".join(captured_prompts)
    assert track.uri is not None
    assert track.item_id not in prompt_text
    assert track.uri not in prompt_text
    assert "artist-track-1" not in prompt_text
    assert "album-track-1" not in prompt_text
    assert '"selected_playlists": ["Night Mix"]' in prompt_text
    assert "using ONLY the supplied JSON metadata" in prompt_text


@pytest.mark.asyncio
async def test_fact_pool_excludes_unavailable_and_unplayable_tracks() -> None:
    """Ground questions only in source items that can actually be played."""
    playable = _track()
    unavailable = _track(
        "unavailable",
        "Unavailable Song",
        "Unavailable Artist",
        available=False,
    )
    unplayable = _track(
        "unplayable",
        "Unplayable Song",
        "Unplayable Artist",
        is_playable=False,
    )
    quiz, _ = _quiz([playable, unavailable, unplayable], _ai_provider())

    facts = await quiz._build_fact_pool()

    prompt_metadata = [fact.metadata for fact in facts]
    assert prompt_metadata
    assert {metadata.title for metadata in prompt_metadata} == {"Teardrop"}
    assert {metadata.artist for metadata in prompt_metadata} == {"Massive Attack"}


@pytest.mark.asyncio
async def test_generation_retries_rejected_output() -> None:
    """Retry malformed AI output before accepting a strictly valid response."""
    provider = _ai_provider()
    call_count = 0

    async def _respond(prompt: str) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return '{"question": "not enough fields"}'
        return _response_for_prompt(prompt)

    provider.ai_query.side_effect = _respond
    quiz, _ = _quiz([_track()], provider)

    await quiz.initialize()

    assert provider.ai_query.await_count == 2
    assert len(quiz._prepared_rounds) == 1


@pytest.mark.asyncio
async def test_generation_rejects_unusable_ai_and_insufficient_metadata() -> None:
    """Raise precise localized errors instead of presenting unverified rounds."""
    provider = _ai_provider('{"question": "malformed"}')
    unusable_quiz, _ = _quiz([_track()], provider)

    with pytest.raises(MusicQuizAIUnavailableError) as unavailable:
        await unusable_quiz.initialize()
    assert unavailable.value.translation_key == "music_quiz_ai_unavailable"

    sparse_track = _track(album=None, release_year=None, genres=None)
    insufficient_quiz, _ = _quiz([sparse_track], provider, round_count=3)
    insufficient_quiz._source_playlist_names = {}
    with pytest.raises(InvalidDataError) as insufficient:
        await insufficient_quiz.initialize()
    assert insufficient.value.translation_key == "music_quiz_not_enough_trivia_questions"


@pytest.mark.asyncio
async def test_prepare_round_requires_consistent_unique_history() -> None:
    """Serve only the fully prepared round sequence for this game."""
    provider = _ai_provider()
    provider.ai_query.side_effect = _response_for_prompt
    quiz, _ = _quiz([_track(genres={"Trip-hop"})], provider, round_count=2)
    await quiz.initialize()

    first_round = await quiz.prepare_round(0, [])
    second_round = await quiz.prepare_round(1, [first_round])

    assert first_round.round_index == 0
    assert second_round.round_index == 1
    assert first_round.question != second_round.question
    assert normalize_answer_label(first_round.answer_label) != normalize_answer_label(
        second_round.answer_label
    )
    with pytest.raises(InvalidDataError, match="history"):
        await quiz.prepare_round(1, [])


def test_ai_availability_requires_a_loaded_plugin_provider() -> None:
    """Advertise Trivia only for an available AI-capable plugin instance."""
    mass = MagicMock()
    mass.get_providers_supporting_feature.return_value = []
    assert TriviaQuizType.is_available(mass) is False

    mass.get_providers_supporting_feature.return_value = [SimpleNamespace()]
    assert TriviaQuizType.is_available(mass) is False

    mass.get_providers_supporting_feature.return_value = [_ai_provider()]
    assert TriviaQuizType.is_available(mass) is True
