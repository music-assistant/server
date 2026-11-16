"""Tests for the DLNA player provider."""

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.providers.dlna.player import DLNAPlayer
from music_assistant.providers.dlna.provider import DLNAPlayerProvider

UDN = "uuid:renderer-123"
DESCRIPTION_URL = "http://192.168.1.10/description.xml"


async def test_on_upnp_service_discovered_handles_media_renderer() -> None:
    """SSDP discovery should only forward valid MediaRenderer devices."""
    provider = cast("Any", DLNAPlayerProvider.__new__(DLNAPlayerProvider))
    provider.config = MagicMock()
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


def _provider(attached: DLNAPlayer | None = None) -> Any:
    """
    Return a provider with a mocked server.

    :param attached: The attached protocol of the renderer the tests discover, if any.
    """
    provider = cast("Any", DLNAPlayerProvider.__new__(DLNAPlayerProvider))
    provider.lock = asyncio.Lock()
    provider._ignored_udns = set()
    provider.mass = MagicMock()
    provider.mass.players.wants_protocol.return_value = True
    provider.logger = MagicMock()
    provider.get_protocol = MagicMock(return_value=attached)
    return provider


async def test_a_renderer_without_an_attached_protocol_is_set_up() -> None:
    """A renderer found again after its player was removed or enabled is set up again."""
    provider = _provider()

    with patch("music_assistant.providers.dlna.provider.DLNAPlayer") as player_class:
        player_class.return_value.setup = AsyncMock(return_value=True)
        await provider._device_discovered(UDN, DESCRIPTION_URL)

    player_class.assert_called_once_with(
        provider=provider, protocol_id=UDN, description_url=DESCRIPTION_URL
    )
    player_class.return_value.setup.assert_awaited_once()


async def test_a_connected_renderer_is_left_alone() -> None:
    """Discovering a connected renderer again sets nothing up."""
    attached = MagicMock(spec=DLNAPlayer)
    attached.description_url = DESCRIPTION_URL
    attached.available = True
    attached.setup = AsyncMock()
    provider = _provider(attached)

    await provider._device_discovered(UDN, DESCRIPTION_URL)

    attached.setup.assert_not_awaited()


async def test_a_renderer_the_server_does_not_want_is_not_set_up() -> None:
    """Nothing connects to a renderer of a disabled player."""
    provider = _provider()
    provider.mass.players.wants_protocol.return_value = False

    with patch("music_assistant.providers.dlna.provider.DLNAPlayer") as player_class:
        await provider._device_discovered(UDN, DESCRIPTION_URL)

    provider.mass.players.wants_protocol.assert_called_once_with(UDN)
    player_class.assert_not_called()
