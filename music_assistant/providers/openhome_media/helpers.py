"""Various helpers and utils for the Open Home Player Provider."""

from __future__ import annotations

import defusedxml.ElementTree as DET
from typing import TYPE_CHECKING

from aiohttp.web import Request, Response
from async_upnp_client.const import HttpRequest
from async_upnp_client.event_handler import UpnpEventHandler, UpnpNotifyServer

from music_assistant_models.player import PlayerMedia

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpRequester
    from music_assistant import MusicAssistant

from .constants import CALLBACK_URL


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
        self.mass.streams.register_dynamic_route(path=CALLBACK_URL, handler=self._handle_request, method="NOTIFY")

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
        except DET.ParseError as err:
            self.mass.logger.debug(
                "Ignoring malformed XML in OpenHome Media notify from %s: %s",
                request.remote,
                err,
            )
            return Response(status=400)

        return Response(status=status)
