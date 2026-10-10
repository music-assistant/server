"""Tests for the DLNA player provider."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS
from music_assistant.providers.dlna.provider import DLNAPlayerProvider


def _bare_provider() -> DLNAPlayerProvider:
    """Create a provider instance without running __init__ (config mocked per-test)."""
    provider = DLNAPlayerProvider.__new__(DLNAPlayerProvider)
    provider.config = MagicMock()
    return provider


async def test_on_upnp_service_discovered_handles_media_renderer() -> None:
    """SSDP discovery should only forward valid MediaRenderer devices."""
    provider = cast("Any", _bare_provider())
    provider.config.get_value.return_value = False
    provider._device_discovered = AsyncMock()

    await provider.on_upnp_service_discovered(
        "ssdp:all",
        {
            "st": "urn:schemas-upnp-org:device:MediaRenderer:1",
            "usn": "uuid:renderer-123::urn:schemas-upnp-org:device:MediaRenderer:1",
            "location": "http://192.168.1.10/description.xml",
        },
    )

    provider._device_discovered.assert_awaited_once_with(
        "uuid:renderer-123",
        "http://192.168.1.10/description.xml",
    )


async def test_config_entries_contain_manual_discovery_ips() -> None:
    """The provider must expose the shared manual discovery IPs config entry."""
    provider = _bare_provider()
    assert CONF_ENTRY_MANUAL_DISCOVERY_IPS in await provider.get_config_entries()


def test_manual_discovery_addresses_reads_config() -> None:
    """The hook must return the manually configured IP addresses."""
    provider = _bare_provider()
    get_value_mock = cast("MagicMock", provider.config.get_value)
    get_value_mock.return_value = ["192.0.2.10", "192.0.2.11"]
    assert provider.upnp_manual_discovery_addresses == ["192.0.2.10", "192.0.2.11"]
    get_value_mock.assert_called_once_with(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key)


def test_manual_discovery_addresses_default_empty() -> None:
    """With nothing configured the hook must return an empty list."""
    provider = _bare_provider()
    get_value_mock = cast("MagicMock", provider.config.get_value)
    get_value_mock.return_value = None
    assert provider.upnp_manual_discovery_addresses == []
