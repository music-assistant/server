"""Scoring helpers for the Music Quiz provider."""

from __future__ import annotations


def calculate_linear_scores(
    correct_answer_order: list[str], max_points: int = 1000
) -> dict[str, int]:
    """
    Calculate linearly descending scores for correct answers.

    :param correct_answer_order: Player IDs ordered by correct answer time.
    :param max_points: Points awarded to the first correct answer.
    """
    correct_count = len(correct_answer_order)
    if correct_count == 0:
        return {}
    return {
        player_id: round(max_points * (correct_count - index) / correct_count)
        for index, player_id in enumerate(correct_answer_order)
    }
