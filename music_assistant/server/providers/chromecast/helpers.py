"""Helpers to deal with Cast devices."""

from __future__ import annotations

import urllib.error
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Self
from uuid import UUID

from pychromecast import dial
from pychromecast.const import CAST_TYPE_GROUP

from music_assistant.constants import VERBOSE_LOG_LEVEL

if TYPE_CHECKING:
    from pychromecast.controllers.media import MediaStatus
    from pychromecast.controllers.multizone import MultizoneManager
    from pychromecast.controllers.receiver import CastStatus
    from pychromecast.models import CastInfo
    from pychromecast.socket_client import ConnectionStatus
    from zeroconf import ServiceInfo, Zeroconf

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
    is_multichannel_group: bool = False  # group created for e.g. stereo pair
    is_multichannel_child: bool = False  # speaker that is part of multichannel setup

    @property
    def is_audio_group(self) -> bool:
        """Return if the cast is an audio group."""
        return self.cast_type == CAST_TYPE_GROUP

    @classmethod
    def from_cast_info(cls: Self, cast_info: CastInfo) -> Self:
        """Instantiate ChromecastInfo from CastInfo."""
        return cls(**asdict(cast_info))

    def update(self, cast_info: CastInfo) -> None:
        """Update ChromecastInfo from CastInfo."""
        for key, value in asdict(cast_info).items():
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

        # Fill out missing group information via HTTP API.
        dynamic_groups, multichannel_groups = get_multizone_info(self.services, zconf)
        self.is_dynamic_group = self.uuid in dynamic_groups
        if self.uuid in multichannel_groups:
            self.is_multichannel_group = True
        elif multichannel_groups:
            self.is_multichannel_child = True


def get_multizone_info(services: list[ServiceInfo], zconf: Zeroconf, timeout=30):
    """Get multizone info from eureka endpoint."""
    dynamic_groups: set[str] = set()
    multichannel_groups: set[str] = set()
    try:
        _, status = dial._get_status(
            services,
            zconf,
            "/setup/eureka_info?params=multizone",
            True,
            timeout,
            None,
        )
        if "multizone" in status and "dynamic_groups" in status["multizone"]:
            for group in status["multizone"]["dynamic_groups"]:
                if udn := group.get("uuid"):
                    uuid = UUID(udn.replace("-", ""))
                    dynamic_groups.add(uuid)

        if "multizone" in status and "groups" in status["multizone"]:
            for group in status["multizone"]["groups"]:
                if group["multichannel_group"] and (udn := group.get("uuid")):
                    uuid = UUID(udn.replace("-", ""))
                    multichannel_groups.add(uuid)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        pass
    return (dynamic_groups, multichannel_groups)


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
    ) -> None:
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
        if not self._valid:
            return
        self.prov.on_new_cast_status(self.castplayer, status)

    def new_media_status(self, status: MediaStatus) -> None:
        """Handle updated MediaStatus."""
        if not self._valid:
            return
        self.prov.on_new_media_status(self.castplayer, status)

    def new_connection_status(self, status: ConnectionStatus) -> None:
        """Handle updated ConnectionStatus."""
        if not self._valid:
            return
        self.prov.on_new_connection_status(self.castplayer, status)

    def added_to_multizone(self, group_uuid) -> None:
        """Handle the cast added to a group."""
        self.prov.logger.debug(
            "%s is added to multizone: %s", self.castplayer.player.display_name, group_uuid
        )
        self.new_cast_status(self.castplayer.cc.status)

    def removed_from_multizone(self, group_uuid) -> None:
        """Handle the cast removed from a group."""
        if not self._valid:
            return
        if group_uuid == self.castplayer.player.active_source:
            self.castplayer.player.active_source = None
        self.prov.logger.debug(
            "%s is removed from multizone: %s", self.castplayer.player.display_name, group_uuid
        )
        self.new_cast_status(self.castplayer.cc.status)

    def multizone_new_cast_status(self, group_uuid, cast_status) -> None:
        """Handle reception of a new CastStatus for a group."""
        if group_player := self.prov.castplayers.get(group_uuid):
            if group_player.cc.media_controller.is_active:
                self.castplayer.active_group = group_uuid
                self.castplayer.player.active_source = group_uuid
                self.castplayer.player.state = group_player.player.state
            elif group_uuid == self.castplayer.active_group:
                self.castplayer.active_group = None
                self.castplayer.player.active_source = self.castplayer.player.player_id

        self.prov.logger.log(
            VERBOSE_LOG_LEVEL,
            "%s got new cast status for group: %s",
            self.castplayer.player.display_name,
            group_uuid,
        )
        self.new_cast_status(self.castplayer.cc.status)

    def multizone_new_media_status(self, group_uuid, media_status) -> None:
        """Handle reception of a new MediaStatus for a group."""
        if not self._valid:
            return
        self.prov.logger.log(
            VERBOSE_LOG_LEVEL,
            "%s got new media_status for group: %s",
            self.castplayer.player.display_name,
            group_uuid,
        )
        self.prov.on_new_media_status(self.castplayer, media_status)

    def load_media_failed(self, queue_item_id, error_code) -> None:
        """Call when media failed to load."""
        self.prov.logger.warning(
            "Load media failed: %s - error code: %s", queue_item_id, error_code
        )

    def invalidate(self) -> None:
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
