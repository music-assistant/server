"""DLNA Player."""

import asyncio
import functools
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Concatenate
from urllib.parse import urlparse

import defusedxml.ElementTree as DefusedET
from async_upnp_client.client import UpnpDevice, UpnpService, UpnpStateVariable
from async_upnp_client.exceptions import UpnpError, UpnpResponseError
from async_upnp_client.profiles.dlna import DmrDevice, TransportState
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.upnp import create_didl_metadata
from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.models.player import DeviceInfo, Player

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
    """DLNA Player.

    All DLNA players are considered generic protocol endpoints (PlayerType.PROTOCOL)
    and will be wrapped in a UniversalPlayer. Devices with native provider support
    (e.g., Sonos) are handled by their respective providers and will link to
    the DLNA player as a protocol output.
    """

    # All DLNA devices are generic protocol endpoints - no vendor has native DLNA support in MA
    _attr_type = PlayerType.PROTOCOL

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

    def set_available(self, available: bool) -> None:
        """Set the availability of the player."""
        self._attr_available = available

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
                    manufacturer=self.device.manufacturer,
                )
                # Add UDN (player_id) as UUID identifier for matching with other protocols
                # Strip the "uuid:" prefix if present for proper matching
                uuid_value = self.player_id
                if uuid_value.lower().startswith("uuid:"):
                    uuid_value = uuid_value[5:]
                self._attr_device_info.add_identifier(IdentifierType.UUID, uuid_value)
                # Try to extract MAC address from UUID
                # Many UPnP devices embed MAC in the last 12 chars of UUID
                # e.g., uuid:4d691234-444c-164e-1234-001f33eaacf1 -> 00:1f:33:ea:ac:f1
                mac_address = self._extract_mac_from_uuid(uuid_value)
                # Only add MAC address if it's valid (not 00:00:00:00:00:00)
                if mac_address and is_valid_mac_address(mac_address):
                    self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac_address)
                # Try to extract just the IP from the URL for matching
                ip_address = self.device.device.presentation_url or self.description_url
                with suppress(ValueError):
                    parsed = urlparse(ip_address)
                    if parsed.hostname:
                        self._attr_device_info.add_identifier(
                            IdentifierType.IP_ADDRESS, parsed.hostname
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
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
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
        supported_features: set[PlayerFeature] = set()

        # Only add PLAY_MEDIA if the device actually supports playback
        # Passive speakers (like stereo pair satellites) don't have play capability
        if self.device.has_play_media:
            supported_features.add(PlayerFeature.PLAY_MEDIA)
            # there is no way to check if a dlna player support enqueuing
            # so we simply assume it does and if it doesn't
            # you'll find out at playback time and we log a warning
            supported_features.add(PlayerFeature.ENQUEUE)
            supported_features.add(PlayerFeature.GAPLESS_PLAYBACK)

        if self.device.has_volume_level:
            supported_features.add(PlayerFeature.VOLUME_SET)
        if self.device.has_volume_mute:
            supported_features.add(PlayerFeature.VOLUME_MUTE)
        if self.device.has_pause:
            supported_features.add(PlayerFeature.PAUSE)
        self._attr_supported_features = supported_features

    async def setup(self) -> bool:
        """Set up player in MA.

        :return: True if setup was successful, False if device should be ignored.
        """
        await self._device_connect()

        if self.device and not self.device.has_play_media:
            self.logger.debug("Ignoring %s - no play capability", self.device.name)
            return False

        if self.device and await self._is_sonos_passive_speaker():
            self.logger.debug("Ignoring %s - passive stereo pair speaker", self.device.name)
            return False

        self.set_static_attributes()
        await self.mass.players.register_or_update(self)
        return True

    async def _is_sonos_passive_speaker(self) -> bool:
        """Check if this is a Sonos passive stereo pair speaker.

        Queries the device's own topology. If that returns 403, the device is
        considered passive (passive satellites and speakers with UPnP disabled
        block topology queries). If successful, checks for Invisible="1" attribute.
        """
        if not self.device:
            return False

        manufacturer = (self.device.manufacturer or "").lower()
        if "sonos" not in manufacturer:
            return False

        # Extract base UUID (strip "uuid:" prefix and "_MR" suffix)
        our_uuid = self.player_id.removeprefix("uuid:").removesuffix("_MR")

        # Query this device's topology
        upnp_device = self.device.profile_device.root_device
        result = await self._check_invisible_in_topology(upnp_device, our_uuid)

        # Return the result: True if passive/403, False if active or check failed
        return result if result is not None else False

    async def _check_invisible_in_topology(
        self, upnp_device: UpnpDevice, our_uuid: str
    ) -> bool | None:
        """Check if our UUID is marked as Invisible in the topology.

        :param upnp_device: UPnP device to query
        :param our_uuid: Our device UUID to search for
        :return: True if invisible/403 error, False if visible, None if check failed
        """
        zone_topology_service = None
        for service in upnp_device.all_services:
            if "ZoneGroupTopology" in service.service_type:
                zone_topology_service = service
                break

        if not zone_topology_service:
            return None

        try:
            action = zone_topology_service.action("GetZoneGroupState")
            if not action:
                return None

            result = await action.async_call()
            zone_group_state_xml = result.get("ZoneGroupState", "")
            if not zone_group_state_xml:
                return None

            root = DefusedET.fromstring(zone_group_state_xml)
            for member in root.iter("ZoneGroupMember"):
                if member.get("UUID", "").upper() == our_uuid.upper():
                    return str(member.get("Invisible", "0")) == "1"

        except UpnpResponseError as err:
            # 403 Forbidden indicates passive satellite (blocks topology queries)
            if "403" in str(err):
                self.logger.debug(
                    "Sonos device %s returned 403 - treating as passive satellite",
                    our_uuid,
                )
                return True
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Error checking Sonos zone topology: %s",
                err,
            )
        except (UpnpError, DefusedET.ParseError) as err:
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Error checking Sonos zone topology: %s",
                err,
            )

        return None

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
        return [*PLAYER_CONFIG_ENTRIES]

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
        url = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
        await self.device.async_set_transport_uri(url, title, didl_metadata)
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
        url = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
        try:
            await self.device.async_set_next_transport_uri(url, title, didl_metadata)
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
            await self._device_disconnect()
            raise PlayerUnavailableError from err
        finally:
            self.force_poll = False

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        await self._device_disconnect()

    async def _device_disconnect(self) -> None:
        """Destroy connections to the device."""
        async with self.lock:
            if not self.device:
                self.logger.debug("Disconnecting from device that's not connected")
                return

            self.logger.debug("Disconnecting from %s", self.device.name)

            self.device.on_event = None
            old_device = self.device
            self.device = None
            self.set_available(False)
            await old_device.async_unsubscribe_services()

    @staticmethod
    def _extract_mac_from_uuid(uuid_value: str) -> str | None:
        """Try to extract MAC address from UUID.

        Many UPnP devices embed the MAC address in the last 12 hex characters of the UUID.
        E.g., uuid:4d691234-444c-164e-1234-001f33eaacf1 -> 00:1f:33:ea:ac:f1

        :param uuid_value: The UUID string (without 'uuid:' prefix).
        :return: MAC address string in XX:XX:XX:XX:XX:XX format, or None if not extractable.
        """
        # Remove dashes and get last 12 hex characters
        hex_chars = uuid_value.replace("-", "")
        if len(hex_chars) < 12:
            return None

        mac_hex = hex_chars[-12:]

        # Validate it looks like a MAC (all hex characters)
        try:
            int(mac_hex, 16)
        except ValueError:
            return None

        # Check if it could be a valid MAC (not all zeros or all ones)
        if mac_hex in ("000000000000", "ffffffffffff", "FFFFFFFFFFFF"):
            return None

        # Format as XX:XX:XX:XX:XX:XX
        return ":".join(mac_hex[i : i + 2].upper() for i in range(0, 12, 2))
