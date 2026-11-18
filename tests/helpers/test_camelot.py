"""Tests for the Camelot wheel helpers."""

from __future__ import annotations

import pytest

from music_assistant.helpers.camelot import camelot_key


@pytest.mark.parametrize(
    ("key", "mode", "expected"),
    [
        ("A", "minor", "8A"),
        ("C", "major", "8B"),
        ("Eb", "major", "5B"),
        ("F#", "minor", "11A"),
        ("C", "dorian", None),
        ("C", None, None),
        (None, "major", None),
    ],
)
def test_camelot_key(key: str | None, mode: str | None, expected: str | None) -> None:
    """Keys map to their Camelot code, unknown input yields None."""
    assert camelot_key(key, mode) == expected
