"""Various helpers and utils for the DLNA Player Provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.helpers.upnp import MassUpnpNotifyServer

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpRequester

    from music_assistant import MusicAssistant


class DLNANotifyServer(MassUpnpNotifyServer):
    """Notify server for async_upnp_client which uses the MA webserver."""

    def __init__(self, requester: UpnpRequester, mass: MusicAssistant) -> None:
        """Initialize with the DLNA-specific NOTIFY route."""
        super().__init__(requester, mass, route_path="/notify")
