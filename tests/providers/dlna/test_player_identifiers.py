"""Tests for the identifiers the DLNA player exposes for protocol linking."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from async_upnp_client.client import UpnpRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.const import HttpRequest, HttpResponse
from music_assistant_models.enums import IdentifierType

from music_assistant.providers.dlna.player import DLNAPlayer
from tests.common import MockProvider

DESCRIPTION_URL = "http://192.168.50.21:49152/description.xml"
DESCRIPTION_XML = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>Kitchen</friendlyName>
    <manufacturer>Acme</manufacturer>
    <modelName>Renderer</modelName>
    <UDN>uuid:dlna-player</UDN>
    <presentationURL>{presentation}</presentationURL>
    <serviceList/>
  </device>
</root>"""


class _Requester(UpnpRequester):
    """Requester that answers every request with a fixed body."""

    def __init__(self, body: str) -> None:
        """Initialize with the body to return."""
        self.body = body

    async def async_http_request(self, http_request: HttpRequest) -> HttpResponse:
        """Return the fixed body."""
        return HttpResponse(200, {}, self.body)


def _dmr(upnp_device: Any, _handler: Any) -> MagicMock:
    """Return a DmrDevice mock wrapping the real UpnpDevice."""
    dmr = MagicMock()
    dmr.device = upnp_device
    dmr.model_name = upnp_device.model_name
    dmr.manufacturer = upnp_device.manufacturer
    dmr.async_subscribe_services = AsyncMock()
    return dmr


@pytest.mark.parametrize(
    ("presentation", "expected_ip"),
    [
        ("/", "192.168.50.21"),
        ("", "192.168.50.21"),
        ("http://192.168.50.22/", "192.168.50.22"),
    ],
)
async def test_ip_identifier_from_presentation_url(presentation: str, expected_ip: str) -> None:
    """The IP identifier is set for relative, empty and absolute presentation URLs."""
    provider: Any = MockProvider("dlna", instance_id="dlna_test")
    provider.upnp_factory = UpnpFactory(
        _Requester(DESCRIPTION_XML.format(presentation=presentation))
    )
    provider.notify_server = MagicMock()
    player = DLNAPlayer(provider, "uuid:dlna-player", DESCRIPTION_URL)

    with patch("music_assistant.providers.dlna.player.DmrDevice", _dmr):
        await player._device_connect()

    assert player.device_info.identifiers.get(IdentifierType.IP_ADDRESS) == expected_ip
