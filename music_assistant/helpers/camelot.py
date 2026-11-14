"""Helpers for the Camelot wheel notation of musical keys."""

from __future__ import annotations


def camelot_code(key: str | None, mode: str | None) -> tuple[int, bool] | None:
    """Return (wheel number, is_major) or None for unknown input."""
    if not key or mode not in ("major", "minor"):
        return None
    key = _FLAT_TO_SHARP.get(key, key)
    table = _CAMELOT_MAJOR if mode == "major" else _CAMELOT_MINOR
    num = table.get(key)
    return (num, mode == "major") if num else None


def camelot_key(key: str | None, mode: str | None) -> str | None:
    """Return the Camelot code of a key (e.g. "8A" for A minor, "8B" for C major) or None."""
    code = camelot_code(key, mode)
    if code is None:
        return None
    num, is_major = code
    return f"{num}{'B' if is_major else 'A'}"


_FLAT_TO_SHARP = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}
# Camelot wheel numbers; relative major/minor share a number (Am=8A, C=8B)
_CAMELOT_MAJOR = {
    "C": 8,
    "G": 9,
    "D": 10,
    "A": 11,
    "E": 12,
    "B": 1,
    "F#": 2,
    "C#": 3,
    "G#": 4,
    "D#": 5,
    "A#": 6,
    "F": 7,
}
_CAMELOT_MINOR = {
    "A": 8,
    "E": 9,
    "B": 10,
    "F#": 11,
    "C#": 12,
    "G#": 1,
    "D#": 2,
    "A#": 3,
    "F": 4,
    "C": 5,
    "G": 6,
    "D": 7,
}
