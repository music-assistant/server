"""Tests for the MSX Bridge Provider entry point."""

from __future__ import annotations

from unittest.mock import Mock

from music_assistant_models.enums import ConfigEntryType

from music_assistant.providers.msx_bridge import get_config_entries, setup
from music_assistant.providers.msx_bridge.constants import (
    CONF_HTTP_PORT,
    CONF_OUTPUT_FORMAT,
    DEFAULT_HTTP_PORT,
    DEFAULT_OUTPUT_FORMAT,
)
from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider


async def test_setup_returns_provider(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """setup() should return an MSXBridgeProvider instance."""
    result = await setup(mass_mock, manifest_mock, config_mock)
    assert isinstance(result, MSXBridgeProvider)


async def test_get_config_entries(mass_mock: Mock) -> None:
    """get_config_entries() should return core config entries."""
    entries = await get_config_entries(mass_mock)
    assert len(entries) >= 2  # at least http_port, output_format

    port_entry = entries[0]
    assert port_entry.key == CONF_HTTP_PORT
    assert port_entry.type == ConfigEntryType.INTEGER
    assert port_entry.default_value == str(DEFAULT_HTTP_PORT)

    format_entry = entries[1]
    assert format_entry.key == CONF_OUTPUT_FORMAT
    assert format_entry.type == ConfigEntryType.STRING
    assert format_entry.default_value == DEFAULT_OUTPUT_FORMAT

    # Optional: show_stop_notification if present
    if len(entries) >= 4:
        show_notification_entry = entries[3]
        assert show_notification_entry.key == "show_stop_notification"
        assert show_notification_entry.type == ConfigEntryType.BOOLEAN
        assert show_notification_entry.default_value is False
