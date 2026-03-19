"""Tests for the DLNA player provider."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.dlna.provider import DLNAPlayerProvider


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
