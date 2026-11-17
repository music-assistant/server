"""Tests for the Music Quiz title-reveal quiz type."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.music.recency import RecencySnapshot
from music_assistant.providers.music_quiz.answer_types.hint_text import HintTextAnswerType
from music_assistant.providers.music_quiz.models import (
    HintTextRoundState,
    MusicQuizAnswerType,
    MusicQuizConfig,
    MusicQuizGame,
    TimelineBonusMode,
    TitleRevealHintSpeed,
    TitleRevealTypoTolerance,
)
from music_assistant.providers.music_quiz.quiz_types.title_reveal import TitleRevealQuizType


def _track(item_id: str, name: str, duration: int = 180) -> Track:
    """Return a minimal Track by a single artist."""
    return Track(
        item_id=item_id,
        provider="prov",
        name=name,
        duration=duration,
        artists=UniqueList(
            [
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id="artist",
                    provider="prov",
                    name="The Band",
                )
            ]
        ),
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="prov", provider_instance="prov")
        },
    )


def _quiz_type(tracks: list[Track], **config: object) -> TitleRevealQuizType:
    """Return a title-reveal quiz type drawing from the given tracks."""
    mass = MagicMock()
    mass.metadata.get_image_url_for_item = AsyncMock(return_value=None)
    mass.music.recency.snapshot = AsyncMock(return_value=RecencySnapshot(now=0))
    quiz_type = TitleRevealQuizType(
        mass,
        MusicQuizConfig(source_uris=["prov://playlist/1"], answer_duration=31, **config),  # type: ignore[arg-type]
    )
    quiz_type._source_track_pool = {track.uri: track for track in tracks if track.uri}
    return quiz_type


@pytest.mark.asyncio
async def test_prepare_round_builds_a_masked_title_round() -> None:
    """Prepare a round with the cleaned title, typo budget and spread hints."""
    quiz_type = _quiz_type(
        [_track("1", "Bohemian Rhapsody - Remastered 2011")],
        typo_tolerance=TitleRevealTypoTolerance.NORMAL,
        hint_speed=TitleRevealHintSpeed.NORMAL,
    )

    game_round = await quiz_type.prepare_round(0, [])

    state = game_round.answer_state
    assert isinstance(state, HintTextRoundState)
    assert state.solution == "Bohemian Rhapsody"
    assert game_round.answer_label == "The Band - Bohemian Rhapsody"
    assert state.accepted_answers == ["bohemianrhapsody"]
    assert state.allowed_typos == 3
    assert len(state.reveal_order) == 16
    assert len(state.hint_fractions) == 9
    assert state.hint_fractions[0] == 0.2
    assert state.hint_fractions[-1] == 0.85
    game = MusicQuizGame(
        config=quiz_type.config,
        quiz_type="title_reveal",
        answer_type=MusicQuizAnswerType.HINT_TEXT,
    )
    HintTextAnswerType().validate_round(game, state)


@pytest.mark.asyncio
async def test_prepare_round_skips_unguessable_and_repeated_titles() -> None:
    """Skip titles that are too short and songs already played in another version."""
    previous = await _quiz_type([_track("1", "Hey Jude")]).prepare_round(0, [])
    quiz_type = _quiz_type(
        [
            _track("1", "Hey Jude"),
            _track("2", "Hey Jude - Live"),
            _track("3", "X"),
            _track("4", "Let It Be"),
        ]
    )

    game_round = await quiz_type.prepare_round(1, [previous])

    assert isinstance(game_round.answer_state, HintTextRoundState)
    assert game_round.answer_state.solution == "Let It Be"


@pytest.mark.asyncio
async def test_prepare_round_fails_without_guessable_tracks() -> None:
    """Fail the round when no source track has a guessable title."""
    quiz_type = _quiz_type([_track("1", "X")])

    with pytest.raises(InvalidDataError):
        await quiz_type.prepare_round(0, [])


def test_validate_config_requires_sources() -> None:
    """Refuse a title-reveal game without sources."""
    with pytest.raises(InvalidDataError):
        TitleRevealQuizType.validate_config(MusicQuizConfig())


def test_serialize_game_config_exposes_hint_settings() -> None:
    """Expose the hint speed and typo tolerance to clients."""
    game = MusicQuizGame(
        config=MusicQuizConfig(
            hint_speed=TitleRevealHintSpeed.FAST,
            typo_tolerance=TitleRevealTypoTolerance.STRICT,
        ),
        quiz_type="title_reveal",
        answer_type=MusicQuizAnswerType.HINT_TEXT,
    )

    assert TitleRevealQuizType.serialize_game_config(game) == {
        "include_similar_music": False,
        "hint_speed": "fast",
        "typo_tolerance": "strict",
        "artist_bonus_mode": "off",
    }


@pytest.mark.asyncio
async def test_prepare_round_offers_the_artist_bonus_only_when_enabled() -> None:
    """Store the accepted artist names only for games with the artist bonus."""
    track = _track("1", "Hey Jude")

    without_bonus = await _quiz_type([track]).prepare_round(0, [])
    with_bonus = await _quiz_type(
        [track], artist_bonus_mode=TimelineBonusMode.FREE_TEXT
    ).prepare_round(0, [])

    assert isinstance(without_bonus.answer_state, HintTextRoundState)
    assert without_bonus.answer_state.artist_answers == []
    assert isinstance(with_bonus.answer_state, HintTextRoundState)
    assert with_bonus.answer_state.artist_answers == ["The Band", "band, the"]
    assert with_bonus.answer_state.artist_label == "The Band"


def test_config_keeps_artist_bonus_and_rejects_multiple_choice() -> None:
    """Keep a free-text artist bonus and refuse the timeline-only multiple-choice mode."""
    config = TitleRevealQuizType.normalize_config(
        MusicQuizConfig(
            source_uris=["prov://playlist/1"],
            artist_bonus_mode=TimelineBonusMode.FREE_TEXT,
            title_bonus_mode=TimelineBonusMode.FREE_TEXT,
        )
    )

    assert config.artist_bonus_mode == TimelineBonusMode.FREE_TEXT
    assert config.title_bonus_mode == TimelineBonusMode.OFF
    TitleRevealQuizType.validate_config(config)
    with pytest.raises(InvalidDataError):
        TitleRevealQuizType.validate_config(
            MusicQuizConfig(
                source_uris=["prov://playlist/1"],
                artist_bonus_mode=TimelineBonusMode.MULTIPLE_CHOICE,
            )
        )
