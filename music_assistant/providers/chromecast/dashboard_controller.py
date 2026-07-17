"""Cast controller to show MA dashboards via the MA cast receiver app."""

from __future__ import annotations

from functools import partial
from typing import Any

from pychromecast.controllers import BaseController

from .constants import MASS_APP_ID

APP_NAMESPACE = "urn:x-cast:io.music-assistant.cast"


class MusicAssistantCastController(BaseController):
    """Controller that launches the MA cast receiver and shows a dashboard."""

    def __init__(self, app_id: str = MASS_APP_ID) -> None:
        """
        Initialize the controller.

        :param app_id: Cast receiver app ID to launch (override for a dev receiver app).
        """
        super().__init__(APP_NAMESPACE, app_id)

    def show_dashboard(
        self, remote_id: str, cast_code: str, path: str, server_version: str
    ) -> None:
        """
        Launch the receiver app and show the given MA dashboard.

        This is a blocking call, run it from an executor.

        :param remote_id: Remote access ID the receiver connects to.
        :param cast_code: One-time code the receiver exchanges for a viewer token.
        :param path: Frontend route to show (e.g. "/party").
        :param server_version: MA server version; the receiver picks the frontend channel from it.
        """
        self.launch(
            callback_function=partial(self._on_launched, remote_id, cast_code, path, server_version)
        )

    def _on_launched(
        self,
        remote_id: str,
        cast_code: str,
        path: str,
        server_version: str,
        msg_sent: bool,
        _response: dict[str, Any] | None,
    ) -> None:
        """Send the show_dashboard message once the receiver app has launched."""
        if msg_sent:
            self._send_show_dashboard(remote_id, cast_code, path, server_version)

    def _send_show_dashboard(
        self, remote_id: str, cast_code: str, path: str, server_version: str
    ) -> None:
        self.send_message(
            {
                "type": "show_dashboard",
                "remoteId": remote_id,
                "castCode": cast_code,
                "path": path,
                "serverVersion": server_version,
            }
        )
