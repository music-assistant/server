"""Helper(s) to deal with authentication for (music) providers."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp.web import Request, Response

from music_assistant.common.models.enums import EventType

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant


class AuthenticationHelper:
    """Context manager helper class for authentication with a forward and redirect URL."""

    def __init__(self, mass: MusicAssistant, session_id: str):
        """
        Initialize the Authentication Helper.

        Params:
        - url: The URL the user needs to open for authentication.
        - session_id: a unique id for this auth session.
        """
        self.mass = mass
        self.session_id = session_id
        self._callback_response: asyncio.Queue[dict[str, str]] = asyncio.Queue(1)

    @property
    def callback_url(self) -> str:
        """Return the callback URL."""
        return f"{self.mass.webserver.base_url}/callback/{self.session_id}"

    async def __aenter__(self) -> AuthenticationHelper:
        """Enter context manager."""
        self.mass.webserver.register_route(
            f"/callback/{self.session_id}", self._handle_callback, "GET"
        )
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        """Exit context manager."""
        self.mass.webserver.unregister_route(f"/callback/{self.session_id}", "GET")

    async def authenticate(self, auth_url: str, timeout: int = 60) -> dict[str, str]:
        """Start the auth process and return any query params if received on the callback."""
        # redirect the user in the frontend to the auth url
        self.mass.signal_event(EventType.AUTH_SESSION, self.session_id, auth_url)
        async with asyncio.timeout(timeout):
            return await self._callback_response.get()

    async def _handle_callback(self, request: Request) -> Response:
        """Handle callback response."""
        params = dict(request.query)
        await self._callback_response.put(params)
        return_html = """
        <html>
        <body onload="window.close();">
            Authentication completed, you may now close this window.
        </body>
        </html>
        """
        return Response(body=return_html, headers={"content-type": "text/html"})
