"""
Class to hold all data about a chromecast for creating connections.

This also has the same attributes as the mDNS fields by zeroconf.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional, Tuple

from pychromecast.const import CAST_MANUFACTURERS

from .const import PROV_ID

LOGGER = logging.getLogger(PROV_ID)
DEFAULT_PORT = 8009


@dataclass()
class ChromecastInfo:
    """Class to hold all data about a chromecast for creating connections.

    This also has the same attributes as the mDNS fields by zeroconf.
    """

    services: Optional[set] = field(default_factory=set)
    host: Optional[str] = ""
    port: Optional[int] = 0
    uuid: Optional[str] = ""
    model_name: str = ""
    friendly_name: Optional[str] = ""

    def __post_init__(self):
        """Convert UUID to string."""
        self.uuid = str(self.uuid)

    @property
    def is_audio_group(self) -> bool:
        """Return if this is an audio group."""
        return self.port != DEFAULT_PORT

    @property
    def host_port(self) -> Tuple[str, int]:
        """Return the host+port tuple."""
        return self.host, self.port

    @property
    def manufacturer(self) -> str:
        """Return the manufacturer."""
        if not self.model_name:
            return None
        return CAST_MANUFACTURERS.get(self.model_name.lower(), "Google Inc.")


class CastStatusListener:
    """Helper class to handle pychromecast status callbacks.

    Necessary because a CastDevice entity can create a new socket client
    and therefore callbacks from multiple chromecast connections can
    potentially arrive. This class allows invalidating past chromecast objects.
    """

    def __init__(self, cast_device, chromecast, mz_mgr):
        """Initialize the status listener."""
        self._cast_device = cast_device
        self._uuid = chromecast.uuid
        self._valid = True
        self._mz_mgr = mz_mgr

        chromecast.register_status_listener(self)
        chromecast.socket_client.media_controller.register_status_listener(self)
        chromecast.register_connection_listener(self)
        if cast_device._cast_info.is_audio_group:
            self._mz_mgr.add_multizone(chromecast)
        else:
            self._mz_mgr.register_listener(chromecast.uuid, self)

    def new_cast_status(self, cast_status):
        """Handle reception of a new CastStatus."""
        if self._valid:
            self._cast_device.new_cast_status(cast_status)

    def new_media_status(self, media_status):
        """Handle reception of a new MediaStatus."""
        if self._valid:
            self._cast_device.new_media_status(media_status)

    def new_connection_status(self, connection_status):
        """Handle reception of a new ConnectionStatus."""
        if self._valid:
            self._cast_device.new_connection_status(connection_status)

    def added_to_multizone(self, group_uuid):
        """Handle the cast added to a group."""
        LOGGER.debug(
            "Player %s is added to group %s", self._cast_device.name, group_uuid
        )

    def removed_from_multizone(self, group_uuid):
        """Handle the cast removed from a group."""
        if self._valid:
            self._cast_device.multizone_new_media_status(group_uuid, None)

    def multizone_new_cast_status(self, group_uuid, cast_status):
        """Handle reception of a new CastStatus for a group."""

    def multizone_new_media_status(self, group_uuid, media_status):
        """Handle reception of a new MediaStatus for a group."""
        if self._valid:
            self._cast_device.multizone_new_media_status(group_uuid, media_status)

    def invalidate(self):
        """Invalidate this status listener.

        All following callbacks won't be forwarded.
        """
        # pylint: disable=protected-access
        if self._cast_device._cast_info.is_audio_group:
            self._mz_mgr.remove_multizone(self._uuid)
        else:
            self._mz_mgr.deregister_listener(self._uuid, self)
        self._valid = False
