"""Tests for the Music Quiz multiple-choice answer strategy."""

from __future__ import annotations

import pytest

from music_assistant.providers.music_quiz.answer_types.multiple_choice import (
    MultipleChoiceAnswerType,
    MultipleChoiceSubmission,
)
from music_assistant.providers.music_quiz.errors import MusicQuizInvalidAnswerError
from music_assistant.providers.music_quiz.models import (
    MusicQuizAnswer,
    MusicQuizRound,
    MusicQuizSuggestion,
)

ANSWER_TYPE = MultipleChoiceAnswerType()


def _round() -> MusicQuizRound:
    """Return a multiple-choice round."""
    return MusicQuizRound(
        round_index=0,
        track_uri="library://track/1",
        answer_label="Daft Punk - One More Time",
        suggestions=[
            MusicQuizSuggestion(
                suggestion_id="correct",
                label="Daft Punk - One More Time",
                is_correct=True,
            ),
            MusicQuizSuggestion(
                suggestion_id="wrong",
                label="Justice - D.A.N.C.E.",
            ),
        ],
        image_url="https://example.test/artwork.jpg",
        duration=180,
        started_at=10,
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
    game_round = _round()

    hidden = ANSWER_TYPE.serialize_round(game_round, revealed=False)
    revealed = ANSWER_TYPE.serialize_round(game_round, revealed=True)

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
    game_round = _round()
    game_round.answers["p1"] = MusicQuizAnswer(
        player_id="p1",
        suggestion_id="correct",
        answered_at=12,
        is_correct=True,
        points=1000,
    )

    assert ANSWER_TYPE.serialize_public_player(game_round, "p1", revealed=False) == {
        "answered": True
    }
    assert ANSWER_TYPE.serialize_personal_player(game_round, "p1", revealed=False) == {
        "answer": {
            "suggestion_id": "correct",
            "answered_at": 12,
        }
    }
    assert ANSWER_TYPE.serialize_public_player(game_round, "p1", revealed=True) == {
        "answered": True,
        "last_answer": {
            "suggestion_id": "correct",
            "correct": True,
            "points": 1000,
        },
    }
    assert ANSWER_TYPE.serialize_personal_player(game_round, "p1", revealed=True) == {
        "answer": {
            "suggestion_id": "correct",
            "answered_at": 12,
            "correct": True,
            "points": 1000,
        }
    }
