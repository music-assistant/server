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
    MusicQuizRound,
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
    }
    assert restored == game_round
    assert isinstance(restored.answer_state, MultipleChoiceRoundState)
    assert restored.answer_state.answer_type is MusicQuizAnswerType.MULTIPLE_CHOICE


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
