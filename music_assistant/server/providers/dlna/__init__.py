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
from contextlib import suppress
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar

from async_upnp_client.aiohttp import AiohttpSessionRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.exceptions import UpnpError, UpnpResponseError
from async_upnp_client.profiles.dlna import DmrDevice, TransportState
from async_upnp_client.search import async_search

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_CROSSFADE_FLOW_MODE_REQUIRED,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_ENFORCE_MP3,
    CONF_ENTRY_HTTP_PROFILE,
    ConfigEntry,
    ConfigValueType,
    create_sample_rates_config_entry,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    PlayerFeature,
    PlayerState,
    PlayerType,
)
from music_assistant.common.models.errors import PlayerUnavailableError
from music_assistant.common.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.constants import CONF_ENFORCE_MP3, CONF_PLAYERS, VERBOSE_LOG_LEVEL
from music_assistant.server.helpers.didl_lite import create_didl_metadata
from music_assistant.server.helpers.util import TaskManager
from music_assistant.server.models.player_provider import PlayerProvider

from .helpers import DLNANotifyServer

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Sequence

    from async_upnp_client.client import UpnpRequester, UpnpService, UpnpStateVariable
    from async_upnp_client.utils import CaseInsensitiveDict

    from music_assistant.common.models.config_entries import PlayerConfig, ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

BASE_PLAYER_FEATURES = (
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.VOLUME_SET,
)

CONF_ENQUEUE_NEXT = "enqueue_next"


PLAYER_CONFIG_ENTRIES = (
    ConfigEntry(
        key=CONF_ENQUEUE_NEXT,
        type=ConfigEntryType.BOOLEAN,
        label="Player supports enqueue next/gapless",
        default_value=False,
        description="If the player supports enqueuing the next item for fluid/gapless playback. "
        "\n\nUnfortunately this feature is missing or broken on many DLNA players. \n"
        "Enable it with care. If music stops after one song, "
        "disable this setting (and use flow-mode instead).",
    ),
    CONF_ENTRY_CROSSFADE_FLOW_MODE_REQUIRED,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_ENFORCE_MP3,
    CONF_ENTRY_HTTP_PROFILE,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    create_sample_rates_config_entry(192000, 24, 96000, 24),
)


CONF_NETWORK_SCAN = "network_scan"

_DLNAPlayerProviderT = TypeVar("_DLNAPlayerProviderT", bound="DLNAPlayerProvider")
_R = TypeVar("_R")
_P = ParamSpec("_P")


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return DLNAPlayerProvider(mass, manifest, config)


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
    return (
        ConfigEntry(
            key=CONF_NETWORK_SCAN,
            type=ConfigEntryType.BOOLEAN,
            label="Allow network scan for discovery",
            default_value=False,
            description="Enable network scan for discovery of players. \n"
            "Can be used if (some of) your players are not automatically discovered.",
        ),
    )


def catch_request_errors(
    func: Callable[Concatenate[_DLNAPlayerProviderT, _P], Awaitable[_R]],
) -> Callable[Concatenate[_DLNAPlayerProviderT, _P], Coroutine[Any, Any, _R | None]]:
    """Catch UpnpError errors."""

    @functools.wraps(func)
    async def wrapper(self: _DLNAPlayerProviderT, *args: _P.args, **kwargs: _P.kwargs) -> _R | None:
        """Catch UpnpError errors and check availability before and after request."""
        player_id = kwargs["player_id"] if "player_id" in kwargs else args[0]
        dlna_player = self.dlnaplayers[player_id]
        dlna_player.last_command = time.time()
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            self.logger.debug(
                "Handling command %s for player %s",
                func.__name__,
                dlna_player.player.display_name,
            )
        if not dlna_player.available:
            self.logger.warning("Device disappeared when trying to call %s", func.__name__)
            return None
        try:
            return await func(self, *args, **kwargs)
        except UpnpError as err:
            dlna_player.force_poll = True
            if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
                self.logger.exception("Error during call %s: %r", func.__name__, err)
            else:
                self.logger.error("Error during call %s: %r", func.__name__, str(err))
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
    last_command: float = field(default_factory=time.time)

    def update_attributes(self) -> None:
        """Update attributes of the MA Player from DLNA state."""
        # generic attributes

        if self.available:
            self.player.available = True
            self.player.name = self.device.name
            self.player.volume_level = int((self.device.volume_level or 0) * 100)
            self.player.volume_muted = self.device.is_volume_muted or False
            self.player.state = self.get_state(self.device)
            self.player.supported_features = self.get_supported_features(self.device)
            self.player.current_item_id = self.device.current_track_uri or ""
            if self.player.player_id in self.player.current_item_id:
                self.player.active_source = self.player.player_id
            elif "spotify" in self.player.current_item_id:
                self.player.active_source = "spotify"
            elif self.player.current_item_id.startswith("http"):
                self.player.active_source = "http"
            else:
                # TODO: handle other possible sources here
                self.player.active_source = None
            if self.device.media_position:
                # only update elapsed_time if the device actually reports it
                self.player.elapsed_time = float(self.device.media_position)
                if self.device.media_position_updated_at is not None:
                    self.player.elapsed_time_last_updated = (
                        self.device.media_position_updated_at.timestamp()
                    )
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
    def get_supported_features(device: DmrDevice) -> set[PlayerFeature]:
        """Get player features that are supported at this moment.

        Supported features may change as the device enters different states.
        """
        supported_features = set()

        if device.has_volume_level:
            supported_features.add(PlayerFeature.VOLUME_SET)
        if device.has_volume_mute:
            supported_features.add(PlayerFeature.VOLUME_MUTE)

        return supported_features


class DLNAPlayerProvider(PlayerProvider):
    """DLNA Player provider."""

    dlnaplayers: dict[str, DLNAPlayer] | None = None
    _discovery_running: bool = False

    lock: asyncio.Lock
    requester: UpnpRequester
    upnp_factory: UpnpFactory
    notify_server: DLNANotifyServer

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.dlnaplayers = {}
        self.lock = asyncio.Lock()
        # silence the async_upnp_client logger
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("async_upnp_client").setLevel(logging.DEBUG)
        else:
            logging.getLogger("async_upnp_client").setLevel(self.logger.level + 10)
        self.requester = AiohttpSessionRequester(self.mass.http_session, with_sleep=True)
        self.upnp_factory = UpnpFactory(self.requester, non_strict=True)
        self.notify_server = DLNANotifyServer(self.requester, self.mass)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await self._run_discovery()

    async def unload(self) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        self.mass.streams.unregister_dynamic_route("/notify", "NOTIFY")
        async with TaskManager(self.mass) as tg:
            for dlna_player in self.dlnaplayers.values():
                tg.create_task(self._device_disconnect(dlna_player))

    async def get_player_config_entries(
        self,
        player_id: str,
    ) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        return base_entries + PLAYER_CONFIG_ENTRIES

    def on_player_config_changed(
        self,
        config: PlayerConfig,
        changed_keys: set[str],
    ) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        super().on_player_config_changed(config, changed_keys)
        # run discovery to catch any re-enabled players
        self.mass.create_task(self._run_discovery())
        # reset player features based on config values
        if not (dlna_player := self.dlnaplayers.get(config.player_id)):
            return
        self._set_player_features(dlna_player)

    @catch_request_errors
    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        dlna_player = self.dlnaplayers[player_id]
        assert dlna_player.device is not None
        await dlna_player.device.async_stop()

    @catch_request_errors
    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        dlna_player = self.dlnaplayers[player_id]
        assert dlna_player.device is not None
        await dlna_player.device.async_play()

    @catch_request_errors
    async def play_media(self, player_id: str, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        if self.mass.config.get_raw_player_config_value(player_id, CONF_ENFORCE_MP3, False):
            media.uri = media.uri.replace(".flac", ".mp3")
        dlna_player = self.dlnaplayers[player_id]
        # always clear queue (by sending stop) first
        if dlna_player.device.can_stop:
            await self.cmd_stop(player_id)
        didl_metadata = create_didl_metadata(media)
        title = media.title or media.uri
        await dlna_player.device.async_set_transport_uri(media.uri, title, didl_metadata)
        # Play it
        await dlna_player.device.async_wait_for_can_play(10)
        # optimistically set this timestamp to help in case of a player
        # that does not report the progress
        now = time.time()
        dlna_player.player.elapsed_time = 0
        dlna_player.player.elapsed_time_last_updated = now
        await dlna_player.device.async_play()
        # force poll the device
        for sleep in (1, 2):
            await asyncio.sleep(sleep)
            dlna_player.force_poll = True
            await self.poll_player(dlna_player.udn)

    @catch_request_errors
    async def enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """Handle enqueuing of the next queue item on the player."""
        dlna_player = self.dlnaplayers[player_id]
        if self.mass.config.get_raw_player_config_value(player_id, CONF_ENFORCE_MP3, False):
            media.uri = media.uri.replace(".flac", ".mp3")
        didl_metadata = create_didl_metadata(media)
        title = media.title or media.uri
        await dlna_player.device.async_set_next_transport_uri(media.uri, title, didl_metadata)
        self.logger.debug(
            "Enqued next track (%s) to player %s",
            title,
            dlna_player.player.display_name,
        )

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
        """Poll player for state updates."""
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
            with suppress(ValueError):
                await dlna_player.device.async_update(do_ping=do_ping)
            dlna_player.last_seen = now if do_ping else dlna_player.last_seen
        except UpnpError as err:
            self.logger.debug("Device unavailable: %r", err)
            await self._device_disconnect(dlna_player)
            raise PlayerUnavailableError from err
        finally:
            dlna_player.force_poll = False

    async def _run_discovery(self, use_multicast: bool = False) -> None:
        """Discover DLNA players on the network."""
        if self._discovery_running:
            return
        try:
            self._discovery_running = True
            self.logger.debug("DLNA discovery started...")
            allow_network_scan = self.config.get_value(CONF_NETWORK_SCAN)
            discovered_devices: set[str] = set()

            async def on_response(discovery_info: CaseInsensitiveDict) -> None:
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
                if "rincon" in ssdp_udn.lower():
                    # ignore Sonos devices
                    return

                discovered_devices.add(ssdp_udn)

                await self._device_discovered(ssdp_udn, discovery_info["location"])

            # we iterate between using a regular and multicast search (if enabled)
            if allow_network_scan and use_multicast:
                await async_search(on_response, target=(str(IPv4Address("255.255.255.255")), 1900))
            else:
                await async_search(on_response)

        finally:
            self._discovery_running = False

        def reschedule() -> None:
            self.mass.create_task(self._run_discovery(use_multicast=not use_multicast))

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
                conf_key = f"{CONF_PLAYERS}/{udn}/enabled"
                enabled = self.mass.config.get(conf_key, True)
                # ignore disabled players
                if not enabled:
                    self.logger.debug("Ignoring disabled player: %s", udn)
                    return

                dlna_player = DLNAPlayer(
                    udn=udn,
                    player=Player(
                        player_id=udn,
                        provider=self.instance_id,
                        type=PlayerType.PLAYER,
                        name=udn,
                        available=False,
                        powered=False,
                        # device info will be discovered later after connect
                        device_info=DeviceInfo(
                            model="unknown",
                            address=description_url,
                            manufacturer="unknown",
                        ),
                        needs_poll=True,
                        poll_interval=30,
                    ),
                    description_url=description_url,
                )
                self.dlnaplayers[udn] = dlna_player

            await self._device_connect(dlna_player)

            self._set_player_features(dlna_player)
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
                    self.logger.debug(
                        "Received new state from event for Player %s: %s",
                        dlna_player.player.display_name,
                        state_variable.value,
                    )

        dlna_player.last_seen = time.time()
        self.mass.create_task(self._update_player(dlna_player))

    async def _update_player(self, dlna_player: DLNAPlayer) -> None:
        """Update DLNA Player."""
        prev_url = dlna_player.player.current_item_id
        prev_state = dlna_player.player.state
        dlna_player.update_attributes()
        current_url = dlna_player.player.current_item_id
        current_state = dlna_player.player.state

        if (prev_url != current_url) or (prev_state != current_state):
            # fetch track details on state or url change
            dlna_player.force_poll = True

        # let the MA player manager work out if something actually updated
        self.mass.players.update(dlna_player.udn)

    def _set_player_features(self, dlna_player: DLNAPlayer) -> None:
        """Set Player Features based on config values and capabilities."""
        dlna_player.player.supported_features = BASE_PLAYER_FEATURES
        player_id = dlna_player.player.player_id
        if self.mass.config.get_raw_player_config_value(player_id, CONF_ENQUEUE_NEXT, False):
            dlna_player.player.supported_features = (
                *dlna_player.player.supported_features,
                PlayerFeature.ENQUEUE_NEXT,
            )
