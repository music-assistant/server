"""Tests for Music Quiz scoring helpers."""

from __future__ import annotations

from music_assistant.providers.music_quiz.scoring import (
    calculate_linear_scores,
    calculate_time_score,
)


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


def test_calculate_time_score_descends_from_max_to_min() -> None:
    """Award more points for earlier correct answers, never less than the minimum."""
    assert calculate_time_score(0, 30) == 1000
    assert calculate_time_score(15, 30) == 550
    assert calculate_time_score(30, 30) == 100
    assert calculate_time_score(45, 30) == 100
    assert calculate_time_score(-2, 30) == 1000


def test_calculate_time_score_without_window_awards_max_points() -> None:
    """Award max points when the answer window is empty."""
    assert calculate_time_score(5, 0) == 1000
