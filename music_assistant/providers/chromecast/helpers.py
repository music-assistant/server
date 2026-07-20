"""Helpers to deal with Cast devices."""

from __future__ import annotations

import threading
import time
import urllib.error
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from pychromecast import dial
from pychromecast.const import CAST_TYPE_GROUP

from music_assistant.constants import VERBOSE_LOG_LEVEL

from .constants import DASHBOARD_NAMESPACE, MASS_APP_ID

if TYPE_CHECKING:
    from pychromecast import Chromecast
    from pychromecast.controllers.media import MediaStatus, MediaStatusListener
    from pychromecast.controllers.multizone import MultizoneManager, MultiZoneManagerListener
    from pychromecast.controllers.receiver import CastStatus
    from pychromecast.controllers.receiver import CastStatusListener as ReceiverStatusListener
    from pychromecast.models import CastInfo, HostServiceInfo, MDNSServiceInfo
    from pychromecast.socket_client import ConnectionStatus, ConnectionStatusListener
    from zeroconf import Zeroconf

    from .player import ChromecastPlayer

DEFAULT_PORT = 8009
DASHBOARD_NAMESPACE_POLL_INTERVAL = 0.1


def send_show_dashboard(
    chromecast: Chromecast,
    remote_id: str,
    dashboard_code: str,
    path: str,
    server_version: str,
    timeout: float = 30.0,
) -> None:
    """
    Launch the MA cast receiver app and send it a show_dashboard message.

    Blocking call, run from an executor.

    :param chromecast: Connected Chromecast to show the dashboard on.
    :param remote_id: Remote access ID the receiver connects to.
    :param dashboard_code: One-time code the receiver exchanges for a viewer token.
    :param path: Frontend route to show (e.g. "/party").
    :param server_version: MA server version; the receiver picks the frontend channel from it.
    :param timeout: Seconds to wait for the app launch and the dashboard namespace.
    :raises TimeoutError: If the receiver app did not launch (in time), or the
        dashboard namespace never became available.
    """
    launched = threading.Event()
    launch_success = False

    def _on_launched(success: bool, _response: dict[str, Any] | None) -> None:
        nonlocal launch_success
        launch_success = success
        launched.set()

    deadline = time.monotonic() + timeout
    chromecast.socket_client.receiver_controller.launch_app(
        MASS_APP_ID, callback_function=_on_launched
    )
    if not launched.wait(timeout):
        msg = f"Timed out launching app on {chromecast.name}"
        raise TimeoutError(msg)
    if not launch_success:
        msg = f"Failed to launch app on {chromecast.name}"
        raise TimeoutError(msg)

    # tiny race: the namespace only appears once the socket client has processed
    # the same receiver status that completes the launch callback
    while DASHBOARD_NAMESPACE not in chromecast.socket_client.app_namespaces:
        if time.monotonic() >= deadline:
            msg = f"Timed out waiting for the dashboard namespace on {chromecast.name}"
            raise TimeoutError(msg)
        time.sleep(DASHBOARD_NAMESPACE_POLL_INTERVAL)

    chromecast.socket_client.send_app_message(
        DASHBOARD_NAMESPACE,
        {
            "type": "show_dashboard",
            "remoteId": remote_id,
            "dashboardCode": dashboard_code,
            "path": path,
            "serverVersion": server_version,
        },
    )


def send_hide_dashboard(chromecast: Chromecast) -> bool:
    """
    Send a hide_dashboard message to an already-running MA receiver app.

    Blocking call, run from an executor. Does not launch the app: if the
    receiver isn't already showing a dashboard, there is nothing to hide.

    :param chromecast: Connected Chromecast to hide the dashboard on.
    :return: Whether a hide_dashboard message was sent.
    """
    if (
        chromecast.app_id != MASS_APP_ID
        or DASHBOARD_NAMESPACE not in chromecast.socket_client.app_namespaces
    ):
        return False

    chromecast.socket_client.send_app_message(DASHBOARD_NAMESPACE, {"type": "hide_dashboard"})
    return True


@dataclass
class ChromecastInfo:
    """
    Class to hold all data about a chromecast for creating connections.

    This also has the same attributes as the mDNS fields by zeroconf.
    """

    services: set[HostServiceInfo | MDNSServiceInfo]
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
    mac_address: str | None = None  # MAC address from eureka_info API

    @property
    def is_audio_group(self) -> bool:
        """Return if the cast is an audio group."""
        return self.cast_type == CAST_TYPE_GROUP

    @classmethod
    def from_cast_info(cls, cast_info: CastInfo) -> ChromecastInfo:
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
                cast("CastInfo", self),
                zconf=zconf,
            )
            self.cast_type = cast_info.cast_type
            self.manufacturer = cast_info.manufacturer

        # Fill out missing group information via HTTP API.
        dynamic_groups, multichannel_groups = get_multizone_info(self.services, zconf)
        self.is_dynamic_group = self.uuid in dynamic_groups
        if self.uuid in multichannel_groups:
            self.is_multichannel_group = True
        elif (
            multichannel_groups
            # Prevent a multichannel group being marked as a multichannel child
            # if not in UUID list
            and self.cast_type != "group"
            and self.model_name != "Google Cast Group"
        ):
            self.is_multichannel_child = True

        # Get MAC address for device matching (not available for groups)
        if self.mac_address is None and self.cast_type != "group":
            self.mac_address = get_mac_address(self.services, zconf)


def get_multizone_info(
    services: set[HostServiceInfo | MDNSServiceInfo],
    zconf: Zeroconf,
    timeout: int = 30,
) -> tuple[set[UUID], set[UUID]]:
    """Get multizone info from eureka endpoint."""
    dynamic_groups: set[UUID] = set()
    multichannel_groups: set[UUID] = set()
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
                if "multichannel_group" not in group:
                    continue
                if group["multichannel_group"] and (udn := group.get("uuid")):
                    uuid = UUID(udn.replace("-", ""))
                    # new firmware drops cast_port and renames elected_leader to leader
                    is_leader = (
                        group.get("elected_leader") == "self" or group.get("leader") == "self"
                    )
                    if group.get("cast_port") or not is_leader:
                        multichannel_groups.add(uuid)
    except urllib.error.HTTPError, urllib.error.URLError, OSError, KeyError, ValueError:
        pass
    return (dynamic_groups, multichannel_groups)


def get_mac_address(
    services: set[HostServiceInfo | MDNSServiceInfo], zconf: Zeroconf, timeout: int = 10
) -> str | None:
    """
    Get MAC address from Chromecast eureka_info API.

    :param services: Set of zeroconf service info.
    :param zconf: Zeroconf instance.
    :param timeout: Request timeout in seconds.
    :return: MAC address string or None if not available.
    """
    try:
        _, status = dial._get_status(
            services,
            zconf,
            "/setup/eureka_info?options=detail",
            True,
            timeout,
            None,
        )
        if mac_address := status.get("mac_address"):
            # Normalize to uppercase with colons
            mac = mac_address.upper().replace("-", ":")
            # Ensure proper format
            if ":" not in mac and len(mac) == 12:
                mac = ":".join(mac[i : i + 2] for i in range(0, 12, 2))
            return str(mac)
    except urllib.error.HTTPError, urllib.error.URLError, OSError, KeyError, ValueError:
        pass
    return None


class CastStatusListener:
    """
    Helper class to handle pychromecast status callbacks.

    Necessary because a CastDevice entity can create a new socket client
    and therefore callbacks from multiple chromecast connections can
    potentially arrive. This class allows invalidating past chromecast objects.
    """

    def __init__(
        self,
        castplayer: ChromecastPlayer,
        mz_mgr: MultizoneManager,
        mz_only: bool = False,
    ) -> None:
        """Initialize the status listener."""
        self.castplayer = castplayer
        self._uuid = castplayer.cc.uuid
        self._valid = True
        self._mz_mgr = mz_mgr
        if self.castplayer.cast_info.is_audio_group:
            self._mz_mgr.add_multizone(castplayer.cc)
        if mz_only:
            return
        castplayer.cc.register_status_listener(cast("ReceiverStatusListener", self))
        castplayer.cc.socket_client.media_controller.register_status_listener(
            cast("MediaStatusListener", self)
        )
        castplayer.cc.register_connection_listener(cast("ConnectionStatusListener", self))
        if not self.castplayer.cast_info.is_audio_group:
            self._mz_mgr.register_listener(
                castplayer.cc.uuid, cast("MultiZoneManagerListener", self)
            )

    def new_cast_status(self, status: CastStatus) -> None:
        """Handle updated CastStatus."""
        if not self._valid:
            return
        self.castplayer.on_new_cast_status(status)

    def new_media_status(self, status: MediaStatus) -> None:
        """Handle updated MediaStatus."""
        if not self._valid:
            return
        self.castplayer.on_new_media_status(status)

    def new_connection_status(self, status: ConnectionStatus) -> None:
        """Handle updated ConnectionStatus."""
        if not self._valid:
            return
        self.castplayer.on_new_connection_status(status)

    def added_to_multizone(self, group_uuid: str) -> None:
        """Handle the cast added to a group."""
        self.castplayer.logger.debug(
            "%s is added to multizone: %s", self.castplayer.display_name, group_uuid
        )
        player_status = self.castplayer.cc.status
        if player_status is None:
            return

        self.new_cast_status(player_status)

    def removed_from_multizone(self, group_uuid: str) -> None:
        """Handle the cast removed from a group."""
        if not self._valid:
            return
        if group_uuid == self.castplayer.active_source:
            mass = self.castplayer.mass
            mass.loop.call_soon_threadsafe(self.castplayer.update_state)
        self.castplayer.logger.debug(
            "%s is removed from multizone: %s", self.castplayer.display_name, group_uuid
        )
        player_status = self.castplayer.cc.status
        if player_status is None:
            return

        self.new_cast_status(player_status)

    def multizone_new_cast_status(self, group_uuid: str, cast_status: CastStatus) -> None:
        """Handle reception of a new CastStatus for a group."""
        mass = self.castplayer.mass
        if group_player := mass.players.get_player(group_uuid):
            if TYPE_CHECKING:
                assert isinstance(group_player, ChromecastPlayer)
            if group_player.cc.media_controller.is_active:
                self.castplayer.active_cast_group = group_uuid
            elif group_uuid == self.castplayer.active_cast_group:
                self.castplayer.active_cast_group = None

        self.castplayer.logger.log(
            VERBOSE_LOG_LEVEL,
            "%s got new cast status for group: %s",
            self.castplayer.display_name,
            group_uuid,
        )
        player_status = self.castplayer.cc.status
        if player_status is None:
            return

        self.new_cast_status(player_status)

    def multizone_new_media_status(self, group_uuid: str, media_status: MediaStatus) -> None:
        """Handle reception of a new MediaStatus for a group."""
        if not self._valid:
            return
        self.castplayer.logger.log(
            VERBOSE_LOG_LEVEL,
            "%s got new media_status for group: %s",
            self.castplayer.display_name,
            group_uuid,
        )
        self.castplayer.on_new_media_status(media_status)

    def load_media_failed(self, queue_item_id: int, error_code: int) -> None:
        """Call when media failed to load."""
        self.castplayer.logger.warning(
            "Load media failed: %s - error code: %s", queue_item_id, error_code
        )

    def invalidate(self) -> None:
        """
        Invalidate this status listener.

        All following callbacks won't be forwarded.
        """
        if self.castplayer.cast_info.is_audio_group:
            self._mz_mgr.remove_multizone(self._uuid)
        else:
            self._mz_mgr.deregister_listener(self._uuid, cast("MultiZoneManagerListener", self))
        self._valid = False
