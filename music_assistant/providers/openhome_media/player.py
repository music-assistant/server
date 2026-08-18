"""Linn/OpenHome Media Player implementation."""

from __future__ import annotations

import asyncio
import functools
import time

from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Concatenate
from urllib.parse import urlparse
from uuid import UUID

from async_upnp_client.aiohttp import AiohttpNotifyServer
from async_upnp_client.client import UpnpService, UpnpStateVariable

from async_upnp_client.exceptions import UpnpError, UpnpResponseError, UpnpActionResponseError
from async_upnp_client.profiles.ohmedia import (
    InfoState,
    OhmDevice,
    PlaylistState,
    PlaylistStateAllowedValues,
    ProductSourceType,
    ProductState,
    RadioState,
    ReceiverState,
    ServiceId,
    TimeState,
    Transport,
    TransportState,
    TransportStateAllowedValues,
    VolumeState,
    Service,
)
from async_upnp_client.utils import get_local_ip

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    IdentifierType,
    ConfigEntryType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    # RepeatMode,
)
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.player import PlayerMedia, PlayerSource
from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.upnp import create_didl_metadata
from music_assistant.models.player import DeviceInfo, Player

if TYPE_CHECKING:
    from .provider import OpenHomePlayerProvider


def catch_request_errors[OpenHomePlayerT: "OpenHomePlayer", **P, R](
        func: Callable[Concatenate[OpenHomePlayerT, P], Awaitable[R]],
) -> Callable[Concatenate[OpenHomePlayerT, P], Coroutine[Any, Any, R | None]]:
    """Catch UpnpError errors."""

    @functools.wraps(func)
    async def wrapper(self: OpenHomePlayerT, *args: P.args, **kwargs: P.kwargs) -> R | None:
        """Catch UpnpError errors and check availability before and after request."""
        self.last_command = time.time()
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            self.logger.debug(
                "Handling command %s for player %s",
                func.__name__,
                self.display_name,
            )
        if not self.available:
            self.logger.warning("Device disappeared while calling %s", func.__name__)
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


class OpenHomePlayer(Player):
    """Linn/OpenHome Media Player in Music Assistant."""

    _attr_type = PlayerType.PROTOCOL

    def __init__(
            self,
            provider: "OpenHomePlayerProvider",
            player_id: str,
            description_url: str,
            device: OhmDevice | None = None,
    ) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)

        self.force_poll = False
        self.last_seen = None
        self.profile = device
        self.description_url = description_url

        self.lock = asyncio.Lock()  # Held when connecting or disconnecting the device

        # init some static variables
        self._attr_name = f"Linn/OpenHome Media Player {player_id}"

        self.sources = []
        # self._attr_type = PlayerType.PROTOCOL
        # self.logger = self.provider.logger.getChild(self.player_id)

    # region adapted from DLNA Player
    def set_available(self, available: bool) -> None:
        """Set the availability of the player."""
        self._attr_available = available

    async def _device_connect(self) -> None:
        """Connect Linn/OpenHome Media Device."""
        self.logger.debug("Connecting to device at %s", self.description_url)

        async with self.lock:
            if self.profile:
                self.logger.debug("Trying to connect when device already connected")
                return

            # Connect to the base UPNP device
            if TYPE_CHECKING:
                assert isinstance(self.provider, OpenHomePlayerProvider)  # for type checking
            upnp_device = await self.provider.upnp_factory.async_create_device(self.description_url)

            # Create profile wrapper
            if OhmDevice.is_profile_device(upnp_device):
                self.profile = OhmDevice(upnp_device, self.provider.notify_server.event_handler)
            else:
                self.logger.debug("Device is not an OpenHome Profile: %s", upnp_device)
                return

            # Subscribe to event notifications
            try:
                self.profile.on_event = self._handle_event
                await self.profile.async_subscribe_services(auto_resubscribe=True)
            except UpnpResponseError as err:
                # Device rejected subscription request.
                # This is OK, variables will be polled instead.
                self.logger.debug("Device rejected subscription: %r", err)
                self.force_poll = True
                # populate the state variables
                await self.profile.async_update_state_variables()
            except UpnpError as err:
                # Don't leave the device half-constructed
                self.profile.on_event = None
                self.profile = None
                self.logger.debug("Error while subscribing during device connect: %r", err)
                raise
            else:
                # connect was successful, update device info
                # assign device info
                self._attr_device_info = DeviceInfo(
                    model=self.profile.model_name,
                    manufacturer=self.profile.manufacturer,
                    model_id=self.profile.model_number,
                    manufacturer_id=self.profile.device.manufacturer_url,
                )

                # Identifiers in descending priority MAC_ADDRESS, UUID, IP_ADDRESS
                # MAC address is extracted from UUID if format of UDN is UUID
                # OpenHome Player uses machine name so will be excluded
                if OpenHomePlayer.is_valid_uuid(self.player_id):
                    mac_address = OpenHomePlayer.get_mac_from_uuid(self.player_id)
                    self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac_address)

                # Add player_id (= UDN) as UUID identifier for identifying player across protocols
                # Strip the "uuid:" prefix if present for proper matching
                if self.player_id:
                    self._attr_device_info.add_identifier(IdentifierType.UUID, self.player_id.removeprefix("uuid:"))

                # Try to extract just the IP from the URL for matching
                # All currently known examples have a higher priority identifier available
                ip_address = self.profile.device.presentation_url or self.description_url
                with suppress(ValueError):
                    parsed = urlparse(ip_address)
                    if parsed.hostname:
                        self._attr_device_info.add_identifier(
                            IdentifierType.IP_ADDRESS, parsed.hostname
                        )

                # Get the sources available
                self.sources = await self.profile.async_visible_sources()
                self._attr_source_list = self._source_list_from_sources(self.sources)

    def _handle_event(
            self,
            service: UpnpService,
            state_variables: Sequence[UpnpStateVariable[Any]],
    ) -> None:
        """Handle changed state variable(s) value event from Linn/OpenHome Media device."""
        if not state_variables:
            # Indicates a failure to resubscribe, check if device is still available
            self.force_poll = True
            return
        #
        # EventCallbackType = Callable[["UpnpService", Sequence["UpnpStateVariable"]], None]
        #
        # NOTE: service is a UpnpService and has state_variables property: see class definition in client.py
        # NOTE: on initial subscription, all service variables are returned
        # NOTE: subsequently, only state_variables with changed values will be sent
        #
        active_queue = self.mass.player_queues.get_active_queue(self.player_id)
        if active_queue:
            active_queue_id = active_queue.queue_id  # NOTE: no active queue for some sources
        else:
            active_queue_id = None

        match service.service_id:
            case ServiceId.CREDENTIALS:
                pass
            case ServiceId.INFO:
                self.logger.debug("Info Event: %s", service.service_id)
                for sv in state_variables:
                    self.logger.debug("Info Event: %s %s", sv.name, sv.value)
                    match sv.name:
                        case InfoState.DURATION:
                            if self._attr_current_media:
                                self._attr_current_media.duration = sv.value
                        case _:
                            pass
            case ServiceId.PINS:
                pass
            case ServiceId.PLAYLIST:
                self.logger.debug("Playlist Event: %s", state_variables)
                for sv in state_variables:
                    match sv.name:
                        case PlaylistState.TRANSPORT_STATE:
                            self._attr_playback_state = self._transport_state_to_playback_state(sv.value)
                        case PlaylistState.REPEAT:
                            # TODO: mode dependent
                            if active_queue_id is not None:
                                self._attr_repeat_state = sv.value
                            # if active_queue_id is not None:
                            #     if sv.value:
                            #         self.mass.player_queues.set_repeat(
                            #             active_queue_id, RepeatMode.ALL
                            #         )
                            #     else:
                            #         self.mass.player_queues.set_repeat(
                            #             active_queue_id, RepeatMode.OFF
                            #         )
                        case PlaylistState.SHUFFLE:
                            # TODO: mode dependent
                            if active_queue_id is not None:
                                self._attr_shuffle_state = sv.value
                        case PlaylistState.ID:
                            pass
                        # Should play this element of the Playlist
                        case PlaylistState.ID_ARRAY:
                            pass
                        # NOTE: playlist on Linn should be parsed and used to update playlist on MAss
                        case PlaylistState.TRACKS_MAX:
                            pass
                        case PlaylistState.PROTOCOL_INFO:
                            pass
                        case _:
                            self.logger.warning("Unhandled Playlist State Variable %s", sv.name)
            case ServiceId.PRODUCT:
                self.logger.debug("Product Event: %s", state_variables)
                for sv in state_variables:
                    match sv.name:
                        # case ProductState.ATTRIBUTES:
                        #      These form dict MANUFACTURER
                        # case ProductState.MANUFACTURER_IMAGE_URI:
                        # case ProductState.MANUFACTURER_INFO:
                        # case ProductState.MANUFACTURER_NAME:
                        # case ProductState.MANUFACTURER_URL:
                        #      These form dict MODEL
                        # case ProductState.MODEL_IMAGE_URI:
                        # case ProductState.MODEL_INFO:
                        # case ProductState.MODEL_NAME:
                        # case ProductState.MODEL_URL:
                        #      These form dict PRODUCT
                        # case ProductState.PRODUCT_IMAGE_HIRES_URI:
                        # case ProductState.PRODUCT_IMAGE_URI:
                        # case ProductState.PRODUCT_INFO:
                        # case ProductState.PRODUCT_NAME:
                        # case ProductState.PRODUCT_ROOM:
                        # case ProductState.PRODUCT_URL:

                        # case ProductState.SOURCE_COUNT:
                        case ProductState.SOURCE_INDEX:
                            # TODO make it show in MAss - is it a different attribute? - check other providers
                            # sources only updated on start - but will be fairly static
                            active_source = next(
                                (x for x in self.sources if x["Index"] == sv.value),
                                None,
                            )
                            if active_source:
                                self._attr_active_source = active_source["Name"]
                            else:
                                self._attr_active_source = "N/A"
                            self.update_state()
                        # case ProductState.SOURCE_XML:
                        # case ProductState.STANDBY:
                        # case ProductState.STANDBY_TRANSITIONING:
                        case _:
                            pass
                # TODO update current source
            case ServiceId.RADIO:
                # TODO: add updates on Radio events
                self.logger.debug("Radio Event: %s", state_variables)
                for sv in state_variables:
                    match sv.name:
                        case RadioState.TRANSPORT_STATE:
                            self._attr_playback_state = self._transport_state_to_playback_state(sv.value)
            case ServiceId.RECEIVER:
                pass
            case ServiceId.SENDER:
                pass
            case ServiceId.TRANSPORT:
                self.logger.debug("Transport Event: %s", state_variables)
                for sv in state_variables:
                    match sv.name:
                        case TransportState.TRANSPORT_STATE:
                            self._attr_playback_state = self._transport_state_to_playback_state(sv.value)
                        case TransportState.REPEAT:
                            if active_queue_id is not None:
                                self._attr_repeat_state = sv.value
                        case TransportState.SHUFFLE:
                            if active_queue_id is not None:
                                self._attr_shuffle_state = sv.value
                        case _:
                            pass
            case ServiceId.VOLUME:
                self.logger.debug("Volume Event: %s", state_variables)
                for sv in state_variables:
                    match sv.name:
                        case VolumeState.MUTE:
                            self._attr_volume_muted = sv.value
                        case VolumeState.VOLUME:
                            self._attr_volume_level = sv.value
                        case _:
                            pass  # NOTE: ignore any other state variables
            case ServiceId.TIME:
                # self.logger.debug("Time Event: %s", state_variables)
                for sv in state_variables:
                    match sv.name:
                        case TimeState.TRACK_COUNT:
                            pass
                        case TimeState.DURATION:
                            pass
                        case TimeState.SECONDS:
                            self._attr_elapsed_time = sv.value
                            self._attr_elapsed_time_last_updated = time.time()
                        case _:
                            self.logger.error("Unknown State Variable: %s", sv.name)
            case ServiceId.UPDATE:
                pass
            case _:
                self.logger.warning("Unhandled event for service id: %s", service.service_id)

        self.update_state()
        self.last_seen = time.time()
        # run when not Time event
        if service.service_id != ServiceId.TIME:
            self.mass.create_task(self._update_player())

    async def _update_player(self) -> None:
        """Update Linn/OpenHome Media Player."""
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

        supported_features: set[PlayerFeature] = set()
        supported_features.add(PlayerFeature.PLAY_MEDIA)
        supported_features.add(PlayerFeature.PAUSE)
        supported_features.add(PlayerFeature.NEXT_PREVIOUS)
        if self.profile:
            if self.profile.has_product_standby:
                supported_features.add(PlayerFeature.POWER)
            if (
                    self.profile.has_transport_seek_second_absolute
                    # or self.profile.has_playlist_seek_second_absolute
                    # or self.profile.has_radio_seek_second_absolute
            ):
                supported_features.add(PlayerFeature.SEEK)
            if self.profile.has_volume_mute:
                supported_features.add(PlayerFeature.VOLUME_MUTE)
            if self.profile.has_volume_set:
                supported_features.add(PlayerFeature.VOLUME_SET)
            if self.profile.has_product_set_source_index:
                supported_features.add(PlayerFeature.SELECT_SOURCE)

        self._attr_supported_features = supported_features

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player requires flow mode."""
        return False

    async def setup(self) -> bool:
        """Set up player in MA."""
        await self._device_connect()

        self.set_static_attributes()
        await self.mass.players.register_or_update(self)
        return True

    def set_static_attributes(self) -> None:
        """Set static attributes."""
        self._attr_needs_poll = True  # assume polling until successful subscription
        self._attr_poll_interval = 30
        self._set_player_features()

    async def poll_transport_state_variables(self):
        if self.profile.has_transport_state:
            await self.profile._async_poll_state_variables(Service.TRANSPORT, TransportState.TRANSPORT_STATE)
        else:
            await self.profile._async_poll_state_variables(Service.PRODUCT, ProductState.SOURCE_XML)
            await self.profile._async_poll_state_variables(Service.PRODUCT, ProductState.SOURCE_INDEX)
            await self.profile._async_poll_state_variables(Service.PLAYLIST, PlaylistState.TRANSPORT_STATE)
            await self.profile._async_poll_state_variables(Service.RADIO, RadioState.TRANSPORT_STATE)
            await self.profile._async_poll_state_variables(Service.RECEIVER, ReceiverState.TRANSPORT_STATE)

    async def poll_min_state_variables(self):
        """Poll a minimal set of state variables."""
        await self.profile._async_poll_state_variables(Service.PRODUCT, ProductState.STANDBY)
        await self.profile._async_poll_state_variables(Service.VOLUME, VolumeState.MUTE)
        await self.profile._async_poll_state_variables(Service.VOLUME, VolumeState.VOLUME)

    async def set_dynamic_attributes(self) -> None:
        """Set dynamic attributes."""

        logger = self.provider.logger.getChild(self.player_id)
        # TODO simplify to self.profile.available by adding to profile ???
        available = self.profile is not None and self.profile.device.available
        self._attr_available = available
        if not available:
            logger.warning("Player not available %s", self.display_name)
            return

        # await self.profile.async_update_state_variables()
        await self.poll_min_state_variables()
        await self.poll_transport_state_variables()

        self._attr_active_source = await self.profile.async_active_source_name()
        self.sources = await self.profile.async_visible_sources()

        self._attr_name = self.profile.name
        self._attr_powered = not self.profile.product_standby
        self._attr_volume_muted = self.profile.is_muted
        self._attr_volume_level = self.profile.volume
        self._attr_playback_state = self._transport_state_to_playback_state(self.profile.transport_state)
        self._attr_source_list = self._source_list_from_sources(self.sources)

        # TODO add other options modelled on DLNA
        # _playback_state = self._get_playback_state()
        # assert _playback_state is not None  # for type checking
        # self._attr_playback_state = _playback_state
        # etc.

    # endregion

    # region adapted from Demo Player
    async def on_config_updated(self) -> None:
        """Handle logic when the PlayerConfig is first loaded or updated."""
        # OPTIONAL

    @property
    def needs_poll(self) -> bool:
        """Return True if the player needs to be polled for state updates."""
        # MANDATORY
        # this should return True if the player needs to be polled for state updates,
        # If you player does not need to be polled, you can return False.
        return False

    @property
    def poll_interval(self) -> int:
        """Return the interval in seconds to poll the player for state updates."""
        return 5 if self._attr_playback_state == PlaybackState.PLAYING else 30

    async def get_config_entries(
            self,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""

        base_entries = await super().get_config_entries()
        config_entries: list[ConfigEntry] = [
            *base_entries,
        ]
        return config_entries

    # endregion

    # region COMMANDS
    @catch_request_errors
    async def power(self, powered: bool) -> None:
        """Handle POWER command on the player."""
        await self.profile.async_product_set_standby(not powered)
        self.update_state()

    @catch_request_errors
    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME SET command on the player."""
        await self.profile.async_volume_set(volume_level)

        logger = self.provider.logger.getChild(self.player_id)
        logger.debug(
            "Received VOLUME_SET command on player %s with level %s",
            self.display_name,
            volume_level,
        )
        # update the player state in the player manager
        self.update_state()

    @catch_request_errors
    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        await self.profile.async_volume_set_mute(muted)
        # OPTIONAL - required only if you specified PlayerFeature.VOLUME_MUTE
        # this method should send a volume mute command to the given player.
        logger = self.provider.logger.getChild(self.player_id)
        logger.debug(
            "Received VOLUME_MUTE command on player %s with muted %s",
            self.display_name,
            muted,
        )
        self.update_state()

    @catch_request_errors
    async def play(self) -> None:
        """Play command."""

        logger = self.provider.logger.getChild(self.player_id)
        logger.info("Received PLAY command on player %s", self.display_name)
        try:
            await self.profile.async_play()
        except UpnpError:
            logger.warning("Could not execute PLAY command on player %s", self.display_name)

        self.update_state()

    @catch_request_errors
    async def stop(self) -> None:
        """Stop command."""

        logger = self.provider.logger.getChild(self.player_id)
        logger.debug("Received STOP command on player %s", self.display_name)
        try:
            await self.profile.async_stop()
        except UpnpError:
            logger.warning("Could not execute STOP command on player %s", self.display_name)

        self.update_state()

    @catch_request_errors
    async def pause(self) -> None:
        """Pause command.

        If stream can not pause then stop (check transport-StreamInfo)
        """
        logger = self.provider.logger.getChild(self.player_id)
        logger.debug("Received PAUSE command on player %s", self.display_name)

        if can_pause := self.profile.get_state_variable_value(Service.TRANSPORT, TransportState.CAN_PAUSE) is None:
            await self.profile._async_poll_state_variables(Service.TRANSPORT, Transport.STREAM_INFO)
            can_pause = self.profile.get_state_variable_value(Service.TRANSPORT, TransportState.CAN_PAUSE)

        if can_pause:
            try:
                await self.profile.async_pause()
            except UpnpError:
                logger.warning("Could not execute PAUSE command on player %s", self.display_name)
        else:
            # just stop
            try:
                await self.profile.async_stop()
            except UpnpError:
                logger.warning("Could not execute STOP command on player %s", self.display_name)

        self.update_state()

    @catch_request_errors
    async def next_track(self) -> None:
        """Next command."""
        await self.profile.async_playlist_next()

    @catch_request_errors
    async def previous_track(self) -> None:
        """Previous command."""
        await self.profile.async_playlist_previous()

    @catch_request_errors
    async def seek(self, position: int) -> None:
        """SEEK command on the player."""

    @catch_request_errors
    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command.

        Configures metadata and URL and attempts to play the media.
        """
        logger = self.provider.logger.getChild(self.player_id)
        logger.info("Received PLAY_MEDIA command on player %s", self.display_name)

        if self.profile is None:
            logger.warning("play_media - no profile available for %s", self.display_name)
            return

        # always clear MA queue (by sending stop) first
        try:
            await self.profile.async_stop()
        except UpnpError as err:
            logger.warning("Could not execute STOP command on player %s - %s", self.display_name, err)

        didl_metadata = create_didl_metadata(media)
        url = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)

        self.set_current_media(uri=url, clear_all=True)
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_elapsed_time = -1

        if self.profile.has_source_type(ProductSourceType.RADIO):
            # Radio service offers an API to allow arbitrary URLs to be played.
            # flip source to Playlist to avoid buffering problem with Linn DSM
            await self.profile.async_product_set_source_index(0)
            await self.profile.async_radio_set_channel(url, didl_metadata)
            time.sleep(1)
            await self.profile.async_radio_play()
        else:
            # if no Radio available (e.g. BubbleUPnPserver) then revert to using Playlist
            logger.debug("play_media - using playlist")
            last_id = await self.profile.async_playlist_last_id()
            new_id = (await self.profile.async_playlist_insert(last_id, url, didl_metadata)).get("NewId")
            if new_id is not None:
                await self.profile.async_playlist_seek_id(new_id)

        self.update_state()

    @catch_request_errors
    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing of the next (queue) item on the player."""

    @catch_request_errors
    async def play_announcement(self, announcement: PlayerMedia, volume_level: int | None = None) -> None:
        """Handle (native) playback of an announcement on the player."""

    @catch_request_errors
    async def select_source(self, source_name: str) -> None:
        """Handle SELECT SOURCE command on the player.

        :param source_name: The name of the source to select, as defined by source_list.
        """
        new_source = next((x for x in self.source_list if x.name.lower() == source_name.lower()), None)
        if new_source:
            await self.profile.async_product_set_source_index(int(new_source.id))

    @catch_request_errors
    async def set_members(
            self,
            player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""

    async def poll(self) -> None:
        """Poll player for state updates."""
        # OPTIONAL - This is called by the Player Manager if the 'needs_poll' property is True.
        if not self.profile:
            if not self.force_poll:
                return
            try:
                await self._device_connect()
            except UpnpError as err:
                raise PlayerUnavailableError from err

        try:
            now = time.time()
            do_ping = self.force_poll or (now - self.last_seen) > 60
            with suppress(ValueError):
                await self.profile.async_update_state_variables(do_ping=do_ping)
            self.last_seen = now if do_ping else self.last_seen
        except UpnpError as err:
            self.logger.debug("Device unavailable: %r", err)
            await self._device_disconnect()
            raise PlayerUnavailableError from err
        finally:
            self.force_poll = False

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""

        self.logger.debug("Player %s unloaded", self.name)
        await super().on_unload()
        await self._device_disconnect()

    async def _device_disconnect(self) -> None:
        """Destroy connections to the device."""
        async with self.lock:
            if not self.profile:
                self.logger.debug("Disconnecting from device that's not connected")
                return

            self.logger.debug("Disconnecting from %s", self.profile.name)

            self.profile.on_event = None
            old_device = self.profile
            self.profile = None
            self.set_available(False)
            await old_device.async_unsubscribe_services()
        self.update_state()

    # endregion

    # region Linn/OpenHome Media specific helper functions
    @staticmethod
    def _source_list_from_sources(sources) -> list[PlayerSource]:
        """Return MusicAssistant source list from the Linn/OpenHome Media device list of sources."""
        player_source_list = []
        # passive: this source can not be selected/activated by MA/the user
        # can_play_pause: this source can be paused and resumed
        # can_seek: this source can be seeked
        # can_next_previous: this source can be skipped to next/previous item

        # defaults
        passive = False
        can_play_pause = False
        can_seek = False
        can_next_previous = False
        for source in sources:
            match source["Type"]:
                case ProductSourceType.PLAYLIST:
                    can_play_pause = True
                    can_seek = True
                    can_next_previous = True
                case ProductSourceType.RADIO:
                    can_play_pause = True
                case _:
                    pass

            source_entry = PlayerSource(
                id=str(source["Index"]),
                name=source["Name"],
                passive=passive,
                can_play_pause=can_play_pause,
                can_seek=can_seek,
                can_next_previous=can_next_previous,
            )
            player_source_list.append(source_entry)

        return player_source_list

    @staticmethod
    def _transport_state_to_playback_state(transport_state: str | None) -> PlaybackState:
        """Return MA playback state from Linn/OpenHome Media device transport state."""
        match transport_state:
            case TransportStateAllowedValues.PLAYING:
                return PlaybackState.PLAYING
            case TransportStateAllowedValues.PAUSED:
                return PlaybackState.PAUSED
            case TransportStateAllowedValues.STOPPED:
                return PlaybackState.IDLE
            case TransportStateAllowedValues.BUFFERING:
                return PlaybackState.UNKNOWN  # NOTE not ideal but would need MA update to match
            case TransportStateAllowedValues.WAITING:
                return PlaybackState.IDLE  # NOTE not ideal but would need MA update to match
            case _:
                return PlaybackState.UNKNOWN
    # endregion

    @staticmethod
    def get_mac_from_uuid(uuid: str) -> str | None:
        """Return a mac-address-like identifier from the UDN of the device."""
        uuid = uuid.removeprefix("uuid:")
        # extract text between first and last -
        mac_like = uuid[uuid.find("-") + 1:uuid.rfind("-")]
        mac_like = mac_like.replace("-", "")
        # Format string like a MAC address i.e. XX:XX:XX:XX:XX:XX
        mac_like = ":".join(mac_like[i: i + 2].upper() for i in range(0, 12, 2))
        if len(mac_like) == 17:
            return mac_like
        return None

    @staticmethod
    def is_valid_uuid(uuid_string):
        try:
            UUID(uuid_string)
            return True
        except ValueError:
            return False
