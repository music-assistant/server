"""DLNA/uPNP Player provider for Music Assistant.

Most of this code is based on the implementation within Home Assistant:
https://github.com/home-assistant/core/blob/dev/homeassistant/components/dlna_dmr

All rights/credits reserved.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar

from async_upnp_client.aiohttp import AiohttpSessionRequester
from async_upnp_client.client import UpnpRequester, UpnpService, UpnpStateVariable
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.exceptions import UpnpError, UpnpResponseError
from async_upnp_client.profiles.dlna import DmrDevice, TransportState
from async_upnp_client.search import async_search
from async_upnp_client.utils import CaseInsensitiveDict

from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import PlayerFeature, PlayerState, PlayerType
from music_assistant.common.models.errors import PlayerUnavailableError, QueueEmpty
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import CONF_PLAYERS
from music_assistant.server.helpers.didl_lite import create_didl_metadata
from music_assistant.server.models.player_provider import PlayerProvider

from .helpers import DLNANotifyServer

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import PlayerConfig, ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

PLAYER_FEATURES = (
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.SYNC,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.VOLUME_SET,
)

_DLNAPlayerProviderT = TypeVar("_DLNAPlayerProviderT", bound="DLNAPlayerProvider")
_R = TypeVar("_R")
_P = ParamSpec("_P")


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = DLNAPlayerProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return tuple()  # we do not have any config entries (yet)


def catch_request_errors(
    func: Callable[Concatenate[_DLNAPlayerProviderT, _P], Awaitable[_R]]
) -> Callable[Concatenate[_DLNAPlayerProviderT, _P], Coroutine[Any, Any, _R | None]]:
    """Catch UpnpError errors."""

    @functools.wraps(func)
    async def wrapper(self: _DLNAPlayerProviderT, *args: _P.args, **kwargs: _P.kwargs) -> _R | None:
        """Catch UpnpError errors and check availability before and after request."""
        player_id = kwargs["player_id"] if "player_id" in kwargs else args[0]
        dlna_player = self.dlnaplayers[player_id]
        if not dlna_player.available:
            self.logger.warning("Device disappeared when trying to call %s", func.__name__)
            return None
        try:
            return await func(self, *args, **kwargs)
        except UpnpError as err:
            dlna_player.force_poll = True
            self.logger.error("Error during call %s: %r", func.__name__, err)
        return None

    return wrapper


@dataclass
class DLNAPlayer:
    """Class that holds all dlna variables for a player."""

    udn: str  # = player_id
    player: Player  # mass player
    description_url: str  # last known location (description.xml) url

    device: DmrDevice | None = None
    lock: asyncio.Lock = field(
        default_factory=asyncio.Lock
    )  # Held when connecting or disconnecting the device
    force_poll: bool = False
    ssdp_connect_failed: bool = False

    # Track BOOTID in SSDP advertisements for device changes
    bootid: int | None = None
    last_seen: float = field(default_factory=time.time)
    next_item: str | None = None
    supports_next_uri = True
    end_of_track_reached = False

    def update_attributes(self):
        """Update attributes of the MA Player from DLNA state."""
        # generic attributes

        if self.available:
            self.player.available = True
            self.player.name = self.device.name
            self.player.volume_level = int((self.device.volume_level or 0) * 100)
            self.player.volume_muted = self.device.is_volume_muted or False
            self.player.state = self.get_state(self.device)
            self.player.supported_features = self.get_supported_features(self.device)
            self.player.current_url = self.device.current_track_uri or ""
            self.player.elapsed_time = float(self.device.media_position or 0)
            if self.device.media_position_updated_at is not None:
                self.player.elapsed_time_last_updated = (
                    self.device.media_position_updated_at.timestamp()
                )
            self.player.current_item_id = self.device._get_current_track_meta_data("queue_item_id")
            if self.device.media_duration and self.player.corrected_elapsed_time:
                self.end_of_track_reached = (
                    self.device.media_duration - self.player.corrected_elapsed_time
                ) < 15
        else:
            # device is unavailable
            self.player.available = False

    @property
    def available(self) -> bool:
        """Device is available when we have a connection to it."""
        return self.device is not None and self.device.profile_device.available

    @staticmethod
    def get_state(device: DmrDevice) -> PlayerState:
        """Return current PlayerState of the player."""
        if device.transport_state is None:
            return PlayerState.IDLE
        if device.transport_state in (
            TransportState.PLAYING,
            TransportState.TRANSITIONING,
        ):
            return PlayerState.PLAYING
        if device.transport_state in (
            TransportState.PAUSED_PLAYBACK,
            TransportState.PAUSED_RECORDING,
        ):
            return PlayerState.PAUSED
        if device.transport_state == TransportState.VENDOR_DEFINED:
            # Unable to map this state to anything reasonable, fallback to idle
            return PlayerState.IDLE

        return PlayerState.IDLE

    @staticmethod
    def get_supported_features(device: DmrDevice) -> set(PlayerFeature):
        """Get player features that are supported at this moment.

        Supported features may change as the device enters different states.
        """
        supported_features = set()

        if device.has_volume_level:
            supported_features.add(PlayerFeature.VOLUME_SET)
        if device.has_volume_mute:
            supported_features.add(PlayerFeature.VOLUME_MUTE)

        if device.can_seek_rel_time or device.can_seek_abs_time:
            supported_features.add(PlayerFeature.SEEK)

        return supported_features


class DLNAPlayerProvider(PlayerProvider):
    """DLNA Player provider."""

    dlnaplayers: dict[str, DLNAPlayer] | None = None
    _discovery_running: bool = False

    lock: asyncio.Lock
    requester: UpnpRequester
    upnp_factory: UpnpFactory
    notify_server: DLNANotifyServer

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self.dlnaplayers = {}
        self.lock = asyncio.Lock()
        self.requester = AiohttpSessionRequester(self.mass.http_session, with_sleep=True)
        # silence the async_upnp_client logger a bit
        logging.getLogger("async_upnp_client").setLevel(logging.INFO)
        logging.getLogger("charset_normalizer").setLevel(logging.INFO)

        self.upnp_factory = UpnpFactory(self.requester, non_strict=True)
        self.notify_server = DLNANotifyServer(self.requester, self.mass)
        self.mass.create_task(self._run_discovery())

    async def unload(self) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        self.mass.webserver.unregister_route("/notify", "NOTIFY")
        async with asyncio.TaskGroup() as tg:
            for dlna_player in self.dlnaplayers.values():
                tg.create_task(self._device_disconnect(dlna_player))

    def on_player_config_changed(
        self, config: PlayerConfig, changed_keys: set[str]  # noqa: ARG002
    ) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        # run discovery to catch any re-enabled players
        self.mass.create_task(self._run_discovery())

    @catch_request_errors
    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        dlna_player = self.dlnaplayers[player_id]
        dlna_player.end_of_track_reached = False
        dlna_player.next_item = None
        assert dlna_player.device is not None
        await dlna_player.device.async_stop()

    @catch_request_errors
    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        dlna_player = self.dlnaplayers[player_id]
        assert dlna_player.device is not None
        await dlna_player.device.async_play()

    @catch_request_errors
    async def cmd_play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
        flow_mode: bool = False,
    ) -> None:
        """Send PLAY MEDIA command to given player."""
        dlna_player = self.dlnaplayers[player_id]

        # always clear queue (by sending stop) first
        if dlna_player.device.can_stop:
            await self.cmd_stop(player_id)
        url = await self.mass.streams.resolve_stream_url(
            queue_item=queue_item,
            player_id=dlna_player.udn,
            seek_position=seek_position,
            fade_in=fade_in,
            flow_mode=flow_mode,
        )

        didl_metadata = create_didl_metadata(self.mass, url, queue_item, flow_mode)
        await dlna_player.device.async_set_transport_uri(url, queue_item.name, didl_metadata)
        # Play it
        await dlna_player.device.async_wait_for_can_play(10)
        await dlna_player.device.async_play()
        # force poll the device
        for sleep in (0, 1, 2):
            await asyncio.sleep(sleep)
            dlna_player.force_poll = True
            await self.poll_player(dlna_player.udn)

    @catch_request_errors
    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        dlna_player = self.dlnaplayers[player_id]
        assert dlna_player.device is not None
        if dlna_player.device.can_pause:
            await dlna_player.device.async_pause()
        else:
            await dlna_player.device.async_stop()

    @catch_request_errors
    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        dlna_player = self.dlnaplayers[player_id]
        assert dlna_player.device is not None
        await dlna_player.device.async_set_volume_level(volume_level / 100)

    @catch_request_errors
    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        dlna_player = self.dlnaplayers[player_id]
        assert dlna_player.device is not None
        await dlna_player.device.async_mute_volume(muted)

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates.

        This is called by the Player Manager;
        - every 360 seconds if the player if not powered
        - every 30 seconds if the player is powered
        - every 10 seconds if the player is playing

        Use this method to request any info that is not automatically updated and/or
        to detect if the player is still alive.
        If this method raises the PlayerUnavailable exception,
        the player is marked as unavailable until
        the next successful poll or event where it becomes available again.
        If the player does not need any polling, simply do not override this method.
        """
        dlna_player = self.dlnaplayers[player_id]

        # try to reconnect the device if the connection was lost
        if not dlna_player.device:
            if not dlna_player.force_poll:
                return
            try:
                await self._device_connect(dlna_player)
            except UpnpError as err:
                raise PlayerUnavailableError from err

        assert dlna_player.device is not None

        try:
            now = time.time()
            do_ping = dlna_player.force_poll or (now - dlna_player.last_seen) > 60
            await dlna_player.device.async_update(do_ping=do_ping)
            dlna_player.last_seen = now if do_ping else dlna_player.last_seen
        except UpnpError as err:
            self.logger.debug("Device unavailable: %r", err)
            await self._device_disconnect(dlna_player)
            raise PlayerUnavailableError from err
        finally:
            dlna_player.force_poll = False

    async def _run_discovery(self) -> None:
        """Discover DLNA players on the network."""
        if self._discovery_running:
            return
        try:
            self._discovery_running = True
            self.logger.debug("DLNA discovery started...")
            discovered_devices: set[str] = set()

            async def on_response(discovery_info: CaseInsensitiveDict):
                """Process discovered device from ssdp search."""
                ssdp_st: str = discovery_info.get("st", discovery_info.get("nt"))
                if not ssdp_st:
                    return

                if "MediaRenderer" not in ssdp_st:
                    # we're only interested in MediaRenderer devices
                    return

                ssdp_usn: str = discovery_info["usn"]
                ssdp_udn: str | None = discovery_info.get("_udn")
                if not ssdp_udn and ssdp_usn.startswith("uuid:"):
                    ssdp_udn = ssdp_usn.split("::")[0]

                if ssdp_udn in discovered_devices:
                    # already processed this device
                    return

                discovered_devices.add(ssdp_udn)

                await self._device_discovered(ssdp_udn, discovery_info["location"])

            await async_search(on_response, 60)

        finally:
            self._discovery_running = False

        def reschedule():
            self.mass.create_task(self._run_discovery())

        # reschedule self once finished
        self.mass.loop.call_later(300, reschedule)

    async def _device_disconnect(self, dlna_player: DLNAPlayer) -> None:
        """
        Destroy connections to the device now that it's not available.

        Also call when removing this entity from MA to clean up connections.
        """
        async with dlna_player.lock:
            if not dlna_player.device:
                self.logger.debug("Disconnecting from device that's not connected")
                return

            self.logger.debug("Disconnecting from %s", dlna_player.device.name)

            dlna_player.device.on_event = None
            old_device = dlna_player.device
            dlna_player.device = None
            await old_device.async_unsubscribe_services()

    async def _device_discovered(self, udn: str, description_url: str) -> None:
        """Handle discovered DLNA player."""
        async with self.lock:
            if dlna_player := self.dlnaplayers.get(udn):
                # existing player
                if dlna_player.description_url == description_url and dlna_player.player.available:
                    # nothing to do, device is already connected
                    return
                # update description url to newly discovered one
                dlna_player.description_url = description_url
            else:
                # new player detected, setup our DLNAPlayer wrapper

                # ignore disabled players
                conf_key = f"{CONF_PLAYERS}/{udn}/enabled"
                enabled = self.mass.config.get(conf_key, True)
                if not enabled:
                    self.logger.debug("Ignoring disabled player: %s", udn)
                    return

                dlna_player = DLNAPlayer(
                    udn=udn,
                    player=Player(
                        player_id=udn,
                        provider=self.domain,
                        type=PlayerType.PLAYER,
                        name=udn,
                        available=False,
                        powered=False,
                        supported_features=PLAYER_FEATURES,
                        # device info will be discovered later after connect
                        device_info=DeviceInfo(
                            model="unknown",
                            address=description_url,
                            manufacturer="unknown",
                        ),
                        # disable sonos players by default in dlna
                        enabled_by_default="rincon" not in udn.lower(),
                    ),
                    description_url=description_url,
                )
                self.dlnaplayers[udn] = dlna_player

            await self._device_connect(dlna_player)

            dlna_player.update_attributes()
            self.mass.players.register_or_update(dlna_player.player)

    async def _device_connect(self, dlna_player: DLNAPlayer) -> None:
        """Connect DLNA/DMR Device."""
        self.logger.debug("Connecting to device at %s", dlna_player.description_url)

        async with dlna_player.lock:
            if dlna_player.device:
                self.logger.debug("Trying to connect when device already connected")
                return

            # Connect to the base UPNP device
            upnp_device = await self.upnp_factory.async_create_device(dlna_player.description_url)

            # Create profile wrapper
            dlna_player.device = DmrDevice(upnp_device, self.notify_server.event_handler)

            # Subscribe to event notifications
            try:
                dlna_player.device.on_event = self._handle_event
                await dlna_player.device.async_subscribe_services(auto_resubscribe=True)
            except UpnpResponseError as err:
                # Device rejected subscription request. This is OK, variables
                # will be polled instead.
                self.logger.debug("Device rejected subscription: %r", err)
            except UpnpError as err:
                # Don't leave the device half-constructed
                dlna_player.device.on_event = None
                dlna_player.device = None
                self.logger.debug("Error while subscribing during device connect: %r", err)
                raise
            else:
                # connect was successful, update device info
                dlna_player.player.device_info = DeviceInfo(
                    model=dlna_player.device.model_name,
                    address=dlna_player.device.device.presentation_url
                    or dlna_player.description_url,
                    manufacturer=dlna_player.device.manufacturer,
                )

    def _handle_event(
        self,
        service: UpnpService,
        state_variables: Sequence[UpnpStateVariable],
    ) -> None:
        """Handle state variable(s) changed event from DLNA device."""
        udn = service.device.udn

        dlna_player = self.dlnaplayers[udn]
        self.logger.debug(
            "Received event for Player %s: %s",
            dlna_player.player.display_name,
            service,
        )

        if not state_variables:
            # Indicates a failure to resubscribe, check if device is still available
            dlna_player.force_poll = True
            return

        if service.service_id == "urn:upnp-org:serviceId:AVTransport":
            for state_variable in state_variables:
                # Force a state refresh when player begins or pauses playback
                # to update the position info.
                if state_variable.name == "TransportState" and state_variable.value in (
                    TransportState.PLAYING,
                    TransportState.PAUSED_PLAYBACK,
                ):
                    dlna_player.force_poll = True
                    self.mass.create_task(self.poll_player(dlna_player.udn))

        dlna_player.last_seen = time.time()
        self.mass.create_task(self._update_player(dlna_player))

    async def _enqueue_next_track(
        self, dlna_player: DLNAPlayer, current_queue_item_id: str
    ) -> None:
        """Enqueue the next track of the MA queue on the CC queue."""
        if not current_queue_item_id:
            return  # guard
        if not self.mass.players.queues.get_item(dlna_player.udn, current_queue_item_id):
            return  # guard
        try:
            next_item, crossfade = await self.mass.players.queues.player_ready_for_next_track(
                dlna_player.udn, current_queue_item_id
            )
        except QueueEmpty:
            return

        if dlna_player.next_item == next_item.queue_item_id:
            return  # already set ?!
        dlna_player.next_item = next_item.queue_item_id

        # no need to try setting the next url if we already know the player does not support it
        if not dlna_player.supports_next_uri:
            return

        # send queue item to dlna queue
        url = await self.mass.streams.resolve_stream_url(
            queue_item=next_item,
            player_id=dlna_player.udn,
            # DLNA pre-caches pretty aggressively so do not yet start the runner
            auto_start_runner=False,
        )
        didl_metadata = create_didl_metadata(self.mass, url, next_item)
        try:
            await dlna_player.device.async_set_next_transport_uri(
                url, next_item.name, didl_metadata
            )
        except UpnpError:
            dlna_player.supports_next_uri = False
            self.logger.info("Player does not support next uri")

        self.logger.debug(
            "Enqued next track (%s) to player %s",
            next_item.name,
            dlna_player.player.display_name,
        )

    async def _update_player(self, dlna_player: DLNAPlayer) -> None:
        """Update DLNA Player."""
        prev_item_id = dlna_player.player.current_item_id
        prev_url = dlna_player.player.current_url
        prev_state = dlna_player.player.state
        dlna_player.update_attributes()
        current_item_id = dlna_player.player.current_item_id
        current_url = dlna_player.player.current_url
        current_state = dlna_player.player.state

        if (prev_url != current_url) or (prev_state != current_state):
            # fetch track details on state or url change
            dlna_player.force_poll = True

        # let the MA player manager work out if something actually updated
        self.mass.players.update(dlna_player.udn)

        # enqueue next item if needed
        if dlna_player.player.state == PlayerState.PLAYING and (
            prev_item_id != current_item_id
            or not dlna_player.next_item
            or dlna_player.next_item == current_item_id
        ):
            self.mass.create_task(self._enqueue_next_track(dlna_player, current_item_id))
        # if player does not support next uri, manual play it
        if (
            not dlna_player.supports_next_uri
            and prev_state == PlayerState.PLAYING
            and current_state == PlayerState.IDLE
            and dlna_player.next_item
            and dlna_player.end_of_track_reached
        ):
            await self.mass.players.queues.play_index(dlna_player.udn, dlna_player.next_item)
            dlna_player.end_of_track_reached = False
            dlna_player.next_item = None
