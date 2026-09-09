"""Tests for the MSX Bridge Provider entry point."""

from __future__ import annotations

from unittest.mock import Mock

from music_assistant_models.enums import ConfigEntryType, PlayerFeature, ProviderFeature

from music_assistant.providers.msx_bridge import setup
from music_assistant.providers.msx_bridge.constants import (
    CONF_GROUP_STREAM_MODE,
    CONF_HTTP_PORT,
    CONF_INCLUDE_CONTENT_LENGTH,
    CONF_OUTPUT_FORMAT,
    DEFAULT_HTTP_PORT,
    DEFAULT_INCLUDE_CONTENT_LENGTH,
    DEFAULT_OUTPUT_FORMAT,
    GROUP_STREAM_MODE_INDEPENDENT,
    GROUP_STREAM_MODE_REDIRECT,
)
from music_assistant.providers.msx_bridge.player import MSXPlayer
from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider


async def test_setup_returns_provider(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """setup() should return an MSXBridgeProvider instance."""
    result = await setup(mass_mock, manifest_mock, config_mock)
    assert isinstance(result, MSXBridgeProvider)


async def test_get_config_entries(provider: MSXBridgeProvider) -> None:
    """get_config_entries() should return core config entries."""
    entries = await provider.get_config_entries()
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

    assert all(e.key != "enable_player_grouping" for e in entries)


async def test_sendspin_bridge_removed_from_config(provider: MSXBridgeProvider) -> None:
    """The Sendspin bridge lived with the web kiosk and is no longer a config entry."""
    entries = await provider.get_config_entries()
    keys = [e.key for e in entries]
    assert "enable_sendspin_bridge" not in keys
    mode_entry = next(e for e in entries if e.key == CONF_GROUP_STREAM_MODE)
    assert mode_entry.default_value == GROUP_STREAM_MODE_REDIRECT


async def test_stream_delivery_advanced_options(provider: MSXBridgeProvider) -> None:
    """Redirect is the default and independent remains an advanced fallback."""
    entries = await provider.get_config_entries()
    entry = next(e for e in entries if e.key == CONF_GROUP_STREAM_MODE)
    assert entry.options is not None
    values = [o.value for o in entry.options]
    assert values == [GROUP_STREAM_MODE_REDIRECT, GROUP_STREAM_MODE_INDEPENDENT]
    assert entry.advanced is True

    content_length = next(e for e in entries if e.key == CONF_INCLUDE_CONTENT_LENGTH)
    assert content_length.type == ConfigEntryType.BOOLEAN
    assert content_length.default_value is DEFAULT_INCLUDE_CONTENT_LENGTH
    assert content_length.advanced is True


async def test_setup_does_not_advertise_native_grouping(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """MSX players are grouped by Universal Group, not their provider."""
    result = await setup(mass_mock, manifest_mock, config_mock)
    assert isinstance(result, MSXBridgeProvider)
    assert ProviderFeature.SYNC_PLAYERS not in result.supported_features


async def test_setup_removes_legacy_grouping_config(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """The removed provider-native grouping switch is cleared from storage."""
    mass_mock.config.get_raw_provider_config_value.return_value = False

    await setup(mass_mock, manifest_mock, config_mock)

    mass_mock.config.remove_provider_config_value.assert_awaited_once_with(
        config_mock.instance_id,
        "enable_player_grouping",
    )


def test_player_is_eligible_as_universal_group_member(provider: MSXBridgeProvider) -> None:
    """An MSX player remains a regular player without native grouping features."""
    player = MSXPlayer(provider, "msx_tv")
    assert PlayerFeature.SET_MEMBERS not in player.supported_features
    assert player.can_group_with == set()
