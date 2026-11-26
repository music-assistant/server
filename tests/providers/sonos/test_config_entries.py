"""Tests for the config entries a Sonos player offers."""

from __future__ import annotations

from music_assistant.constants import CONF_REQUIRES_FIXED_CONTENT_LENGTH
from music_assistant.models.player import DeviceInfo
from music_assistant.providers.sonos.player import SonosPlayer


async def test_no_fixed_content_length_entry() -> None:
    """Sonos plays fine without a fixed length, so the setting is not offered."""
    player = SonosPlayer.__new__(SonosPlayer)
    player._attr_device_info = DeviceInfo(model="Era 300", manufacturer="Sonos")

    entries = await player.get_config_entries()

    assert CONF_REQUIRES_FIXED_CONTENT_LENGTH not in {entry.key for entry in entries}
