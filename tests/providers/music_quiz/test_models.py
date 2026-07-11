"""Tests for Music Quiz persisted models."""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import pytest
from mashumaro.exceptions import ExtraKeysError, InvalidFieldValue

from music_assistant.providers.music_quiz.models import (
    MultipleChoiceAnswer,
    MultipleChoiceRoundState,
    MultipleChoiceSuggestion,
    MusicQuizAnswerType,
    MusicQuizPlayer,
    MusicQuizRound,
    TimelineAnswerResult,
    TimelineBonusOption,
    TimelineBonusResult,
    TimelineBonusType,
    TimelineCandidate,
    TimelineChoiceBonusAnswer,
    TimelineEntry,
    TimelineFreeTextBonusDefinition,
    TimelineMultipleChoiceBonusDefinition,
    TimelinePlacementAnswer,
    TimelinePlacementResult,
    TimelineRoundState,
)


def test_round_answer_state_round_trips_with_discriminator() -> None:
    """Round-trip strict multiple-choice state through the common round model."""
    game_round = MusicQuizRound(
        round_index=0,
        answer_label="Daft Punk - One More Time",
        answer_state=MultipleChoiceRoundState(
            suggestions=[
                MultipleChoiceSuggestion(
                    suggestion_id="correct",
                    label="Daft Punk - One More Time",
                    uri="library://track/1",
                    is_correct=True,
                )
            ],
            answers={
                "p1": MultipleChoiceAnswer(
                    player_id="p1",
                    suggestion_id="correct",
                    answered_at=12.0,
                    is_correct=True,
                    points=1000,
                )
            },
        ),
        track_uri="library://track/1",
        image_url="https://example.test/artwork.jpg",
        duration=180.0,
        started_at=10.0,
        ended_at=12.0,
        auto_advance_at=42.0,
    )

    serialized = game_round.to_dict()
    restored = MusicQuizRound.from_dict(serialized)

    assert serialized == {
        "round_index": 0,
        "answer_label": "Daft Punk - One More Time",
        "answer_state": {
            "answer_type": "multiple_choice",
            "suggestions": [
                {
                    "suggestion_id": "correct",
                    "label": "Daft Punk - One More Time",
                    "uri": "library://track/1",
                    "is_correct": True,
                }
            ],
            "answers": {
                "p1": {
                    "player_id": "p1",
                    "suggestion_id": "correct",
                    "answered_at": 12.0,
                    "is_correct": True,
                    "points": 1000,
                }
            },
        },
        "track_uri": "library://track/1",
        "question": None,
        "image_url": "https://example.test/artwork.jpg",
        "duration": 180.0,
        "started_at": 10.0,
        "ended_at": 12.0,
        "auto_advance_at": 42.0,
    }
    assert restored == game_round
    assert isinstance(restored.answer_state, MultipleChoiceRoundState)
    assert restored.answer_state.answer_type is MusicQuizAnswerType.MULTIPLE_CHOICE


def test_player_presence_is_not_serialized() -> None:
    """Keep internal presence timestamps out of serialized player state."""
    player = MusicQuizPlayer(
        player_id="private",
        name="Alice",
        joined_at=10.0,
        active_from_round=0,
        last_seen=20.0,
    )

    assert player.to_dict() == {
        "player_id": "private",
        "name": "Alice",
        "joined_at": 10.0,
        "active_from_round": 0,
        "score": 0,
        "ready": False,
    }


@pytest.mark.parametrize(
    "answer_state",
    [
        {
            "suggestions": [],
            "answers": {},
        },
        {
            "answer_type": "timeline",
            "suggestions": [],
            "answers": {},
        },
        {
            "answer_type": "multiple_choice",
            "suggestions": [],
            "answers": {},
            "extra": True,
        },
        {
            "answer_type": "multiple_choice",
            "suggestions": [
                {
                    "suggestion_id": "correct",
                    "label": "Correct",
                    "uri": None,
                    "is_correct": True,
                    "extra": True,
                }
            ],
            "answers": {},
        },
        {
            "answer_type": "multiple_choice",
            "suggestions": [],
            "answers": {
                "p1": {
                    "player_id": "p1",
                    "suggestion_id": "correct",
                    "answered_at": 12.0,
                    "is_correct": True,
                    "points": 1000,
                    "extra": True,
                }
            },
        },
    ],
)
def test_round_answer_state_rejects_invalid_nested_data(
    answer_state: dict[str, object],
) -> None:
    """Reject missing, unknown, and extra nested answer-state data."""
    with pytest.raises(InvalidFieldValue):
        MusicQuizRound.from_dict(
            {
                "round_index": 0,
                "answer_label": "Correct",
                "answer_state": answer_state,
            }
        )


def test_round_rejects_extra_common_data() -> None:
    """Reject unknown fields on the common persisted round."""
    with pytest.raises(ExtraKeysError):
        MusicQuizRound.from_dict(
            {
                "round_index": 0,
                "answer_label": "Correct",
                "answer_state": {
                    "answer_type": "multiple_choice",
                    "suggestions": [],
                    "answers": {},
                },
                "extra": True,
            }
        )


def test_timeline_round_state_round_trips_with_strict_nested_variants() -> None:
    """Round-trip timeline state with typed bonus definitions, answers and results."""
    state = TimelineRoundState(
        placement_snapshot=[
            TimelineEntry(
                entry_id="anchor",
                release_year=1990,
                title="Anchor",
                artist="Artist",
                track_uri="library://track/anchor",
                image_url=None,
                is_anchor=True,
            )
        ],
        candidate=TimelineCandidate(
            entry=TimelineEntry(
                entry_id="current",
                release_year=2000,
                title="Current",
                artist="Artist",
                track_uri="library://track/current",
                image_url="https://img/current",
            ),
            artist_answers=["Artist", "Artist Alias"],
            title_answers=["Current"],
        ),
        bonus_definitions=[
            TimelineFreeTextBonusDefinition(bonus_type=TimelineBonusType.ARTIST),
            TimelineMultipleChoiceBonusDefinition(
                bonus_type=TimelineBonusType.TITLE,
                options=[
                    TimelineBonusOption("correct", "Current", True),
                    TimelineBonusOption("wrong-a", "Wrong A"),
                    TimelineBonusOption("wrong-b", "Wrong B"),
                    TimelineBonusOption("wrong-c", "Wrong C"),
                ],
            ),
        ],
        placements={
            "p1": TimelinePlacementAnswer(
                previous_entry_id="anchor",
                next_entry_id=None,
                answered_at=12,
            )
        },
        bonus_answers={
            "p1": [
                TimelineChoiceBonusAnswer(
                    bonus_type=TimelineBonusType.TITLE,
                    submitted_at=13,
                    option_id="correct",
                )
            ]
        },
        finished_at={"p1": 14},
        results={
            "p1": TimelineAnswerResult(
                placement=TimelinePlacementResult("anchor", None, True, 1000),
                bonuses=[TimelineBonusResult(TimelineBonusType.TITLE, True, 250)],
            )
        },
        revealed=True,
    )
    game_round = MusicQuizRound(
        round_index=0,
        answer_label="Artist - Current",
        answer_state=state,
        track_uri="library://track/current",
    )

    restored = MusicQuizRound.from_dict(game_round.to_dict())

    assert restored == game_round
    assert isinstance(restored.answer_state, TimelineRoundState)
    assert isinstance(restored.answer_state.candidate, TimelineCandidate)
    assert isinstance(
        restored.answer_state.bonus_definitions[0],
        TimelineFreeTextBonusDefinition,
    )
    assert isinstance(
        restored.answer_state.bonus_definitions[1],
        TimelineMultipleChoiceBonusDefinition,
    )
    assert isinstance(restored.answer_state.bonus_answers["p1"][0], TimelineChoiceBonusAnswer)


@pytest.mark.parametrize(
    "field_path",
    [
        ("placement_snapshot", 0),
        ("candidate", None),
        ("candidate_entry", None),
        ("bonus_definitions", 0),
        ("bonus_answers", 0),
        ("results", None),
    ],
)
def test_timeline_round_state_rejects_extra_nested_data(
    field_path: tuple[str, int | None],
) -> None:
    """Reject extra keys in every nested timeline model family."""
    state = TimelineRoundState(
        placement_snapshot=[
            TimelineEntry(
                "anchor",
                1990,
                "Anchor",
                "Artist",
                "library://track/anchor",
                None,
                True,
            )
        ],
        candidate=TimelineCandidate(
            entry=TimelineEntry(
                "current",
                2000,
                "Current",
                "Artist",
                "library://track/current",
                None,
            ),
            artist_answers=["Artist"],
            title_answers=["Current"],
        ),
        bonus_definitions=[TimelineFreeTextBonusDefinition(bonus_type=TimelineBonusType.ARTIST)],
        bonus_answers={
            "p1": [
                TimelineChoiceBonusAnswer(
                    bonus_type=TimelineBonusType.TITLE,
                    submitted_at=13,
                    option_id="correct",
                )
            ]
        },
        results={
            "p1": TimelineAnswerResult(
                placement=TimelinePlacementResult("anchor", None, True, 1000)
            )
        },
    ).to_dict()
    field_name, index = field_path
    target: dict[str, object]
    if field_name == "placement_snapshot":
        target = state[field_name][index]
    elif field_name == "candidate":
        target = state[field_name]
    elif field_name == "candidate_entry":
        target = state["candidate"]["entry"]
    elif field_name == "bonus_definitions":
        target = state[field_name][index]
    elif field_name == "bonus_answers":
        target = state[field_name]["p1"][index]
    else:
        target = state[field_name]["p1"]["placement"]
    target["extra"] = True

    with pytest.raises(InvalidFieldValue):
        MusicQuizRound.from_dict(
            {
                "round_index": 0,
                "answer_label": "Artist - Current",
                "answer_state": state,
            }
        )


def test_timeline_round_state_rejects_extra_top_level_data() -> None:
    """Reject unknown fields on the discriminated timeline round state."""
    state = TimelineRoundState(
        placement_snapshot=[
            TimelineEntry(
                "anchor",
                1990,
                "Anchor",
                "Artist",
                "library://track/anchor",
                None,
                True,
            )
        ],
        candidate=TimelineCandidate(
            entry=TimelineEntry(
                "current",
                2000,
                "Current",
                "Artist",
                "library://track/current",
                None,
            ),
            artist_answers=["Artist"],
            title_answers=["Current"],
        ),
    ).to_dict()
    state["extra"] = True

    with pytest.raises(InvalidFieldValue):
        MusicQuizRound.from_dict(
            {
                "round_index": 0,
                "answer_label": "Artist - Current",
                "answer_state": state,
            }
        )


def test_round_deserializes_without_importing_answer_strategy() -> None:
    """Deserialize the model module without answer-strategy registration."""
    models_path = Path(inspect.getfile(MusicQuizRound))
    script = f"""
import importlib.util
import sys

module_name = "_music_quiz_models_clean_import"
spec = importlib.util.spec_from_file_location(module_name, {str(models_path)!r})
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[module_name] = module
spec.loader.exec_module(module)
assert "music_assistant.providers.music_quiz.answer_types.multiple_choice" not in sys.modules
game_round = module.MusicQuizRound.from_dict(
    {{
        "round_index": 0,
        "answer_label": "Correct",
        "answer_state": {{
            "answer_type": "multiple_choice",
            "suggestions": [],
            "answers": {{}},
        }},
    }}
)
assert isinstance(game_round.answer_state, module.MultipleChoiceRoundState)
"""

    subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
