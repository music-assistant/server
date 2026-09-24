"""Various helpers and utils for the Open Home Player Provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

import defusedxml.ElementTree as DefusedET
from aiohttp.web import Request, Response
from async_upnp_client.const import HttpRequest
from async_upnp_client.event_handler import UpnpEventHandler, UpnpNotifyServer

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpRequester

    from music_assistant import MusicAssistant

from music_assistant.providers.openhome_media.constants import CALLBACK_URL


class OpenHomeNotifyServer(UpnpNotifyServer):  # type: ignore[misc,unused-ignore]
    """Notify server for async_upnp_client which uses the MA webserver."""

    def __init__(
        self,
        requester: UpnpRequester,
        mass: MusicAssistant,
    ) -> None:
        """Initialize."""
        self.mass = mass
        self.event_handler = UpnpEventHandler(self, requester)
        self.mass.streams.register_dynamic_route(
            path=CALLBACK_URL, handler=self._handle_request, method="NOTIFY"
        )

    @property
    def callback_url(self) -> str:
        """Return callback URL on which we are callable."""
        return f"{self.mass.streams.base_url}{CALLBACK_URL}"

    async def _handle_request(self, request: Request) -> Response:
        """Handle incoming requests."""
        if request.method != "NOTIFY":
            return Response(status=405)

        # follow DLNA example and decode leniently.
        body_bytes = await request.read()
        body = body_bytes.decode("utf-8", errors="replace")

        # transform aiohttp request to async_upnp_client request
        http_request = HttpRequest(
            method=request.method,
            url=str(request.url),
            headers=request.headers,
            body=body,
        )

        try:
            status = await self.event_handler.handle_notify(http_request)
        except DefusedET.ParseError as err:
            self.mass.logger.debug(
                "Ignoring malformed XML in OpenHome Media notify from %s: %s",
                request.remote,
                err,
            )
            return Response(status=400)

        return Response(status=status)


def create_short_player_id(uuid: str) -> str:
    """Return a short identifier from the UDN of the device."""
    # TODO params
    short_id = uuid.removeprefix("uuid:").lower()
    if uuid.count("-") == 4:  # looks like a MAC address
        between_dashes = uuid[uuid.find("-") + 1 : uuid.rfind("-")]
        short_id = between_dashes.replace("-", "")
    return short_id


def get_source_index_of_type(source_xml: str, source_type: str) -> int | None:
    """Return index in source_xml for source with type source_type."""
    # TODO params
    root = DefusedET.fromstring(source_xml)
    sources = root.findall(".//Source")
    source_index = None
    for i, source in enumerate(sources):
        name_elem = source.find("Type")
        if name_elem is not None and name_elem.text == source_type:
            source_index = i
            break

    return source_index
