"""Tests for Music Quiz scoring helpers."""

from __future__ import annotations

from music_assistant.providers.music_quiz.scoring import calculate_linear_scores


def test_calculate_linear_scores_without_correct_answers() -> None:
    """Return no scores when no player answered correctly."""
    assert calculate_linear_scores([]) == {}


def test_calculate_linear_scores_single_correct_answer() -> None:
    """Award max points to the only correct answer."""
    assert calculate_linear_scores(["player_1"]) == {"player_1": 1000}


def test_calculate_linear_scores_multiple_correct_answers() -> None:
    """Award linearly descending scores in answer order."""
    assert calculate_linear_scores(["player_1", "player_2", "player_3"]) == {
        "player_1": 1000,
        "player_2": 667,
        "player_3": 333,
    }


def test_calculate_linear_scores_four_correct_answers() -> None:
    """Award deterministic scores for four correct answers."""
    assert calculate_linear_scores(["p1", "p2", "p3", "p4"]) == {
        "p1": 1000,
        "p2": 750,
        "p3": 500,
        "p4": 250,
    }
