"""Tests for the config entries a Samsung WAM player offers."""

from __future__ import annotations

from music_assistant.constants import CONF_REQUIRES_FIXED_CONTENT_LENGTH
from music_assistant.providers.samsung_wam.player import WamPlayer


async def test_fixed_content_length_is_on_by_default_and_adjustable() -> None:
    """WAM speakers get a fixed content length unless the user switches it off."""
    entries = await WamPlayer.__new__(WamPlayer).get_config_entries()

    entry = next(entry for entry in entries if entry.key == CONF_REQUIRES_FIXED_CONTENT_LENGTH)
    assert entry.default_value is True
    assert not entry.hidden
    assert entry.advanced
