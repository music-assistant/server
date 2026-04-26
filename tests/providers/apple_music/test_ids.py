"""Unit tests for Apple Music ID helpers."""

from typing import Any

from music_assistant.providers.apple_music.helpers.utils import is_library_id


def test_is_library_id_accepts_library_prefixes() -> None:
    """Confirm expected library prefixes are accepted."""
    for prefix in ("a.", "i.", "l.", "p."):
        assert is_library_id(f"{prefix}ABC123")


def test_is_library_id_rejects_pl_u_prefix() -> None:
    """Reject the invalid pl.u- prefix."""
    assert not is_library_id("pl.u-ABC123")
    assert not is_library_id("pl.u-1")


def test_is_library_id_rejects_invalid_values() -> None:
    """Reject malformed values and non-string inputs."""
    for value in ("", "a.", "x.123", "pl.123", "p.123-456"):
        assert not is_library_id(value)
    invalid_non_str: list[Any] = [None, 123, 12.3]
    for value in invalid_non_str:
        assert not is_library_id(value)
