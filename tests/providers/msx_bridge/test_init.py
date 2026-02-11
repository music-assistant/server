"""Tests for the MSX Bridge Provider entry point."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

from music_assistant_models.enums import ConfigEntryType, PlayerFeature, ProviderFeature

from music_assistant.providers.msx_bridge import get_config_entries, setup
from music_assistant.providers.msx_bridge.constants import (
    CONF_ENABLE_GROUPING,
    CONF_HTTP_PORT,
    CONF_OUTPUT_FORMAT,
    DEFAULT_HTTP_PORT,
    DEFAULT_OUTPUT_FORMAT,
)
from music_assistant.providers.msx_bridge.player import MSXPlayer
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

    # Verify enable_player_grouping entry exists
    grouping_entry = next((e for e in entries if e.key == CONF_ENABLE_GROUPING), None)
    assert grouping_entry is not None
    assert grouping_entry.type == ConfigEntryType.BOOLEAN
    assert grouping_entry.default_value is True


async def test_setup_without_sync_players(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """setup() with grouping disabled should not include SYNC_PLAYERS."""
    # Override config to disable grouping
    config_mock.get_value = Mock(
        side_effect=lambda key, default=None: {
            "http_port": 8099,
            "output_format": "mp3",
            "log_level": "GLOBAL",
            "enable_player_grouping": False,
        }.get(key, default)
    )
    result = await setup(mass_mock, manifest_mock, config_mock)
    assert isinstance(result, MSXBridgeProvider)
    assert ProviderFeature.SYNC_PLAYERS not in result.supported_features


async def test_setup_with_sync_players(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """setup() with grouping enabled should include SYNC_PLAYERS."""
    result = await setup(mass_mock, manifest_mock, config_mock)
    assert isinstance(result, MSXBridgeProvider)
    assert ProviderFeature.SYNC_PLAYERS in result.supported_features


def test_player_grouping_enabled(provider: Any) -> None:
    """MSXPlayer with grouping_enabled=True should have SET_MEMBERS."""
    p = MSXPlayer(provider, "msx_g", name="Group TV", output_format="mp3", grouping_enabled=True)
    p.update_state = Mock()  # type: ignore[misc,method-assign]
    assert PlayerFeature.SET_MEMBERS in p._attr_supported_features
    assert len(p._attr_can_group_with) > 0


def test_player_grouping_disabled(provider: Any) -> None:
    """MSXPlayer with grouping_enabled=False should NOT have SET_MEMBERS."""
    p = MSXPlayer(provider, "msx_ng", name="Solo TV", output_format="mp3", grouping_enabled=False)
    p.update_state = Mock()  # type: ignore[misc,method-assign]
    assert PlayerFeature.SET_MEMBERS not in p._attr_supported_features
    assert p._attr_can_group_with == set()
