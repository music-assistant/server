"""Free-text matching helpers for the Music Quiz provider."""

from __future__ import annotations

from music_assistant_models.helpers import create_safe_string

from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.providers.music_quiz.models import TitleRevealTypoTolerance

# (exclusive upper letter count, typos allowed at normal tolerance), shortest first
TYPO_BUDGET_STEPS: tuple[tuple[int, int], ...] = ((5, 0), (9, 1), (15, 2), (23, 3))
MAX_TYPO_BUDGET = 4
MIN_TYPO_LETTER_COUNT = 3
TYPO_TOLERANCE_OFFSETS: dict[TitleRevealTypoTolerance, int] = {
    TitleRevealTypoTolerance.STRICT: -1,
    TitleRevealTypoTolerance.NORMAL: 0,
    TitleRevealTypoTolerance.LENIENT: 1,
}


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


def normalize_guess(text: str) -> str:
    """
    Return the comparable form of a free-text guess or accepted answer.

    :param text: Text to normalize.
    """
    return create_safe_string(text.replace("&", " and "), replace_space=True)


def allowed_typos(letter_count: int, tolerance: TitleRevealTypoTolerance) -> int:
    """
    Return how many typos a guess for an answer of the given length may contain.

    :param letter_count: Number of comparable characters in the answer.
    :param tolerance: Typo tolerance selected for the game.
    """
    # even a lenient game cannot accept a typo in a one- or two-letter answer
    if letter_count < MIN_TYPO_LETTER_COUNT:
        return 0
    budget = next(
        (typos for upper_bound, typos in TYPO_BUDGET_STEPS if letter_count < upper_bound),
        MAX_TYPO_BUDGET,
    )
    return max(budget + TYPO_TOLERANCE_OFFSETS[tolerance], 0)


def compare_hint_guess(
    submitted: str, accepted_answers: list[str], max_typos: int
) -> tuple[bool, bool]:
    """
    Return whether a guess matches any accepted answer, and whether it narrowly missed.

    :param submitted: Player-provided guess.
    :param accepted_answers: Normalized accepted answers (see normalize_guess).
    :param max_typos: Number of typos the guess may contain.
    :return: Tuple of (is_correct, is_close).
    """
    submitted_safe = normalize_guess(submitted)
    if not submitted_safe:
        return False, False
    if any(_within_edit_distance(submitted_safe, answer, max_typos) for answer in accepted_answers):
        return True, False
    is_close = any(
        _within_edit_distance(submitted_safe, answer, max_typos + 1) for answer in accepted_answers
    )
    return False, is_close


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
