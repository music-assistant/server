"""Various helpers and utils for the DLNA Player Provider."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

from aiohttp.web import Request, Response
from async_upnp_client.const import HttpRequest
from async_upnp_client.event_handler import UpnpEventHandler, UpnpNotifyServer

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpRequester

    from music_assistant import MusicAssistant


class DLNANotifyServer(UpnpNotifyServer):  # type: ignore[misc,unused-ignore]
    """Notify server for async_upnp_client which uses the MA webserver."""

    def __init__(
        self,
        requester: UpnpRequester,
        mass: MusicAssistant,
    ) -> None:
        """Initialize."""
        self.mass = mass
        self.event_handler = UpnpEventHandler(self, requester)
        self.mass.streams.register_dynamic_route("/notify", self._handle_request, method="NOTIFY")

    @property
    def callback_url(self) -> str:
        """Return callback URL on which we are callable."""
        return f"{self.mass.streams.base_url}/notify"

    async def _handle_request(self, request: Request) -> Response:
        """Handle incoming requests."""
        if request.method != "NOTIFY":
            return Response(status=405)

        # Some DLNA devices (e.g. Denon HEOS) send NOTIFY bodies that are not
        # valid UTF-8 when track metadata contains non-ASCII characters in the
        # device's native encoding. Decode leniently so we don't drop the event.
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
        except ET.ParseError as err:
            self.mass.logger.debug(
                "Ignoring malformed XML in DLNA notify from %s: %s",
                request.remote,
                err,
            )
            return Response(status=400)

        return Response(status=status)
