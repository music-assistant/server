"""Tests for Music Quiz free-text answer matching."""

from __future__ import annotations

import pytest

from music_assistant.providers.music_quiz.matching import (
    allowed_typos,
    compare_free_text_answer,
    compare_hint_guess,
    normalize_guess,
)
from music_assistant.providers.music_quiz.models import TitleRevealTypoTolerance


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        ("BEYONCE!", "Beyoncé"),
        ("Around the World", "Around the World (Radio Edit)"),
        ("Around the World", "Around the World - Remastered 2019"),
        ("Daft Punk", "Daft Punk feat. Pharrell Williams"),
        ("Daft Punk", "Daft Punk featuring Pharrell Williams"),
        ("Hall & Oates", "Hall and Oates"),
        ("Beatls", "Beatles"),
        ("Bohemian Rapsody", "Bohemian Rhapsody"),
    ],
)
def test_free_text_match_accepts_normalization_suffixes_and_bounded_typos(
    submitted: str,
    expected: str,
) -> None:
    """Accept conservative spelling variation after answer-specific normalization."""
    assert compare_free_text_answer(submitted, expected) is True


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        ("Sung", "Song"),
        ("Air", "Ari"),
        ("Beat", "Beetles"),
        ("Bohemxxn Rapsodi", "Bohemian Rhapsody"),
        ("Another Brick in the Wall Part One", "Another Brick in the Wall Part Two"),
        ("Completely Different", "Bohemian Rhapsody"),
        ("", "Bohemian Rhapsody"),
    ],
)
def test_free_text_match_rejects_short_or_excessively_different_answers(
    submitted: str,
    expected: str,
) -> None:
    """Reject risky short-word edits and answers beyond the bounded tolerance."""
    assert compare_free_text_answer(submitted, expected) is False


@pytest.mark.parametrize(
    ("letter_count", "strict", "normal", "lenient"),
    [
        (2, 0, 0, 0),
        (4, 0, 0, 1),
        (5, 0, 1, 2),
        (8, 0, 1, 2),
        (9, 1, 2, 3),
        (14, 1, 2, 3),
        (15, 2, 3, 4),
        (22, 2, 3, 4),
        (23, 3, 4, 5),
        (60, 3, 4, 5),
    ],
)
def test_allowed_typos_grow_with_answer_length(
    letter_count: int, strict: int, normal: int, lenient: int
) -> None:
    """Allow almost no typos in short titles and more in long ones, shifted by tolerance."""
    assert allowed_typos(letter_count, TitleRevealTypoTolerance.STRICT) == strict
    assert allowed_typos(letter_count, TitleRevealTypoTolerance.NORMAL) == normal
    assert allowed_typos(letter_count, TitleRevealTypoTolerance.LENIENT) == lenient


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        ("Beyonce", "Beyoncé"),
        ("don't fear the reaper", "(Don't Fear) The Reaper"),
        ("Rock and Roll", "Rock & Roll"),
        ("MR BRIGHTSIDE", "Mr. Brightside"),
        ("Strasse", "Straße"),
    ],
)
def test_normalize_guess_ignores_case_accents_and_punctuation(
    submitted: str, expected: str
) -> None:
    """Compare guesses without case, accents, punctuation or spacing."""
    assert normalize_guess(submitted) == normalize_guess(expected)


def test_compare_hint_guess_reports_correct_close_and_wrong_guesses() -> None:
    """Accept guesses within the typo budget and flag guesses one edit beyond it."""
    accepted = [normalize_guess("Bohemian Rhapsody")]

    assert compare_hint_guess("Bohemian Rapsody", accepted, 1) == (True, False)
    assert compare_hint_guess("Bohemian Rapsdy", accepted, 1) == (False, True)
    assert compare_hint_guess("Killer Queen", accepted, 1) == (False, False)
    assert compare_hint_guess("!!!", accepted, 1) == (False, False)


def test_compare_hint_guess_accepts_any_accepted_answer() -> None:
    """Accept the shown title as well as its stripped search form."""
    accepted = [normalize_guess("(Don't Fear) The Reaper"), normalize_guess("The Reaper")]

    assert compare_hint_guess("the reaper", accepted, 0) == (True, False)
