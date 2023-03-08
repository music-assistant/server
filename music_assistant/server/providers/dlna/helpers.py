"""Various helpers and utils for the DLNA Player Provider."""
from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp.web import Request, Response
from async_upnp_client.event_handler import UpnpEventHandler, UpnpNotifyServer

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpRequester

    from music_assistant.server import MusicAssistant


class DLNANotifyServer(UpnpNotifyServer):
    """Notify server for async_upnp_client which uses the MA webserver."""

    def __init__(
        self,
        requester: UpnpRequester,
        mass: MusicAssistant,
    ) -> None:
        """Initialize."""
        self.mass = mass
        self.event_handler = UpnpEventHandler(self, requester)
        self.mass.webapp.router.add_route("NOTIFY", "/notify", self._handle_request)

    async def _handle_request(self, request: Request) -> Response:
        """Handle incoming requests."""
        headers = request.headers
        body = await request.text()

        if request.method != "NOTIFY":
            return Response(status=405)

        status = await self.event_handler.handle_notify(headers, body)

        return Response(status=status)

    @property
    def callback_url(self) -> str:
        """Return callback URL on which we are callable."""
        return f"{self.mass.base_url}/notify"
