"""Tests for Music Quiz free-text answer matching."""

from __future__ import annotations

import pytest

from music_assistant.providers.music_quiz.matching import compare_free_text_answer


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
