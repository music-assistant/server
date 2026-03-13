"""Tests for the discovery core controller."""

from unittest.mock import AsyncMock, MagicMock, patch

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
    mass._providers[provider.instance_id] = provider  # noqa: SLF001

    async def fake_async_search(callback, search_target: str, target=None) -> None:
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
        await mass.discovery.run_provider_discovery(provider)

    assert mock_async_search.await_count == 1
    assert mock_async_search.await_args.kwargs["search_target"] == "roku:ecp"
    provider.on_upnp_service_discovered.assert_awaited_once()
    mass._providers.pop(provider.instance_id, None)  # noqa: SLF001
    mass.discovery.on_provider_unload(provider.instance_id)
