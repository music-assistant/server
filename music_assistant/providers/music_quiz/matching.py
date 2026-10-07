"""Free-text matching helpers for the Music Quiz provider."""

from __future__ import annotations

from music_assistant_models.helpers import create_safe_string

from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.util import parse_title_and_version


def compare_free_text_answer(submitted: str, expected: str) -> bool:
    """
    Return whether a free-text answer conservatively matches the expected value.

    :param submitted: Player-provided answer.
    :param expected: Correct answer value.
    """
    submitted_title, _ = parse_title_and_version(submitted, strip_for_search=True)
    expected_title, _ = parse_title_and_version(expected, strip_for_search=True)
    if compare_strings(submitted_title, expected_title):
        return True

    submitted_safe = create_safe_string(
        submitted_title.replace(" & ", " and "),
        replace_space=True,
    )
    expected_safe = create_safe_string(
        expected_title.replace(" & ", " and "),
        replace_space=True,
    )
    if not submitted_safe or not expected_safe:
        return False
    shortest_length = min(len(submitted_safe), len(expected_safe))
    max_distance = 0 if shortest_length < 5 else 1 if shortest_length < 13 else 2
    return _within_edit_distance(submitted_safe, expected_safe, max_distance)


def _within_edit_distance(first: str, second: str, max_distance: int) -> bool:
    """Return whether two strings are within a bounded Levenshtein distance."""
    if abs(len(first) - len(second)) > max_distance:
        return False
    previous_row = list(range(len(second) + 1))
    for first_index, first_char in enumerate(first, start=1):
        current_row = [first_index]
        for second_index, second_char in enumerate(second, start=1):
            current_row.append(
                min(
                    current_row[-1] + 1,
                    previous_row[second_index] + 1,
                    previous_row[second_index - 1] + (first_char != second_char),
                )
            )
        if min(current_row) > max_distance:
            return False
        previous_row = current_row
    return previous_row[-1] <= max_distance
