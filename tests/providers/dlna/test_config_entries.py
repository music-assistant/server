"""Tests for the config entries a DLNA player offers."""

from __future__ import annotations

from music_assistant.constants import CONF_REQUIRES_FIXED_CONTENT_LENGTH
from music_assistant.providers.dlna.player import DLNAPlayer


async def test_fixed_content_length_is_off_by_default_and_adjustable() -> None:
    """A renderer that stops or loops without a fixed length can be switched over by the user."""
    entries = await DLNAPlayer.__new__(DLNAPlayer).get_config_entries()

    entry = next(entry for entry in entries if entry.key == CONF_REQUIRES_FIXED_CONTENT_LENGTH)
    assert entry.default_value is False
    assert not entry.hidden
    assert entry.advanced
