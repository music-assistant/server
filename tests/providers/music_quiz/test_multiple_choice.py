"""Tests for the Music Quiz multiple-choice answer strategy."""

from __future__ import annotations

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.music_quiz.answer_types.multiple_choice import (
    MultipleChoiceAnswerType,
    MultipleChoiceSubmission,
)
from music_assistant.providers.music_quiz.errors import MusicQuizInvalidAnswerError
from music_assistant.providers.music_quiz.models import (
    MultipleChoiceAnswer,
    MultipleChoiceRoundState,
    MultipleChoiceSuggestion,
    MusicQuizAnswerType,
    QuizRoundAnswerState,
)

ANSWER_TYPE = MultipleChoiceAnswerType()


def _state() -> MultipleChoiceRoundState:
    """Return multiple-choice round state."""
    return MultipleChoiceRoundState(
        suggestions=[
            MultipleChoiceSuggestion(
                suggestion_id="correct",
                label="Daft Punk - One More Time",
                is_correct=True,
            ),
            MultipleChoiceSuggestion(
                suggestion_id="wrong",
                label="Justice - D.A.N.C.E.",
            ),
        ],
    )


def test_parse_submission_returns_typed_request() -> None:
    """Parse the strict wire payload into a typed multiple-choice submission."""
    submission = ANSWER_TYPE.parse_submission(
        {
            "answer_type": "multiple_choice",
            "suggestion_id": "correct",
        }
    )

    assert submission == MultipleChoiceSubmission(suggestion_id="correct")


@pytest.mark.parametrize(
    "payload",
    [
        {"suggestion_id": "correct"},
        {"answer_type": "multiple_choice"},
        {"answer_type": "timeline", "suggestion_id": "correct"},
        {"answer_type": 1, "suggestion_id": "correct"},
        {"answer_type": "multiple_choice", "suggestion_id": 1},
        {"answer_type": "multiple_choice", "suggestion_id": ""},
        {
            "answer_type": "multiple_choice",
            "suggestion_id": "correct",
            "extra": True,
        },
    ],
)
def test_parse_submission_rejects_malformed_payload(payload: dict[str, object]) -> None:
    """Reject missing, mismatched, incorrectly typed and extra fields."""
    with pytest.raises(MusicQuizInvalidAnswerError):
        ANSWER_TYPE.parse_submission(payload)


def test_round_serialization_redacts_answer_until_reveal() -> None:
    """Expose suggestion choices without revealing which one is correct."""
    state = _state()

    hidden = ANSWER_TYPE.serialize_round(state, revealed=False)
    revealed = ANSWER_TYPE.serialize_round(state, revealed=True)

    assert hidden == {
        "suggestions": [
            {"suggestion_id": "correct", "label": "Daft Punk - One More Time"},
            {"suggestion_id": "wrong", "label": "Justice - D.A.N.C.E."},
        ]
    }
    assert "correct_suggestion_id" not in hidden
    assert revealed["correct_suggestion_id"] == "correct"
    assert "answer_label" not in revealed


def test_player_serialization_separates_public_and_personal_state() -> None:
    """Keep a locked answer private and its correctness hidden before reveal."""
    state = _state()
    state.answers["p1"] = MultipleChoiceAnswer(
        player_id="p1",
        suggestion_id="correct",
        answered_at=12,
        is_correct=True,
        points=1000,
    )

    assert ANSWER_TYPE.serialize_public_player(state, "p1", revealed=False) == {"answered": True}
    assert ANSWER_TYPE.serialize_personal_player(state, "p1", revealed=False) == {
        "answer": {
            "suggestion_id": "correct",
            "answered_at": 12,
        }
    }
    assert ANSWER_TYPE.serialize_public_player(state, "p1", revealed=True) == {
        "answered": True,
        "last_answer": {
            "suggestion_id": "correct",
            "correct": True,
            "points": 1000,
        },
    }
    assert ANSWER_TYPE.serialize_personal_player(state, "p1", revealed=True) == {
        "answer": {
            "suggestion_id": "correct",
            "answered_at": 12,
            "correct": True,
            "points": 1000,
        }
    }


def test_host_serialization_preserves_full_flat_answer_state() -> None:
    """Serialize full multiple-choice state for the host round payload."""
    state = _state()
    state.answers["p1"] = MultipleChoiceAnswer(
        player_id="p1",
        suggestion_id="correct",
        answered_at=12,
        is_correct=True,
        points=1000,
    )

    assert ANSWER_TYPE.serialize_host_round(state) == {
        "suggestions": [
            {
                "suggestion_id": "correct",
                "label": "Daft Punk - One More Time",
                "uri": None,
                "is_correct": True,
            },
            {
                "suggestion_id": "wrong",
                "label": "Justice - D.A.N.C.E.",
                "uri": None,
                "is_correct": False,
            },
        ],
        "answers": {
            "p1": {
                "player_id": "p1",
                "suggestion_id": "correct",
                "answered_at": 12,
                "is_correct": True,
                "points": 1000,
            }
        },
    }


def test_strategy_rejects_matching_discriminator_on_wrong_state_class() -> None:
    """Reject state whose discriminator matches but concrete model does not."""
    state = QuizRoundAnswerState(answer_type=MusicQuizAnswerType.MULTIPLE_CHOICE)

    with pytest.raises(InvalidDataError, match="does not match"):
        ANSWER_TYPE.serialize_round(state, revealed=False)
