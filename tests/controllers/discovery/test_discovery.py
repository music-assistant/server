"""Tests for the discovery core controller."""

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from zeroconf import IPVersion
from zeroconf.asyncio import AsyncZeroconf

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.mass import MusicAssistant


class StubUpnpProvider:
    """Minimal provider stub for SSDP discovery tests."""

    def __init__(self) -> None:
        """Initialize the provider stub."""
        self.instance_id = "stub_upnp"
        self.name = "Stub UPNP"
        self.available = True
        self.logger = MagicMock()
        self.manifest = MagicMock(mdns_discovery=None, upnp_discovery=["roku:ecp"])
        self.on_mdns_service_state_change = AsyncMock()
        self.on_upnp_service_discovered = AsyncMock()


@pytest.mark.parametrize(
    ("controller_level", "library_level"),
    [
        (VERBOSE_LOG_LEVEL, logging.DEBUG),
        (logging.DEBUG, logging.INFO),
        (logging.INFO, logging.WARNING),
    ],
)
def test_discovery_library_logger_levels(
    mass_minimal: MusicAssistant, controller_level: int, library_level: int
) -> None:
    """Discovery library loggers should only expose debug output at VERBOSE."""
    controller_logger = mass_minimal.discovery.logger
    library_loggers = [
        logging.getLogger("async_upnp_client"),
        logging.getLogger("zeroconf"),
    ]
    previous_controller_level = controller_logger.level
    previous_library_levels = [logger.level for logger in library_loggers]
    try:
        controller_logger.setLevel(controller_level)
        mass_minimal.discovery._configure_library_loggers()

        assert all(logger.level == library_level for logger in library_loggers)
    finally:
        controller_logger.setLevel(previous_controller_level)
        for logger, previous_level in zip(library_loggers, previous_library_levels, strict=True):
            logger.setLevel(previous_level)


async def test_run_provider_discovery_dispatches_upnp_callbacks(mass: MusicAssistant) -> None:
    """Provider-targeted discovery should fan SSDP results back into the provider callback."""
    provider = StubUpnpProvider()
    mass._providers[provider.instance_id] = provider  # type: ignore[assignment]

    async def fake_async_search(callback: Any, search_target: str, target: Any = None) -> None:
        del target
        await callback(
            {
                "st": search_target,
                "usn": "uuid:roku-123::roku:ecp",
                "_host": "192.168.1.25",
            }
        )

    with patch(
        "music_assistant.controllers.discovery.controller.async_upnp_search",
        new=AsyncMock(side_effect=fake_async_search),
    ) as mock_async_search:
        await mass.discovery.run_provider_discovery(provider)  # type: ignore[arg-type]

    assert mock_async_search.await_count == 1
    assert mock_async_search.await_args is not None
    assert mock_async_search.await_args.kwargs["search_target"] == "roku:ecp"
    provider.on_upnp_service_discovered.assert_awaited_once()
    mass._providers.pop(provider.instance_id, None)
    mass.discovery.on_provider_unload(provider.instance_id)


async def test_discovery_controller_owns_async_zeroconf(mass_minimal: MusicAssistant) -> None:
    """The shared AsyncZeroconf instance should be created by the discovery controller."""
    mass_minimal.config.set(
        "core/discovery",
        {"domain": "discovery", "values": {"zeroconf_interfaces": "all"}},
    )
    mass_minimal.webserver = MagicMock(
        publish_ip="127.0.0.1",
        publish_addresses=["127.0.0.1"],
        publish_port=8095,
        base_url="http://127.0.0.1:8095",
    )
    mock_zc = MagicMock(spec=AsyncZeroconf)
    mock_zc.async_register_service = AsyncMock()
    mock_zc.async_update_service = AsyncMock()
    mock_zc.async_unregister_service = AsyncMock()
    mock_zc.async_close = AsyncMock()

    # Mock a dual-stack adapter so get_zeroconf_args returns predictable results
    mock_adapter = MagicMock()
    mock_adapter.nice_name = "eth0"
    mock_ipv4 = MagicMock()
    mock_ipv4.is_IPv6 = False
    mock_ipv4.ip = "192.168.1.10"
    mock_ipv6 = MagicMock()
    mock_ipv6.is_IPv6 = True
    mock_ipv6.ip = ("fd00::1", 0, 2)
    mock_adapter.ips = [mock_ipv4, mock_ipv6]

    with (
        patch(
            "music_assistant.controllers.discovery.controller.AsyncZeroconf",
            return_value=mock_zc,
        ) as mock_async_zeroconf,
        patch(
            "music_assistant.controllers.discovery.controller.get_ip_pton",
            new=AsyncMock(return_value=b"\x7f\x00\x00\x01"),
        ),
        patch(
            "music_assistant.helpers.util.ifaddr.get_adapters",
            return_value=[mock_adapter],
        ),
    ):
        await mass_minimal.discovery.setup(await mass_minimal.config.get_core_config("discovery"))
        assert mass_minimal.discovery.aiozc is mock_zc

    await mass_minimal.discovery.close()

    mock_async_zeroconf.assert_called_once_with(
        ip_version=IPVersion.All,
        interfaces=["192.168.1.10", "fd00::1%2"],
    )


async def test_mass_service_advertises_webserver_publish_addresses(
    mass_minimal: MusicAssistant,
) -> None:
    """The advertisement must contain exactly the webserver's publish addresses."""
    mass_minimal.webserver = MagicMock(
        publish_ip="192.168.1.10",
        publish_addresses=["192.168.1.10", "fd00::10"],
        publish_port=8095,
        base_url="http://192.168.1.10:8095",
    )
    mock_zc = MagicMock(spec=AsyncZeroconf)
    mock_zc.async_register_service = AsyncMock()
    mock_zc.async_update_service = AsyncMock()
    mass_minimal.discovery._aiozc = mock_zc

    with (
        patch(
            "music_assistant.controllers.discovery.controller.AsyncServiceInfo"
        ) as mock_service_info,
        patch(
            "music_assistant.controllers.discovery.controller.get_ip_pton",
            new=AsyncMock(side_effect=lambda ip: ip.encode()),
        ),
    ):
        await mass_minimal.discovery._register_mass_service()

    assert mock_service_info.call_args.kwargs["addresses"] == [b"192.168.1.10", b"fd00::10"]


async def test_async_find_mdns_service_matches_exact_device_name(mass: MusicAssistant) -> None:
    """A device name must not cross-match another device whose name contains it."""
    mass.discovery.aiozc.zeroconf.cache.cache = {
        "aabbccddeeff@kelder._raop._tcp.local.": {},
        "atv kelder._raop._tcp.local.": {},
    }
    mock_info = MagicMock()
    mock_info.async_request = AsyncMock(return_value=True)
    with patch(
        "music_assistant.controllers.discovery.controller.AsyncServiceInfo",
        return_value=mock_info,
    ) as mock_service_info:
        result = await mass.discovery.async_find_mdns_service(
            "_raop._tcp.local.", "Kelder", timeout=1.0
        )

    assert result is mock_info
    # "Kelder" must resolve to its own RAOP entry, never the "ATV Kelder" one.
    mock_service_info.assert_called_once_with(
        "_raop._tcp.local.", "aabbccddeeff@kelder._raop._tcp.local."
    )


async def test_async_find_mdns_service_no_substring_match(mass: MusicAssistant) -> None:
    """Looking up a name that is only a substring of a discovered name should not match."""
    mass.discovery.aiozc.zeroconf.cache.cache = {
        "atv kelder._raop._tcp.local.": {},
    }
    with patch(
        "music_assistant.controllers.discovery.controller.AsyncServiceInfo",
    ) as mock_service_info:
        result = await mass.discovery.async_find_mdns_service(
            "_raop._tcp.local.", "Kelder", timeout=0.1
        )

    assert result is None
    mock_service_info.assert_not_called()


async def test_async_find_mdns_service_preserves_at_sign_in_name(mass: MusicAssistant) -> None:
    """Only a RAOP MAC prefix is stripped, so device names containing '@' still match."""
    mass.discovery.aiozc.zeroconf.cache.cache = {
        "living@home._airplay._tcp.local.": {},
    }
    mock_info = MagicMock()
    mock_info.async_request = AsyncMock(return_value=True)
    with patch(
        "music_assistant.controllers.discovery.controller.AsyncServiceInfo",
        return_value=mock_info,
    ) as mock_service_info:
        result = await mass.discovery.async_find_mdns_service(
            "_airplay._tcp.local.", "Living@Home", timeout=1.0
        )

    assert result is mock_info
    mock_service_info.assert_called_once_with(
        "_airplay._tcp.local.", "living@home._airplay._tcp.local."
    )
