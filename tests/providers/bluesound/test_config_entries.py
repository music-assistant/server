"""Tests for the config entries a Bluesound player offers."""

from __future__ import annotations

from music_assistant.constants import CONF_REQUIRES_FIXED_CONTENT_LENGTH
from music_assistant.providers.bluesound.player import BluesoundPlayer


async def test_fixed_content_length_is_enforced() -> None:
    """BluOS loops the audio without a fixed length, so it is always on and not offered."""
    entries = await BluesoundPlayer.__new__(BluesoundPlayer).get_config_entries()

    entry = next(entry for entry in entries if entry.key == CONF_REQUIRES_FIXED_CONTENT_LENGTH)
    assert entry.default_value is True
    assert entry.hidden
