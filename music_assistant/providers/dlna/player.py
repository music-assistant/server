"""DLNA Player."""

import asyncio
import functools
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Concatenate

from async_upnp_client.client import UpnpService, UpnpStateVariable
from async_upnp_client.exceptions import UpnpError, UpnpResponseError
from async_upnp_client.profiles.dlna import DmrDevice, TransportState
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.player import DeviceInfo, PlayerMedia

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.upnp import create_didl_metadata
from music_assistant.models.player import Player

from .constants import PLAYER_CONFIG_ENTRIES

if TYPE_CHECKING:
    from .provider import DLNAPlayerProvider


def catch_request_errors[DLNAPlayerT: "DLNAPlayer", **P, R](
    func: Callable[Concatenate[DLNAPlayerT, P], Awaitable[R]],
) -> Callable[Concatenate[DLNAPlayerT, P], Coroutine[Any, Any, R | None]]:
    """Catch UpnpError errors."""

    @functools.wraps(func)
    async def wrapper(self: DLNAPlayerT, *args: P.args, **kwargs: P.kwargs) -> R | None:
        """Catch UpnpError errors and check availability before and after request."""
        self.last_command = time.time()
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            self.logger.debug(
                "Handling command %s for player %s",
                func.__name__,
                self.display_name,
            )
        if not self.available:
            self.logger.warning("Device disappeared when trying to call %s", func.__name__)
            return None
        try:
            return await func(self, *args, **kwargs)
        except UpnpError as err:
            self.force_poll = True
            if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
                self.logger.exception("Error during call %s: %r", func.__name__, err)
            else:
                self.logger.error("Error during call %s: %r", func.__name__, str(err))
        return None

    return wrapper


class DLNAPlayer(Player):
    """DLNA Player."""

    def __init__(
        self,
        provider: "DLNAPlayerProvider",
        player_id: str,
        description_url: str,
        device: DmrDevice | None = None,
    ) -> None:
        """Init Player.

        The player_id is the udn.
        """
        super().__init__(provider, player_id)

        self.device = device
        self.description_url = description_url  # last known location (description.xml) url

        self.lock = asyncio.Lock()  # Held when connecting or disconnecting the device

        self.force_poll = False  # used, if connection is lost

        # ssdp_connect_failed: bool = False
        #
        # Track BOOTID in SSDP advertisements for device changes
        self.bootid: int | None = None
        self.last_seen = time.time()
        self.last_command = time.time()

    async def _device_connect(self) -> None:
        """Connect DLNA/DMR Device."""
        self.logger.debug("Connecting to device at %s", self.description_url)

        async with self.lock:
            if self.device:
                self.logger.debug("Trying to connect when device already connected")
                return

            # Connect to the base UPNP device
            if TYPE_CHECKING:
                assert isinstance(self.provider, DLNAPlayerProvider)  # for type checking
            upnp_device = await self.provider.upnp_factory.async_create_device(self.description_url)

            # Create profile wrapper
            self.device = DmrDevice(upnp_device, self.provider.notify_server.event_handler)

            # Subscribe to event notifications
            try:
                self.device.on_event = self._handle_event
                await self.device.async_subscribe_services(auto_resubscribe=True)
            except UpnpResponseError as err:
                # Device rejected subscription request. This is OK, variables
                # will be polled instead.
                self.logger.debug("Device rejected subscription: %r", err)
            except UpnpError as err:
                # Don't leave the device half-constructed
                self.device.on_event = None
                self.device = None
                self.logger.debug("Error while subscribing during device connect: %r", err)
                raise
            else:
                # connect was successful, update device info
                self._attr_device_info = DeviceInfo(
                    model=self.device.model_name,
                    ip_address=self.device.device.presentation_url or self.description_url,
                    manufacturer=self.device.manufacturer,
                )

    def _handle_event(
        self,
        service: UpnpService,
        state_variables: Sequence[UpnpStateVariable[Any]],
    ) -> None:
        """Handle state variable(s) changed event from DLNA device."""
        if not state_variables:
            # Indicates a failure to resubscribe, check if device is still available
            self.force_poll = True
            return
        if service.service_id == "urn:upnp-org:serviceId:AVTransport":
            for state_variable in state_variables:
                # Force a state refresh when player begins or pauses playback
                # to update the position info.
                if state_variable.name == "TransportState" and state_variable.value in (
                    TransportState.PLAYING,
                    TransportState.PAUSED_PLAYBACK,
                ):
                    self.force_poll = True
                    self.mass.create_task(self.poll())
                    self.logger.debug(
                        "Received new state from event for Player %s: %s",
                        self.display_name,
                        state_variable.value,
                    )
        self.last_seen = time.time()
        self.mass.create_task(self._update_player())

    async def _update_player(self) -> None:
        """Update DLNA Player."""
        prev_url = self._attr_current_media.uri if self._attr_current_media is not None else ""
        prev_state = self.state
        await self.set_dynamic_attributes()
        current_url = self._attr_current_media.uri if self._attr_current_media is not None else ""
        current_state = self.state

        if (prev_url != current_url) or (prev_state != current_state):
            # fetch track details on state or url change
            self.force_poll = True

        try:
            self.update_state()
        except (KeyError, TypeError):
            # at start the update might come faster than the config is initialized
            await asyncio.sleep(2)
            self.update_state()

    def _set_player_features(self) -> None:
        """Set Player Features based on config values and capabilities."""
        assert self.device is not None  # for type checking
        supported_features: set[PlayerFeature] = {
            # there is no way to check if a dlna player support enqueuing
            # so we simply assume it does and if it doesn't
            # you'll find out at playback time and we log a warning
            PlayerFeature.ENQUEUE,
            PlayerFeature.GAPLESS_PLAYBACK,
        }
        if self.device.has_volume_level:
            supported_features.add(PlayerFeature.VOLUME_SET)
        if self.device.has_volume_mute:
            supported_features.add(PlayerFeature.VOLUME_MUTE)
        if self.device.has_pause:
            supported_features.add(PlayerFeature.PAUSE)
        self._attr_supported_features = supported_features

    async def setup(self) -> None:
        """Set up player in MA."""
        await self._device_connect()
        self.set_static_attributes()
        await self.mass.players.register_or_update(self)

    def set_static_attributes(self) -> None:
        """Set static attributes."""
        self._attr_needs_poll = True
        self._attr_poll_interval = 30
        self._set_player_features()

    async def set_dynamic_attributes(self) -> None:
        """Set dynamic attributes."""
        available = self.device is not None and self.device.profile_device.available
        self._attr_available = available
        if not available:
            return
        assert self.device is not None  # for type checking
        self._attr_name = self.device.name
        self._attr_volume_level = int((self.device.volume_level or 0) * 100)
        self._attr_volume_muted = self.device.is_volume_muted or False
        _playback_state = self._get_playback_state()
        assert _playback_state is not None  # for type checking
        self._attr_playback_state = _playback_state

        _device_uri = self.device.current_track_uri or ""
        self.set_current_media(uri=_device_uri, clear_all=True)

        # Let player controller determine active source, only override for known external sources
        if _device_uri and _device_uri.startswith(self.mass.streams.base_url):
            # MA stream - let controller determine source
            self._attr_active_source = None
        elif "spotify" in _device_uri:
            # Spotify or Spotify Connect
            self._attr_active_source = "spotify"
        elif _device_uri:
            # External HTTP source
            self._attr_active_source = "http"
        else:
            # No URI - idle or unknown
            self._attr_active_source = None
        # TODO: extend this list with other possible sources
        if self.device.media_position:
            # only update elapsed_time if the device actually reports it
            self._attr_elapsed_time = float(self.device.media_position)
            if self.device.media_position_updated_at is not None:
                self._attr_elapsed_time_last_updated = (
                    self.device.media_position_updated_at.timestamp()
                )

    def _get_playback_state(self) -> PlaybackState | None:
        """Return current PlaybackState of the player."""
        if self.device is None:
            return None
        if self.device.transport_state is None:
            return PlaybackState.IDLE
        if self.device.transport_state in (
            TransportState.PLAYING,
            TransportState.TRANSITIONING,
        ):
            return PlaybackState.PLAYING
        if self.device.transport_state in (
            TransportState.PAUSED_PLAYBACK,
            TransportState.PAUSED_RECORDING,
        ):
            return PlaybackState.PAUSED
        if self.device.transport_state == TransportState.VENDOR_DEFINED:
            # Unable to map this state to anything reasonable, fallback to idle
            return PlaybackState.IDLE

        return PlaybackState.IDLE

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_config_entries(action=action, values=values)
        return base_entries + PLAYER_CONFIG_ENTRIES

    # async def on_player_config_change(
    #     self,
    #     config: PlayerConfig,
    #     changed_keys: set[str],
    # ) -> None:
    #     """Call (by config manager) when the configuration of a player changes."""
    #     if dlna_player := self.dlnaplayers.get(config.player_id):
    #         # reset player features based on config values
    #         self._set_player_features(dlna_player)
    #     else:
    #         # run discovery to catch any re-enabled players
    #         self.mass.create_task(self.discover_players())

    # COMMANDS
    @catch_request_errors
    async def stop(self) -> None:
        """Send STOP command to given player."""
        assert self.device is not None  # for type checking
        await self.device.async_stop()

    @catch_request_errors
    async def play(self) -> None:
        """Send PLAY command to given player."""
        assert self.device is not None  # for type checking
        await self.device.async_play()

    @catch_request_errors
    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        assert self.device is not None  # for type checking
        # always clear queue (by sending stop) first
        if self.device.can_stop:
            await self.stop()
        didl_metadata = create_didl_metadata(media)
        title = media.title or media.uri
        await self.device.async_set_transport_uri(media.uri, title, didl_metadata)
        # Play it
        await self.device.async_wait_for_can_play(10)
        # optimistically set this timestamp to help in case of a player
        # that does not report the progress
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        await self.device.async_play()
        # force poll the device
        for sleep in (1, 2):
            await asyncio.sleep(sleep)
            self.force_poll = True
            await self.poll()

    @catch_request_errors
    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing of the next queue item on the player."""
        assert self.device is not None  # for type checking
        didl_metadata = create_didl_metadata(media)
        title = media.title or media.uri
        try:
            await self.device.async_set_next_transport_uri(media.uri, title, didl_metadata)
        except UpnpError:
            self.logger.error(
                "Enqueuing the next track failed for player %s - "
                "the player probably doesn't support this. "
                "Enable 'flow mode' for this player.",
                self.display_name,
            )

    @catch_request_errors
    async def pause(self) -> None:
        """Send PAUSE command to given player."""
        assert self.device is not None  # for type checking
        if self.device.can_pause:
            await self.device.async_pause()
        else:
            await self.device.async_stop()

    @catch_request_errors
    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        assert self.device is not None  # for type checking
        await self.device.async_set_volume_level(volume_level / 100)

    @catch_request_errors
    async def volume_mute(self, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        assert self.device is not None  # for type checking
        await self.device.async_mute_volume(muted)

    async def poll(self) -> None:
        """Poll player for state updates."""
        # try to reconnect the device if the connection was lost
        if not self.device:
            if not self.force_poll:
                return
            try:
                await self._device_connect()
            except UpnpError as err:
                raise PlayerUnavailableError from err

        assert self.device is not None

        try:
            now = time.time()
            do_ping = self.force_poll or (now - self.last_seen) > 60
            with suppress(ValueError):
                await self.device.async_update(do_ping=do_ping)
            self.last_seen = now if do_ping else self.last_seen
        except UpnpError as err:
            self.logger.debug("Device unavailable: %r", err)
            if TYPE_CHECKING:
                assert isinstance(self.provider, DLNAPlayerProvider)  # for type checking
            await self.provider._device_disconnect(self)
            raise PlayerUnavailableError from err
        finally:
            self.force_poll = False
