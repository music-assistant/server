"""Linn/OpenHome Media Player implementation."""

from __future__ import annotations

import asyncio
import defusedxml.ElementTree as DefusedET
import functools
import time

from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Concatenate
from urllib.parse import urlparse
from uuid import UUID
from xml.etree.ElementTree import Element, ParseError

from async_upnp_client.client import UpnpService, UpnpStateVariable
from async_upnp_client.exceptions import UpnpError, UpnpResponseError
from async_upnp_client.profiles.ohmedia import (
    InfoState,
    OhmDevice,
    PlaylistState,
    ProductSourceType,
    ProductState,
    RadioState,
    ServiceId,
    TimeState,
    Transport,
    TransportState,
    TransportStateAllowedValues,
    VolumeState,
    Service,
)

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
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
            self._attr_needs_poll = True
            if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
                self.logger.exception("Error during call %s: %r", func.__name__, err)
            else:
                self.logger.error("Error during call %s: %r", func.__name__, str(err))
        return None

    return wrapper


class OpenHomePlayer(Player):
    """Linn/OpenHome Media Player in Music Assistant."""

    def __init__(
        self,
        provider: "OpenHomePlayerProvider",
        player_id: str,
        description_url: str,
        device: OhmDevice | None = None,
    ) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)
        self.profile: OhmDevice | None = device
        self.description_url: str = description_url
        self.last_seen: float | None = None
        self.lock = asyncio.Lock()  # Held when connecting or disconnecting the device
        self.state_update_pending: bool = False
        self.state_update_period_ms: int = 1000
        self.product_source_xml: Element | None = None  # state var converted from string
        # overrides
        self._attr_type: PlayerType = PlayerType.PROTOCOL
        self._attr_name: str = f"Linn/OpenHome Media Player {player_id}"  # update when connected

    def set_available(self, available: bool) -> None:
        """Set the availability of the player."""
        self._attr_available = available

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player requires flow mode."""
        return False

    async def setup(self) -> bool:
        """Set up player in MA."""
        logger = self.provider.logger.getChild(self.player_id)
        logger.debug("Setup player %s", self.player_id)
        await self._device_connect()
        self._set_player_features()
        self._set_attributes()
        await self.mass.players.register_or_update(self)
        return True

    @property
    def poll_interval(self) -> int:
        """Return the interval in seconds to poll the player for state updates."""
        return 5 if self._attr_playback_state == PlaybackState.PLAYING else 30 # _attr_poll_interval

    async def get_config_entries(
        self,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        base_entries = await super().get_config_entries()
        config_entries: list[ConfigEntry] = [
            *base_entries,
        ]
        return config_entries

    # region COMMANDS
    @catch_request_errors
    async def power(self, powered: bool) -> None:
        """Handle POWER command on the player."""
        logger = self.provider.logger.getChild(self.player_id)
        logger.debug(
            "Command POWER %s for player %s",
            powered,
            self.display_name
        )

        await self.profile.async_product_set_standby(not powered)

    @catch_request_errors
    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME SET command on the player."""

        logger = self.provider.logger.getChild(self.player_id)
        logger.debug(
            "Command VOLUME_SET level %s for player %s",
            volume_level,
            self.display_name,
        )

        await self.profile.async_volume_set(volume_level)
        self.update_state()

    @catch_request_errors
    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""

        logger = self.provider.logger.getChild(self.player_id)
        logger.debug(
            "Command VOLUME_MUTE %s for player %s",
            muted,
            self.display_name,
        )

        await self.profile.async_volume_set_mute(muted)
        self.update_state()

    @catch_request_errors
    async def play(self) -> None:
        """Play command."""

        logger = self.provider.logger.getChild(self.player_id)
        logger.debug("Command PLAY for player %s", self.display_name)
        try:
            await self.profile.async_play()
        except UpnpError:
            logger.warning("Could not execute PLAY command on player %s", self.display_name)

    @catch_request_errors
    async def stop(self) -> None:
        """Stop command."""

        logger = self.provider.logger.getChild(self.player_id)
        logger.debug("Command STOP for player %s", self.display_name)
        try:
            await self.profile.async_stop()
        except UpnpError:
            logger.warning("Could not execute STOP command on player %s", self.display_name)

    @catch_request_errors
    async def pause(self) -> None:
        """Pause command."""

        logger = self.provider.logger.getChild(self.player_id)
        logger.debug("Command PAUSE for player %s", self.display_name)

        # Get CAN_PAUSE capability, polling if necessary
        can_pause = self.profile.get_state_variable_value(Service.TRANSPORT, TransportState.CAN_PAUSE)
        if can_pause is None:
            await self.profile._async_poll_state_variables(Service.TRANSPORT, Transport.STREAM_INFO)
            can_pause = self.profile.get_state_variable_value(Service.TRANSPORT, TransportState.CAN_PAUSE)

        # If device supports pause, use pause; otherwise fall back to stop
        if can_pause:
            try:
                await self.profile.async_pause()
            except UpnpError:
                logger.warning("Could not execute PAUSE command on player %s", self.display_name)
        else:
            try:
                await self.profile.async_stop()
            except UpnpError:
                logger.warning("Could not execute STOP command on player %s", self.display_name)

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
        if self.profile.has_transport_seek_second_absolute:
            await self.profile.async_transport_seek_absolute(position)
        else:
            if self.profile.active_source == ProductSourceType.RADIO:
                await self.profile.async_radio_seek_second_absolute(position)
            else:
                await self.profile.async_playlist_seek_second_absolute(position)

    @catch_request_errors
    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""

        logger = self.provider.logger.getChild(self.player_id)
        logger.debug("Command PLAY_MEDIA for player %s", self.display_name)

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
            # must flip source to Playlist to avoid buffering problem with Linn DSM
            await self.profile.async_product_set_source_index(0)
            await self.profile.async_radio_set_channel(url, didl_metadata)
            await asyncio.sleep(1)
            await self.profile.async_radio_play()
        else:
            # if no Radio available (e.g. BubbleUPnP Server) then revert to using Playlist
            logger.debug("play_media - using playlist")
            last_id = await self.profile.async_playlist_last_id()
            new_id = (await self.profile.async_playlist_insert(last_id, url, didl_metadata)).get("NewId")
            if new_id is not None:
                await self.profile.async_playlist_seek_id(new_id)

    @catch_request_errors
    async def select_source(self, source_name: str) -> None:
        """
        Handle SELECT SOURCE command on the player.

        :param source_name: The name of the source to select, as defined by source_list.
        """
        new_source = next((x for x in self.source_list if x.name.lower() == source_name.lower()), None)
        if new_source:
            await self.profile.async_product_set_source_index(int(new_source.id))

    @property
    def source_list(self) -> list[PlayerSource]:
        """Return list of available (native) sources for this player."""
        return self._attr_source_list

    async def poll(self) -> None:
        """Poll player for all state variables (fallback mode only)."""

        logger = self.provider.logger.getChild(self.player_id)

        if self.profile.is_subscribed():
            self._attr_needs_poll = False
            return

        now: float | int = time.time()
        if self.last_seen is None:
            do_ping = True
        else:
            do_ping: bool = (now - self.last_seen) > 60

        try:
            with suppress(ValueError, ParseError):
                await self.profile.async_update_state_variables(do_ping=do_ping)
        except UpnpError as err:
            logger.debug("Device unavailable: %r", err)
            await self._device_disconnect()
            raise PlayerUnavailableError from err
        else:
            self.last_seen = now
        finally:
            self._attr_needs_poll = False

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        logger = self.provider.logger.getChild(self.player_id)
        await super().on_unload()
        await self._device_disconnect()
        logger.debug("Player unloaded: %s", self.name)
    # endregion

    # region Linn/OpenHome Media specific helper functions
    @staticmethod
    def get_mac_from_uuid(uuid: str) -> str | None:
        """Return a mac-address-like identifier from the UDN of the device."""
        uuid = uuid.removeprefix("uuid:")
        mac_like = uuid[uuid.find("-") + 1 : uuid.rfind("-")]
        mac_like = mac_like.replace("-", "")
        # Format string like a MAC address i.e. XX:XX:XX:XX:XX:XX
        mac_like = ":".join(mac_like[i : i + 2].upper() for i in range(0, 12, 2))
        if len(mac_like) == 17:
            return mac_like
        return None

    @staticmethod
    def is_valid_uuid(uuid_string: str) -> bool:
        if uuid_string is not None:
            try:
                UUID(uuid_string)
                return True
            except ValueError:
                return False
        return False

    @staticmethod
    def _source_list_from_source_xml(source_xml: Element) -> list[PlayerSource]:

        player_source_list: list[PlayerSource] = []
        if isinstance(source_xml, Element):
            for index, element in enumerate(source_xml):
                visible: str | None = element.findtext("Visible")
                if visible and visible.lower().strip() in ("true", "1"):
                    source_type = element.findtext("Type")
                    source_entry = PlayerSource(
                        id = str(index),
                        name = element.findtext("Name", default="Unknown"),
                        can_play_pause = True if source_type in ("Playlist", "Radio") else False,
                        can_seek = True if source_type in ("Playlist",) else False,
                        can_next_previous = True if source_type in ("Playlist",) else False,
                        passive = False,  # visible sources only so can be selected
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
                return PlaybackState.UNKNOWN  # NOTE not ideal but would need MA update to accommodate
            case TransportStateAllowedValues.WAITING:
                return PlaybackState.IDLE  # NOTE not ideal but would need MA update to accommodate
            case _:
                return PlaybackState.UNKNOWN
    # endregion

    async def _device_connect(self) -> None:
        """Connect Linn/OpenHome Media Device."""

        logger = self.provider.logger.getChild(self.player_id)
        logger.debug("Connecting to device at %s", self.description_url)

        async with self.lock:
            if self.profile:
                logger.debug("Trying to connect when device already connected")
                return

            # Connect to the base UPNP device
            if TYPE_CHECKING:
                assert isinstance(self.provider, OpenHomePlayerProvider)
            upnp_device = await self.provider.upnp_factory.async_create_device(self.description_url)

            # Create profile wrapper
            if OhmDevice.is_profile_device(upnp_device):
                self.profile = OhmDevice(upnp_device, self.provider.notify_server.event_handler)
            else:
                logger.debug("Device is not an OpenHome Profile: %s", upnp_device)
                return

            # Subscribe to event notifications
            try:
                self.profile.on_event = self._handle_event
                await self.profile.async_subscribe_services(auto_resubscribe=True)
            except UpnpResponseError as err:
                # Device rejected subscription request.
                # This is OK, variables will be polled instead.
                logger.debug("Device rejected subscription: %r", err)
                self._attr_needs_poll = True
            except UpnpError as err:
                # Don't leave the device half-constructed
                self.profile.on_event = None
                self.profile = None
                logger.debug("Error while subscribing during device connect: %r", err)
                raise
            else:
                # async_subscribe_services was successful, update device info
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
                        self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, parsed.hostname)

            if self._attr_needs_poll:
                await self.profile.async_update_state_variables() # poll all state variables

            self.product_source_xml = (await self.profile.async_product_source_xml()).get('Value')
            if self.product_source_xml:
                self._attr_source_list = self._source_list_from_source_xml(self.product_source_xml)

            try:
                self.update_state()
            except (KeyError, TypeError):
                # at start the update might come faster than the config is initialized
                logger.debug("State update failed during device connect, retrying after delay")
                await asyncio.sleep(2)
                self.update_state()

    async def _device_disconnect(self) -> None:
        """Destroy connections to the device."""

        logger = self.provider.logger.getChild(self.player_id)
        async with self.lock:
            if not self.profile:
                logger.debug("Disconnecting from device that's not connected")
                return

            logger.debug("Disconnecting from %s", self.profile.name)

            self.profile.on_event = None
            old_device = self.profile
            self.profile = None
            self.set_available(False)
            await old_device.async_unsubscribe_services()
        self.update_state()

    async def _deferred_state_update(self) -> None:
        """Defer state update for a period."""

        await asyncio.sleep(self.state_update_period_ms / 1000.0)
        try:
            self.update_state()
        finally:
            self.state_update_pending = False

    def _handle_event(
        self,
        service: UpnpService,
        state_variables: Sequence[UpnpStateVariable[Any]],
    ) -> None:
        """Handle changed state variables value event from Linn/OpenHome Media device."""

        logger = self.provider.logger.getChild(self.player_id)
        if not state_variables:
            # Indicates a failure of subscription so revert to polling
            self._attr_needs_poll = True
            return

        active_queue = self.mass.player_queues.get_active_queue(self.player_id)
        if active_queue:
            active_queue_id = active_queue.queue_id
        else:
            active_queue_id = None

        schedule_state_update: bool = False
        # Cases intended to be exhaustive but not fully implemented yet
        match service.service_id:
            case ServiceId.CREDENTIALS:
                pass
            case ServiceId.INFO:
                logger.debug("Info Event: %s", service.service_id)
                for sv in state_variables:
                    logger.debug("Info Event: %s %s", sv.name, sv.value)
                    match sv.name:
                        case InfoState.DURATION:
                            if self._attr_current_media:
                                schedule_state_update = True
                                self._attr_current_media.duration = sv.value
                        case _:
                            pass
            case ServiceId.PINS:
                pass
            case ServiceId.PLAYLIST:
                logger.debug("Playlist Event: %s", state_variables)
                for sv in state_variables:
                    match sv.name:
                        case PlaylistState.TRANSPORT_STATE:
                            schedule_state_update = True
                            self._attr_playback_state = self._transport_state_to_playback_state(sv.value)
                        case PlaylistState.REPEAT:
                            if active_queue_id is not None:
                                schedule_state_update = True
                                self._attr_repeat_state = sv.value
                        case PlaylistState.SHUFFLE:
                            if active_queue_id is not None:
                                schedule_state_update = True
                                self._attr_shuffle_state = sv.value
                        case PlaylistState.ID:
                            pass
                        case PlaylistState.ID_ARRAY:
                            pass
                        case PlaylistState.TRACKS_MAX:
                            pass
                        case PlaylistState.PROTOCOL_INFO:
                            pass
                        case _:
                            logger.warning("Unhandled Playlist State Variable %s", sv.name)
            case ServiceId.PRODUCT:
                logger.debug("Product Event: %s", state_variables)
                for sv in state_variables:
                    match sv.name:
                        case ProductState.SOURCE_INDEX:
                            try:
                                if 0 <= sv.value < len(self.product_source_xml):
                                    schedule_state_update = True
                                    self._attr_active_source = self.product_source_xml[sv.value][0].text
                            except (ParseError, AttributeError, IndexError, KeyError, TypeError):
                                logger.debug("Unable to process Product source index %s", sv.value)
                        case ProductState.SOURCE_XML:
                            schedule_state_update = True
                            try:
                                self.product_source_xml = DefusedET.fromstring(sv.value)
                            except (ParseError, AttributeError, IndexError, KeyError, TypeError):
                                logger.debug("Unable to process Source XML %s", sv.value)
                            else:
                                self._attr_source_list = self._source_list_from_source_xml(self.product_source_xml)
                        case _:
                            pass
            case ServiceId.RADIO:
                logger.debug("Radio Event: %s", state_variables)
                for sv in state_variables:
                    match sv.name:
                        case RadioState.TRANSPORT_STATE:
                            schedule_state_update = True
                            self._attr_playback_state = self._transport_state_to_playback_state(sv.value)
            case ServiceId.RECEIVER:
                pass
            case ServiceId.SENDER:
                pass
            case ServiceId.TRANSPORT:
                logger.debug("Transport Event: %s", state_variables)
                for sv in state_variables:
                    match sv.name:
                        case TransportState.TRANSPORT_STATE:
                            schedule_state_update = True
                            self._attr_playback_state = self._transport_state_to_playback_state(sv.value)
                        case TransportState.REPEAT:
                            if active_queue_id is not None:
                                schedule_state_update = True
                                self._attr_repeat_state = sv.value
                        case TransportState.SHUFFLE:
                            if active_queue_id is not None:
                                schedule_state_update = True
                                self._attr_shuffle_state = sv.value
                        case _:
                            pass
            case ServiceId.VOLUME:
                logger.debug("Volume Event: %s", state_variables)
                for sv in state_variables:
                    match sv.name:
                        case VolumeState.MUTE:
                            schedule_state_update = True
                            self._attr_volume_muted = sv.value
                        case VolumeState.VOLUME:
                            schedule_state_update = True
                            self._attr_volume_level = sv.value
                        case _:
                            pass
            case ServiceId.TIME:
                for sv in state_variables:
                    match sv.name:
                        case TimeState.TRACK_COUNT:
                            pass
                        case TimeState.DURATION:
                            pass
                        case TimeState.SECONDS:
                            schedule_state_update = True
                            self._attr_elapsed_time = sv.value
                            self._attr_elapsed_time_last_updated = time.time()
                        case _:
                            logger.error("Unknown State Variable: %s", sv.name)
            case ServiceId.UPDATE:
                pass
            case _:
                logger.warning("Unhandled event for service id: %s", service.service_id)

        self.last_seen = time.time()
        if schedule_state_update and not self.state_update_pending:
            asyncio.create_task(self._deferred_state_update())
            self.state_update_pending = True

    def _set_player_features(self) -> None:
        """Set Player Features based on config values and capabilities."""

        supported_features: set[PlayerFeature] = set()
        supported_features.add(PlayerFeature.PLAY_MEDIA)
        supported_features.add(PlayerFeature.PAUSE)
        supported_features.add(PlayerFeature.NEXT_PREVIOUS)
        if self.profile:
            if self.profile.has_product_standby:
                supported_features.add(PlayerFeature.POWER)
            if self.profile.has_transport_seek_second_absolute:
                supported_features.add(PlayerFeature.SEEK)
            if self.profile.has_volume_mute:
                supported_features.add(PlayerFeature.VOLUME_MUTE)
            if self.profile.has_volume_set:
                supported_features.add(PlayerFeature.VOLUME_SET)
            if self.profile.has_product_set_source_index:
                supported_features.add(PlayerFeature.SELECT_SOURCE)

        self._attr_supported_features = supported_features

    def _set_attributes(self) -> None:
        """Update/set MA attributes from state variables."""

        self._attr_name = self.profile.name
        self._attr_powered = not self.profile.product_standby
        self._attr_volume_muted = self.profile.is_muted
        self._attr_volume_level = self.profile.volume
        self._attr_playback_state = self._transport_state_to_playback_state(self.profile.transport_state)
        if self.product_source_xml:
            self._attr_source_list = self._source_list_from_source_xml(self.product_source_xml)
        else:
            self._attr_source_list = []
