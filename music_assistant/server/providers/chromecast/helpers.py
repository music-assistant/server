"""Helpers to deal with Cast devices."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self
from uuid import UUID

from pychromecast import dial
from pychromecast.const import CAST_TYPE_GROUP

if TYPE_CHECKING:
    from pychromecast.controllers.media import MediaStatus
    from pychromecast.controllers.multizone import MultizoneManager
    from pychromecast.controllers.receiver import CastStatus
    from pychromecast.models import CastInfo
    from pychromecast.socket_client import ConnectionStatus
    from zeroconf import Zeroconf

    from . import CastPlayer, ChromecastProvider

DEFAULT_PORT = 8009


@dataclass
class ChromecastInfo:
    """Class to hold all data about a chromecast for creating connections.

    This also has the same attributes as the mDNS fields by zeroconf.
    """

    services: set
    uuid: UUID
    model_name: str
    friendly_name: str
    host: str
    port: int
    cast_type: str | None = None
    manufacturer: str | None = None
    is_dynamic_group: bool | None = None

    @property
    def is_audio_group(self) -> bool:
        """Return if the cast is an audio group."""
        return self.cast_type == CAST_TYPE_GROUP

    @classmethod
    def from_cast_info(cls: Self, cast_info: CastInfo) -> Self:
        """Instantiate ChromecastInfo from CastInfo."""
        return cls(**cast_info._asdict())

    def update(self, cast_info: CastInfo) -> None:
        """Update ChromecastInfo from CastInfo."""
        for key, value in cast_info._asdict().items():
            if not value:
                continue
            setattr(self, key, value)

    def fill_out_missing_chromecast_info(self, zconf: Zeroconf) -> None:
        """
        Return a new ChromecastInfo object with missing attributes filled in.

        Uses blocking HTTP / HTTPS.
        """
        if self.cast_type is None or self.manufacturer is None:
            # Manufacturer and cast type is not available in mDNS data,
            # get it over HTTP
            cast_info = dial.get_cast_type(
                self,
                zconf=zconf,
            )
            self.cast_type = cast_info.cast_type
            self.manufacturer = cast_info.manufacturer

        if not self.is_audio_group or self.is_dynamic_group is not None:
            # We have all information, no need to check HTTP API.
            return

        # Fill out missing group information via HTTP API.
        http_group_status = dial.get_multizone_status(
            None,
            services=self.services,
            zconf=zconf,
        )
        if http_group_status is not None:
            self.is_dynamic_group = any(
                g.uuid == self.uuid for g in http_group_status.dynamic_groups
            )


class CastStatusListener:
    """
    Helper class to handle pychromecast status callbacks.

    Necessary because a CastDevice entity can create a new socket client
    and therefore callbacks from multiple chromecast connections can
    potentially arrive. This class allows invalidating past chromecast objects.
    """

    def __init__(
        self,
        prov: ChromecastProvider,
        castplayer: CastPlayer,
        mz_mgr: MultizoneManager,
        mz_only=False,
    ):
        """Initialize the status listener."""
        self.prov = prov
        self.castplayer = castplayer
        self._uuid = castplayer.cc.uuid
        self._valid = True
        self._mz_mgr = mz_mgr

        if self.castplayer.cast_info.is_audio_group:
            self._mz_mgr.add_multizone(castplayer.cc)
        if mz_only:
            return

        castplayer.cc.register_status_listener(self)
        castplayer.cc.socket_client.media_controller.register_status_listener(self)
        castplayer.cc.register_connection_listener(self)
        if not self.castplayer.cast_info.is_audio_group:
            self._mz_mgr.register_listener(castplayer.cc.uuid, self)

    def new_cast_status(self, status: CastStatus) -> None:
        """Handle updated CastStatus."""
        if self._valid:
            self.prov.on_new_cast_status(self.castplayer, status)

    def new_media_status(self, status: MediaStatus) -> None:
        """Handle updated MediaStatus."""
        if self._valid:
            self.prov.on_new_media_status(self.castplayer, status)

    def new_connection_status(self, status: ConnectionStatus) -> None:
        """Handle updated ConnectionStatus."""
        if self._valid:
            self.prov.on_new_connection_status(self.castplayer, status)

    @staticmethod
    def added_to_multizone(group_uuid):
        """Handle the cast added to a group."""
        print("##### added_to_multizone: %s" % group_uuid)

    def removed_from_multizone(self, group_uuid):
        """Handle the cast removed from a group."""
        if self._valid:
            # self._cast_device.multizone_new_media_status(group_uuid, None)
            print("##### removed_from_multizone: %s" % group_uuid)

    def multizone_new_cast_status(self, group_uuid, cast_status):  # noqa: ARG002
        """Handle reception of a new CastStatus for a group."""
        print("##### multizone_new_cast_status: %s" % group_uuid)

    def multizone_new_media_status(self, group_uuid, media_status):  # noqa: ARG002
        """Handle reception of a new MediaStatus for a group."""
        if self._valid:
            # self._cast_device.multizone_new_media_status(group_uuid, media_status)
            print("##### multizone_new_media_status: %s" % group_uuid)

    def invalidate(self):
        """
        Invalidate this status listener.

        All following callbacks won't be forwarded.
        """
        # pylint: disable=protected-access
        if self.castplayer.cast_info.is_audio_group:
            self._mz_mgr.remove_multizone(self._uuid)
        else:
            self._mz_mgr.deregister_listener(self._uuid, self)
        self._valid = False
