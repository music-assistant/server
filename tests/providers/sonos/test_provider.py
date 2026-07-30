"""Tests for Sonos provider discovery."""

import logging
from unittest.mock import MagicMock

import pytest
from zeroconf import ServiceStateChange

from music_assistant.providers.sonos.provider import SonosPlayerProvider


def _make_provider(enabled: bool = False) -> tuple[SonosPlayerProvider, MagicMock]:
    """Create a Sonos provider with mocked discovery dependencies."""
    provider = SonosPlayerProvider.__new__(SonosPlayerProvider)
    mass = MagicMock()
    provider.mass = mass
    provider.logger = logging.getLogger("test.sonos.discovery")
    provider._ignored_disabled_players = set()
    mass.config.get_raw_player_config_value.return_value = enabled
    mass.players.get_player.return_value = None
    return provider, mass


def _make_discovery_info(player_id: str = "sonos_player") -> MagicMock:
    """Create minimal Sonos mDNS discovery information."""
    info = MagicMock()
    info.decoded_properties = {"uuid": player_id}
    return info


@pytest.mark.asyncio
async def test_disabled_discovery_is_not_scheduled_repeatedly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test repeated announcements for a disabled player are ignored once."""
    provider, mass = _make_provider()
    info = _make_discovery_info()

    with caplog.at_level(logging.DEBUG, logger=provider.logger.name):
        await provider.on_mdns_service_state_change(
            "Toilet._sonos._tcp.local.", ServiceStateChange.Added, info
        )
        await provider.on_mdns_service_state_change(
            "Toilet._sonos._tcp.local.", ServiceStateChange.Updated, info
        )

    mass.call_later.assert_not_called()
    ignored_records = [
        record for record in caplog.records if "in discovery as it is disabled" in record.message
    ]
    assert len(ignored_records) == 1


@pytest.mark.asyncio
async def test_disabled_discovery_logs_again_after_reenable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a new disabled period produces a new discovery diagnostic."""
    provider, mass = _make_provider()
    info = _make_discovery_info()

    with caplog.at_level(logging.DEBUG, logger=provider.logger.name):
        await provider.on_mdns_service_state_change(
            "Toilet._sonos._tcp.local.", ServiceStateChange.Added, info
        )
        mass.config.get_raw_player_config_value.return_value = True
        await provider.on_mdns_service_state_change(
            "Toilet._sonos._tcp.local.", ServiceStateChange.Updated, info
        )
        mass.config.get_raw_player_config_value.return_value = False
        await provider.on_mdns_service_state_change(
            "Toilet._sonos._tcp.local.", ServiceStateChange.Updated, info
        )

    mass.call_later.assert_called_once()
    ignored_records = [
        record for record in caplog.records if "in discovery as it is disabled" in record.message
    ]
    assert len(ignored_records) == 2
