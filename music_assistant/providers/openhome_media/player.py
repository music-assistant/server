"""Linn/OpenHome Media Player implementation."""

from __future__ import annotations

# mypy: disable-error-code="attr-defined,union-attr"
import asyncio
import functools
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Concatenate
from urllib.parse import urlparse
from uuid import UUID
from xml.etree.ElementTree import Element, ParseError

import defusedxml.ElementTree as DefusedET
from async_upnp_client.exceptions import UpnpError, UpnpResponseError
from async_upnp_client.profiles.ohmedia import (
    InfoState,
    OhmDevice,
    Playlist,
    PlaylistState,
    ProductSourceType,
    ProductState,
    Radio,
    RadioState,
    Service,
    ServiceId,
    TimeState,
    Transport,
    TransportState,
    TransportStateAllowedValues,
    VolumeState,
)
from didl_lite import didl_lite
from music_assistant_models.enums import (
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.player import PlayerSource

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.upnp import create_didl_metadata
from music_assistant.models.player import DeviceInfo, Player
from music_assistant.providers.openhome_media.constants import (
    CONF_SELECT_DEVICE_SOURCE,
    CONF_SELECT_DEVICE_SOURCE_KEY,
    EXTERNAL,
    PLAYER_CONFIG_ENTRIES,
    PLAYLIST,
    RADIO,
    radio_option,
)
from music_assistant.providers.openhome_media.helpers import get_source_index_of_type

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpService, UpnpStateVariable
    from music_assistant_models.config_entries import ConfigEntry
    from music_assistant_models.player import PlayerMedia
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant.providers.openhome_media.provider import OpenHomePlayerProvider


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
        if not self.available and func.__name__ not in ("pause", "stop"):
            self.logger.warning("Device disappeared when trying to call %s", func.__name__)
            return None
        try:
            return await func(self, *args, **kwargs)
        except UpnpError as err:
            self.force_poll = True
            if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
                self.logger.exception("Error during call %s", func.__name__)
            else:
                self.logger.error("Error during call %s: %r", func.__name__, str(err))
        return None

    return wrapper


class OpenHomePlayer(Player):
    """Linn/OpenHome Media Player in Music Assistant."""

    _attr_type = PlayerType.PROTOCOL

    def __init__(
        self,
        provider: OpenHomePlayerProvider,
        player_id: str,
        description_url: str,
        device: OhmDevice | None = None,
    ) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)
        self.logger = self.provider.logger.getChild(self.player_id)
        self.profile: OhmDevice | None = device
        self.description_url: str = description_url
        # self.last_seen: float = time.time()
        self.last_seen: float | None = None
        self.lock = asyncio.Lock()  # Held when connecting or disconnecting the device
        self.force_poll = False
        self.state_update_pending: bool = False
        self.state_update_period_ms: int = 1000
        self.product_source_xml: Element | None = None  # state var converted from string
        self._observed_playback_state: PlaybackState | None = None
        self._playing_since: float | None = None
        self._attr_name: str = "Linn/OpenHome Media Player"
        self.player: Player | None = None
        self.active_queue: PlayerQueue | None = None

    def set_available(self, available: bool) -> None:
        """Set the availability of the player."""
        self._attr_available = available

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player requires flow mode."""
        return False

    @property
    def has_play_media(self) -> bool:
        """
        Look for usable Play action.

        play_media must select corresponding Play action
        :return: True if Play action found, otherwise False.
        """
        if self.profile.has_transport_play:
            return True
        if self.profile.device.has_service(Service.PLAYLIST):
            svc = self.profile.service(Service.PLAYLIST)
            if svc.has_action(Playlist.PLAY):
                return True
        if self.profile.device.has_service(Service.RADIO):
            svc = self.profile.service(Service.RADIO)
            if svc.has_action(Radio.PLAY):
                return True
        return False

    async def setup(self) -> bool:
        """
        Set up player in MA.

        :return: True if setup was successful, False if device should be ignored.
        """
        await self._device_connect()

        if self.profile and not self.has_play_media:
            self.logger.debug("Ignoring %s - no play capability", self.profile.name)
            return False

        self._attr_name = self.profile.name
        self.set_static_attributes()
        await self.mass.players.register_or_update(self)

        self.player = self.mass.players.get_player(self.player_id)
        assert self.player is not None
        self.active_queue = self.mass.players.get_active_queue(self.player)
        return True

    def set_static_attributes(self) -> None:
        """Set static attributes."""
        self._attr_poll_interval = self.poll_interval
        self._set_player_features()

    async def set_dynamic_attributes(self) -> None:
        """
        Update/set MA attributes from state variables.

        Use after poll has occurred
        When subscribed each update should be done by event handler
        """
        available = self.profile is not None and self.profile.device.available
        self._attr_available = available
        if not available:
            return
        assert self.profile is not None  # for type checking

        if self.force_poll:
            await self.profile.async_update_state_variables()  # poll all state variables

        self._attr_powered = not self.profile.product_standby
        self._attr_volume_muted = self.profile.is_muted

        # should a device report an unknown volume, retain unknown instead of collapsing to 0/unmuted
        volume_level = self.profile.volume
        self._attr_volume_level = int(volume_level) if volume_level is not None else None

        # playback state
        _playback_state = self._transport_state_to_playback_state(self.profile.transport_state)
        assert _playback_state is not None  # for type checking
        prev_playback_state = self._observed_playback_state
        self._observed_playback_state = _playback_state
        if _playback_state != PlaybackState.PLAYING:
            self._playing_since = None
        elif prev_playback_state not in (None, PlaybackState.PLAYING):
            self._playing_since = time.time()
        self._attr_playback_state = _playback_state

        # current media
        if self.profile.info_metadata is not None:
            metadata = didl_lite.from_xml_string(self.profile.info_metadata)[0]
        try:
            media_title = metadata.title
            media_artist = metadata.artist
            media_album = metadata.album
            media_image_url = metadata.albumArtURI
            res = metadata.res[0]
            media_duration = res.duration
        except ParseError as err:
            # drop the metadata but keep the player updated
            self.logger.debug(
                "Ignoring malformed media metadata from device %s: %s", self.display_name, err
            )
            media_title = media_artist = media_album = media_image_url = None
            media_duration = None

        _track_uri: str = str(self.profile.info_uri) if self.profile.info_uri is not None else ""
        self.set_current_media(
            uri=_track_uri,
            clear_all=True,
            title=media_title,
            artist=media_artist,
            album=media_album,
            image_url=media_image_url,
            duration=int(media_duration) if media_duration is not None else None,
        )

        if self.product_source_xml:
            self._attr_source_list = self._source_list_from_source_xml(self.product_source_xml)
        else:
            self._attr_source_list = []

    @property
    def poll_interval(self) -> int:
        """Return the interval in seconds to poll the player for state updates."""
        return (
            5 if self._attr_playback_state == PlaybackState.PLAYING else 30
        )  # _attr_poll_interval

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        config_entries: list[ConfigEntry] = [*PLAYER_CONFIG_ENTRIES]

        if (has_radio := self.profile.has_source_type(ProductSourceType.RADIO)) is None:
            await self.profile._async_poll_state_variables(Service.PRODUCT, ProductState.SOURCE_XML)
            has_radio = self.profile.has_source_type(ProductSourceType.RADIO)

        if has_radio:
            CONF_SELECT_DEVICE_SOURCE.default_value = RADIO
        else:  # most devices do have radio so opt out rather than opt in
            CONF_SELECT_DEVICE_SOURCE.default_value = PLAYLIST
            radio_option.disabled = True
            radio_option.disabled_reason = "Player does not support Radio"

        return config_entries

    # region COMMANDS
    @catch_request_errors
    async def power(self, powered: bool) -> None:
        """Handle POWER command on the player."""
        self.logger.debug("Command POWER %s for player %s", powered, self.display_name)
        await self.profile.async_product_set_standby(not powered)

    @catch_request_errors
    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME SET command on the player."""
        self.logger.debug(
            "Command VOLUME_SET level %s for player %s",
            volume_level,
            self.display_name,
        )

        await self.profile.async_volume_set(volume_level)
        self.update_state()

    @catch_request_errors
    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        self.logger.debug(
            "Command VOLUME_MUTE %s for player %s",
            muted,
            self.display_name,
        )

        await self.profile.async_volume_set_mute(muted)
        self.update_state()

    @catch_request_errors
    async def play(self) -> None:
        """Play command."""
        self.logger.debug("Command PLAY for player %s", self.display_name)
        try:
            await self.profile.async_play()
        except UpnpError:
            self.logger.warning("Could not execute PLAY command on player %s", self.display_name)

    @catch_request_errors
    async def stop(self) -> None:
        """Stop command."""
        self.logger.debug("Command STOP for player %s", self.display_name)
        self.cancel_next_media()
        try:
            await self.profile.async_stop()
        except UpnpError:
            self.logger.debug("Could not execute STOP command on player %s", self.display_name)

    @catch_request_errors
    async def pause(self) -> None:
        """Pause command."""
        self.logger.debug("Command PAUSE for player %s", self.display_name)

        # Get CAN_PAUSE capability, polling if necessary
        can_pause = self.profile.get_state_variable_value(
            Service.TRANSPORT, TransportState.CAN_PAUSE
        )
        if can_pause is None:
            await self.profile._async_poll_state_variables(Service.TRANSPORT, Transport.STREAM_INFO)
            can_pause = self.profile.get_state_variable_value(
                Service.TRANSPORT, TransportState.CAN_PAUSE
            )

        self.cancel_next_media()

        # If device supports pause, use pause; otherwise fall back to stop
        if can_pause:
            try:
                await self.profile.async_pause()
            except UpnpError:
                self.logger.warning(
                    "Could not execute PAUSE command on player %s", self.display_name
                )
        else:
            try:
                await self.profile.async_stop()
            except UpnpError:
                self.logger.warning(
                    "Could not execute STOP command on player %s", self.display_name
                )

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
            stream_id = self.profile.transport_stream_id
            if stream_id:
                await self.profile.async_transport_seek_second_absolute(stream_id, position)
                return
        active_source_type = await self.profile.async_active_source_type()
        if active_source_type == ProductSourceType.RADIO:
            await self.profile.async_radio_seek_second_absolute(position)
        else:
            await self.profile.async_playlist_seek_second_absolute(position)

    @catch_request_errors
    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        self.logger.debug("Command PLAY_MEDIA for player %s", self.display_name)

        # always stop any scheduled enqueue next media task
        await self.stop()

        url = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
        didl_metadata = create_didl_metadata(media, url)

        # title = media.title or media.uri
        # optimistically set the state here to help in case of a player
        # that is slow or failing to report state changes.
        prev_state = self._attr_playback_state
        self.set_current_media(uri=url, clear_all=True)
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()

        selected_play_source: str = await self.mass.config.get_player_config_value(
            self.player_id, CONF_SELECT_DEVICE_SOURCE_KEY
        )
        try:
            if selected_play_source == RADIO:
                # workaround to avoid buffering problem with Linn DSM - flip source away from Radio
                if self.profile.product_source_xml is not None:
                    radio_index = get_source_index_of_type(self.profile.product_source_xml, RADIO)
                    if radio_index and radio_index != 0:
                        new_index = 0
                await self.profile.async_product_set_source_index(new_index)
                await self.profile.async_radio_set_channel(url, didl_metadata)
                await asyncio.sleep(0.5)
                await self.profile.async_radio_play()
            else:  # use Playlist
                last_id = await self.profile.async_playlist_last_id()
                new_id = (
                    await self.profile.async_playlist_insert(last_id, url, didl_metadata)
                ).get("NewId")
                if new_id is not None:
                    await self.profile.async_playlist_seek_id(new_id)  # play track at new_id

        except Exception:
            self._attr_playback_state = prev_state  # rollback optimistic state
            raise
        self.update_state()

    @catch_request_errors
    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing of the next queue item on the player."""
        assert self.profile is not None  # for type checking
        track_url = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
        didl_metadata = create_didl_metadata(media, track_url)
        player = self.mass.players.get_player(self.player_id)
        active_queue_id = self.active_queue.queue_id
        queue = self.active_queue  # TODO swap permanently if it works
        selected_play_source: str = await self.mass.config.get_player_config_value(
            self.player_id, CONF_SELECT_DEVICE_SOURCE_KEY
        )
        if selected_play_source == RADIO:
            # schedule a play_media for when track has ended
            # need duration and elapsed time to calculate this
            remaining_time: float = 0.0
            if queue.current_item.duration:
                remaining_time = float(queue.current_item.duration) - queue.corrected_elapsed_time
                if remaining_time < 0:
                    remaining_time = queue.current_item.duration

            self.logger.warning("enqueue_next_media: remaining_time: %s", remaining_time)
            self.mass.call_later(
                remaining_time, self.play_media, media, task_id=f"ohm_enqueue_{active_queue_id}"
            )
        else:
            # append track to Playlist only if uri is different
            playlist_id = (await self.profile.async_playlist_id())["Value"]
            uri = (await self.profile.async_playlist_read(playlist_id)).get("Uri")

            if uri != track_url:
                last_id = await self.profile.async_playlist_last_id()
                self.logger.debug("enqueue_next_media add: %s", track_url)
                await self.profile.async_playlist_insert(last_id, track_url, didl_metadata)
        self.update_state()

    def cancel_next_media(self) -> None:
        """Cancel the next media scheduled task on the player."""
        assert self.profile is not None
        # active_queue = self.mass.player_queues.get_active_queue(self.player_id)
        active_queue = self.active_queue
        if active_queue is not None:
            self.mass.cancel_timer(task_id=f"ohm_enqueue_{active_queue.queue_id}")
            self.mass.cancel_task(task_id=f"ohm_next_track_{active_queue.queue_id}")

    @catch_request_errors
    async def select_source(self, source_name: str) -> None:
        """
        Handle SELECT SOURCE command on the player.

        :param source_name: The name of the source to select, as defined by source_list.
        """
        new_source = next(
            (x for x in self.source_list if x.name.lower() == source_name.lower()), None
        )
        if new_source:
            await self.profile.async_product_set_source_index(int(new_source.id))

    @property
    def source_list(self) -> list[PlayerSource]:
        """Return list of available (native) sources for this player."""
        return self._attr_source_list

    async def poll(self) -> None:
        """Poll player for all state variables (fallback mode only)."""
        # try to reconnect the device if the connection was lost
        if not self.profile:
            if not self.force_poll:
                return
            try:
                await self._device_connect()
            except UpnpError as err:
                raise PlayerUnavailableError from err
        elif self.profile.is_subscribed:
            self.force_poll = False
            return

        assert self.profile is not None
        try:
            now = time.time()
            if self.last_seen is None:
                do_ping = self.force_poll
            else:
                do_ping = self.force_poll or (now - self.last_seen) > 60

            with suppress(ValueError, ParseError):
                await self.profile.async_update_state_variables(do_ping=do_ping)
        except UpnpError as err:
            self.logger.debug("Device unavailable: %r", err)
            await self._device_disconnect()
            raise PlayerUnavailableError from err
        else:
            self.last_seen = now if do_ping else self.last_seen
        finally:
            self.force_poll = False

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        await self._device_disconnect()
        self.logger.debug("Player unloaded: %s", self.name)

    # endregion

    # region Linn/OpenHome Media specific helper functions
    @staticmethod
    def get_mac_from_udn(udn: str) -> str | None:
        """Return a mac-address-like string from the UDN of the device."""
        udn = udn.removeprefix("uuid:")
        mac_like = udn[udn.find("-") + 1 : udn.rfind("-")]
        mac_like = mac_like.replace("-", "")
        # Format string like a MAC address i.e. XX:XX:XX:XX:XX:XX
        mac_like = ":".join(mac_like[i : i + 2].upper() for i in range(0, 12, 2))
        if len(mac_like) == 17:
            return mac_like
        return None

    @staticmethod
    def is_valid_uuid(uuid_string: str | None) -> bool:
        """Check string is a valid UUID."""
        if uuid_string is not None:
            try:
                UUID(uuid_string)
                return True
            except ValueError:
                return False
        return False

    @staticmethod
    def _source_list_from_source_xml(source_xml: Element | None) -> list[PlayerSource]:
        """Convert source XML into MA source list."""
        player_source_list: list[PlayerSource] = []
        if isinstance(source_xml, Element):
            for _index, element in enumerate(source_xml):
                visible: str | None = element.findtext("Visible")
                if visible and visible.lower().strip() in ("true", "1"):
                    source_type = element.findtext("Type")
                    source_entry = PlayerSource(
                        id=EXTERNAL,  # this is a representative value from EXTERNAL_SOURCES
                        name=element.findtext("Name", default="Unknown"),
                        can_play_pause=bool(source_type in ("Playlist", "Radio")),
                        can_seek=bool(source_type == "Playlist"),
                        can_next_previous=bool(source_type == "Playlist"),
                        passive=False,  # all visible sources so any can be actively selected
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
                return PlaybackState.PLAYING  # consider as still playing
            case TransportStateAllowedValues.WAITING:
                return PlaybackState.PLAYING  # consider as still playing
            case _:
                return PlaybackState.UNKNOWN

    # endregion

    async def _device_connect(self) -> None:
        """Connect Linn/OpenHome Media Device."""
        self.logger.debug("Connecting to device at %s", self.description_url)

        async with self.lock:
            if self.profile:
                self.logger.debug("Trying to connect when device already connected")
                return

            # Connect to the base UPNP device
            if TYPE_CHECKING:
                assert isinstance(self.provider, OpenHomePlayerProvider)
            upnp_device = await self.provider.upnp_factory.async_create_device(self.description_url)
            self._attr_device_info = DeviceInfo(
                model=upnp_device.model_name,
                manufacturer=upnp_device.manufacturer,
            )
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
            except UpnpError as err:
                # Don't leave the device half-constructed
                self.profile.on_event = None
                self.profile = None
                self.logger.debug("Error while subscribing during device connect: %r", err)
                raise
            else:
                self.logger.debug(
                    "async_subscribe_services was successful %s", self._attr_device_info
                )
                # Identifiers in descending priority MAC_ADDRESS, UUID, IP_ADDRESS
                # MAC_ADDRESS is extracted from UUID if format of UDN is UUID-like
                # MAC_ADDRESS will be validated by player controller enrich_device_mac_address
                # OpenHome Media Software Player uses machine name so will be excluded
                assert self.profile is not None
                if OpenHomePlayer.is_valid_uuid(self.profile.device.udn):
                    # MAC address frequently part of udn - will be checked by arp later
                    mac_address = OpenHomePlayer.get_mac_from_udn(self.profile.device.udn)
                    self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac_address)

                    # Add UDN as identifier type uuid for identifying player across protocols
                    # Strip the "uuid:" prefix if present for proper matching
                    self._attr_device_info.add_identifier(
                        IdentifierType.UUID, self.profile.device.udn.removeprefix("uuid:")
                    )

                # Try to extract just the IP from the URL for device matching
                # All currently known examples have a higher priority identifier available
                ip_address = self.profile.device.presentation_url or self.description_url
                with suppress(ValueError):
                    parsed = urlparse(ip_address)
                    if parsed.hostname:
                        self._attr_device_info.add_identifier(
                            IdentifierType.IP_ADDRESS, parsed.hostname
                        )

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

    async def _deferred_update(self, poll_first: bool) -> None:
        """Defer update for a period."""
        await asyncio.sleep(self.state_update_period_ms / 1000.0)
        try:
            self.update_state()
        finally:
            self.state_update_pending = False

    def _handle_event(  # noqa: PLR0915
        self,
        service: UpnpService,
        state_variables: Sequence[UpnpStateVariable[Any]],
    ) -> None:
        """Handle changed state variables value event from Linn/OpenHome Media device."""
        if not state_variables:
            # Indicates a failure of subscription so revert to polling
            self.force_poll = True
            return

        # active_queue = self.mass.player_queues.get_active_queue(self.player_id)
        # active_queue_id = active_queue.queue_id if active_queue else None

        schedule_state_update: bool = False
        # Cases intended to be exhaustive but not fully implemented yet
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
                                schedule_state_update = True
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
                            self._attr_playback_state = self._transport_state_to_playback_state(
                                sv.value
                            )
                            schedule_state_update = True
                        case PlaylistState.REPEAT:
                            self._attr_repeat_state = sv.value
                            schedule_state_update = True
                        case PlaylistState.SHUFFLE:
                            self._attr_shuffle_state = sv.value
                            schedule_state_update = True
                        case PlaylistState.ID:
                            pass
                        case PlaylistState.ID_ARRAY:
                            pass
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
                        case ProductState.SOURCE_INDEX:
                            pass
                        case ProductState.SOURCE_XML:
                            schedule_state_update = True
                            try:
                                if sv.value:
                                    self.product_source_xml = DefusedET.fromstring(str(sv.value))
                            except ParseError, AttributeError, IndexError, KeyError, TypeError:
                                self.logger.debug("Unable to process Source XML %s", sv.value)
                            else:
                                self._attr_source_list = self._source_list_from_source_xml(
                                    self.product_source_xml
                                )
                        case _:
                            pass
            case ServiceId.RADIO:
                self.logger.debug("Radio Event: %s", state_variables)
                for sv in state_variables:
                    match sv.name:
                        case RadioState.TRANSPORT_STATE:
                            schedule_state_update = True
                            self._attr_playback_state = self._transport_state_to_playback_state(
                                sv.value
                            )
            case ServiceId.RECEIVER:
                pass
            case ServiceId.SENDER:
                pass
            case ServiceId.TRANSPORT:
                self.logger.debug("Transport Event: %s", state_variables)
                for sv in state_variables:
                    match sv.name:
                        case TransportState.TRANSPORT_STATE:
                            self._attr_playback_state = self._transport_state_to_playback_state(
                                sv.value
                            )
                            schedule_state_update = True
                        case TransportState.REPEAT:
                            self._attr_repeat_state = sv.value
                            schedule_state_update = True
                        case TransportState.SHUFFLE:
                            self._attr_shuffle_state = sv.value
                            schedule_state_update = True
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
                            self._attr_elapsed_time = sv.value
                            self._attr_elapsed_time_last_updated = time.time()
                            schedule_state_update = True
                        case _:
                            self.logger.error("Unknown State Variable: %s", sv.name)
            case ServiceId.VOLUME:
                self.logger.debug("Volume Event: %s", state_variables)
                for sv in state_variables:
                    match sv.name:
                        case VolumeState.MUTE:
                            self._attr_volume_muted = sv.value
                            schedule_state_update = True
                        case VolumeState.VOLUME:
                            self._attr_volume_level = (
                                int(sv.value) if sv.value is not None else None
                            )
                            schedule_state_update = True
                        case _:
                            pass
            case ServiceId.UPDATE:
                pass
            case _:
                self.logger.warning("Unhandled event for service id: %s", service.service_id)

        self.last_seen = time.time()
        if schedule_state_update and not self.state_update_pending:
            poll_first = False
            self.mass.create_task(
                self._deferred_update(poll_first),
                task_id=f"ohm_deferred_player_update_{self.player_id}",
            )
            self.state_update_pending = True

    async def _update_player(self, poll_first: bool = False) -> None:
        """
        Update Linn/OpenHome Media Player.

        :param poll_first: Refresh the device state before reading it, so that the
            position info belongs to the state that is about to be reported.
        """
        if poll_first:
            # an unavailable device is reported as such by the state update below
            with suppress(PlayerUnavailableError):
                await self.poll()
        prev_url = self._attr_current_media.uri if self._attr_current_media is not None else ""
        prev_state = self.state
        # await self.set_dynamic_attributes()
        current_url = self._attr_current_media.uri if self._attr_current_media is not None else ""
        current_state = self.state

        if (prev_url != current_url) or (prev_state != current_state):
            # fetch track details on state or url change
            self.force_poll = True
        try:
            self.update_state()
        except KeyError, TypeError:
            # at start the update might come faster than the config is initialized
            await asyncio.sleep(2)

        self.update_state()

    def _set_player_features(self) -> None:
        """Set Player Features based on config values and capabilities."""
        assert self.profile is not None  # for type checking
        supported_features: set[PlayerFeature] = set()
        if self.has_play_media:
            supported_features.add(PlayerFeature.PLAY_MEDIA)
            supported_features.add(PlayerFeature.ENQUEUE)
            supported_features.add(PlayerFeature.PAUSE)
            supported_features.add(PlayerFeature.NEXT_PREVIOUS)
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
