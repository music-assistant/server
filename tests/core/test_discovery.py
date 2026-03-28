"""Tests for the discovery core controller."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from zeroconf import InterfaceChoice, IPVersion
from zeroconf.asyncio import AsyncZeroconf

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
        publish_port=8095,
        base_url="http://127.0.0.1:8095",
    )
    mock_zc = MagicMock(spec=AsyncZeroconf)
    mock_zc.async_register_service = AsyncMock()
    mock_zc.async_update_service = AsyncMock()
    mock_zc.async_unregister_service = AsyncMock()
    mock_zc.async_close = AsyncMock()

    with (
        patch(
            "music_assistant.controllers.discovery.controller.AsyncZeroconf",
            return_value=mock_zc,
        ) as mock_async_zeroconf,
        patch(
            "music_assistant.controllers.discovery.controller.get_ip_pton",
            new=AsyncMock(return_value=b"\x7f\x00\x00\x01"),
        ),
    ):
        await mass_minimal.discovery.setup(await mass_minimal.config.get_core_config("discovery"))
        assert mass_minimal.discovery.aiozc is mock_zc
        assert mass_minimal.discovery.aiozc is mock_zc

    await mass_minimal.discovery.close()

    mock_async_zeroconf.assert_called_once_with(
        ip_version=IPVersion.All,
        interfaces=InterfaceChoice.All,
    )
