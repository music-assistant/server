"""Unit tests for Apple Music ID helpers."""

from typing import Any

import pytest

from music_assistant.providers.apple_music import AppleMusicProvider


@pytest.fixture
def provider() -> AppleMusicProvider:
    """Return a minimal AppleMusicProvider instance for helper testing."""
    # Avoid Provider __init__ complexity; is_library_id does not use instance state.
    return AppleMusicProvider.__new__(AppleMusicProvider)


def test_is_library_id_accepts_library_prefixes(provider: AppleMusicProvider) -> None:
    """Confirm expected library prefixes are accepted."""
    for prefix in ("a.", "i.", "l.", "p."):
        assert provider.is_library_id(f"{prefix}ABC123")


def test_is_library_id_rejects_pl_u_prefix(provider: AppleMusicProvider) -> None:
    """Reject the invalid pl.u- prefix."""
    assert not provider.is_library_id("pl.u-ABC123")
    assert not provider.is_library_id("pl.u-1")


def test_is_library_id_rejects_invalid_values(provider: AppleMusicProvider) -> None:
    """Reject malformed values and non-string inputs."""
    for value in ("", "a.", "x.123", "pl.123", "p.123-456"):
        assert not provider.is_library_id(value)
    invalid_non_str: list[Any] = [None, 123, 12.3]
    for value in invalid_non_str:
        assert not provider.is_library_id(value)
