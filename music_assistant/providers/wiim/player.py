"""Wiim Player implementation."""

from __future__ import annotations

import asyncio
import time
import typing
from typing import TYPE_CHECKING

from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo
from wiim import PlayingStatus, WiimDevice
from wiim.exceptions import WiimDeviceException, WiimRequestException
from wiim.models import WiimGroupRole

from music_assistant.helpers.upnp import create_didl_metadata_str
from music_assistant.models.player import Player, PlayerMedia

from .constants import (
    INPUT_MODE_SOURCES,
    PASSIVE_SOURCES,
    SOURCE_AIRPLAY,
    SOURCE_ID_TO_INPUT_MODE,
    SOURCE_SPOTIFY,
    SOURCE_UNKNOWN,
)

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpService, UpnpStateVariable

    from .provider import WiimProvider

SDK_TO_MA_STATE: dict[PlayingStatus, PlaybackState] = {
    PlayingStatus.PLAYING: PlaybackState.PLAYING,
    PlayingStatus.PAUSED: PlaybackState.PAUSED,
    PlayingStatus.STOPPED: PlaybackState.IDLE,
    PlayingStatus.LOADING: PlaybackState.IDLE,
}


class WiimPlayer(Player):
    """Wiim Player in Music Assistant."""

    def __init__(self, provider: WiimProvider, player_id: str, device: WiimDevice) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)

        # init some static variables
        self._attr_name = device.name
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PAUSE,
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.SELECT_SOURCE,
        }
        self._attr_can_group_with = {provider.instance_id}
        self.device = device
        self._wiim_controller = provider.wiim_controller

        self._attr_device_info = DeviceInfo(
            model=device.model_name,
            manufacturer=device.manufacturer or "WiiM",
            software_version=device.firmware_version,
        )
        self._attr_device_info.add_identifier(IdentifierType.UUID, player_id)
        if device.ip_address:
            self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, device.ip_address)

        self.current_uri: str | None = None

        device.general_event_callback = self._handle_sdk_general_device_update
        device.rendering_control_event_callback = self._handle_sdk_rendering_control_event
        device.av_transport_event_callback = self._handle_sdk_av_transport_event
        device.play_queue_event_callback = self._handle_sdk_play_queue_event

    async def setup(self) -> None:
        """Handle logic when the player is set up in the Player controller."""
        for mode_name in self.device.supported_input_modes:
            if mode_name in INPUT_MODE_SOURCES:
                self._attr_source_list.append(INPUT_MODE_SOURCES[mode_name])

        self._attr_source_list.append(PASSIVE_SOURCES[SOURCE_AIRPLAY])
        self._attr_source_list.append(PASSIVE_SOURCES[SOURCE_SPOTIFY])
        self._attr_source_list.append(PASSIVE_SOURCES[SOURCE_UNKNOWN])

    def _handle_sdk_general_device_update(self, device: WiimDevice) -> None:
        """Handle general updates from the SDK (availability changes)."""
        if not device.available:
            self.logger.debug("Device %s became unavailable", self._attr_name)
            self._update_ma_state_from_sdk_cache()
            return

        if device.supports_http_api:
            self.logger.debug("Device %s available, ensuring subscriptions", self._attr_name)
            asyncio.create_task(self._ensure_subscriptions_and_update())
        else:
            self._update_ma_state_from_sdk_cache()

    async def _ensure_subscriptions_and_update(self) -> None:
        """Re-subscribe to UPnP events and update state."""
        try:
            await self.device.ensure_subscriptions()
        except (WiimDeviceException, WiimRequestException) as err:
            self.logger.warning("Failed to re-subscribe for %s: %s", self._attr_name, err)
        self._update_ma_state_from_sdk_cache()

    def _handle_sdk_av_transport_event(
        self, service: UpnpService, state_variables: list[UpnpStateVariable[typing.Any]]
    ) -> None:
        """Handle AVTransport events from the SDK."""
        event_data = self.device.event_data

        if "TransportState" in event_data:
            try:
                sdk_status = PlayingStatus(event_data["TransportState"])
            except ValueError:
                self.logger.warning("Unknown TransportState: %s", event_data["TransportState"])
            else:
                self.device.playing_status = sdk_status
                if sdk_status == PlayingStatus.STOPPED:
                    self.device.current_position = 0
                    self.device.current_track_duration = 0
                elif sdk_status in {PlayingStatus.PAUSED, PlayingStatus.PLAYING}:
                    asyncio.create_task(self._sync_position())

        self._update_ma_state_from_sdk_cache()

    async def _sync_position(self) -> None:
        """Sync duration and position from the device."""
        try:
            await self.device.sync_device_duration_and_position()
        except (WiimDeviceException, WiimRequestException) as err:
            self.logger.debug("Failed to sync position for %s: %s", self._attr_name, err)

    def _handle_sdk_rendering_control_event(
        self, service: UpnpService, state_variables: list[UpnpStateVariable[typing.Any]]
    ) -> None:
        """Handle RenderingControl events from the SDK."""
        self._update_ma_state_from_sdk_cache()

    def _handle_sdk_play_queue_event(
        self, service: UpnpService, state_variables: list[UpnpStateVariable[typing.Any]]
    ) -> None:
        """Handle PlayQueue events from the SDK."""
        self._update_ma_state_from_sdk_cache()

    def _handle_command_error(
        self, action: str, err: WiimDeviceException | WiimRequestException
    ) -> None:
        """Handle a command error by marking the device unavailable."""
        self.logger.warning("Command '%s' failed on %s: %s", action, self._attr_name, err)
        self._attr_available = False
        self.update_state()

    @property
    def needs_poll(self) -> bool:
        """Return if the player needs to be polled for state updates."""
        return False

    async def select_source(self, source: str) -> None:
        """Handle SELECT SOURCE command on the player.

        :param source: The source(id) to select, as defined in the source_list.
        """
        self.logger.debug("SELECT_SOURCE command on %s: %s", self.display_name, source)
        sdk_mode = SOURCE_ID_TO_INPUT_MODE.get(source)
        if not sdk_mode:
            self.logger.warning("Unknown source '%s' for %s", source, self.display_name)
            return
        try:
            await self.device.async_set_play_mode(sdk_mode)
        except (WiimDeviceException, WiimRequestException) as err:
            self._handle_command_error("select_source", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        self.logger.debug("VOLUME_SET command on %s with level %s", self.display_name, volume_level)
        try:
            await self.device.async_set_volume(volume_level)
        except (WiimDeviceException, WiimRequestException) as err:
            self._handle_command_error("volume_set", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        self.logger.debug("VOLUME_MUTE command on %s with muted %s", self.display_name, muted)
        try:
            await self.device.async_set_mute(muted)
        except (WiimDeviceException, WiimRequestException) as err:
            self._handle_command_error("volume_mute", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def play(self) -> None:
        """Play command."""
        self.logger.debug("PLAY command on %s", self.display_name)
        try:
            await self.device.async_play()
        except (WiimDeviceException, WiimRequestException) as err:
            self._handle_command_error("play", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def stop(self) -> None:
        """Stop command."""
        self.logger.debug("STOP command on %s", self.display_name)
        try:
            await self.device.async_stop()
        except (WiimDeviceException, WiimRequestException) as err:
            self._handle_command_error("stop", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def pause(self) -> None:
        """Pause command."""
        self.logger.debug("PAUSE command on %s", self.display_name)
        try:
            await self.device.async_pause()
            await self.device.sync_device_duration_and_position()
        except (WiimDeviceException, WiimRequestException) as err:
            self._handle_command_error("pause", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        self.logger.debug("PLAY_MEDIA command on %s with uri %s", self.display_name, media.uri)
        didl_string = create_didl_metadata_str(media)
        self.current_uri = media.uri
        try:
            await self.device.async_play(uri=media.uri, metadata=didl_string)
        except (WiimDeviceException, WiimRequestException) as err:
            self._handle_command_error("play_media", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        self.device.general_event_callback = None
        self.device.av_transport_event_callback = None
        self.device.rendering_control_event_callback = None
        self.device.play_queue_event_callback = None
        self.logger.debug("Player %s unloaded, callbacks cleared", self.name)

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        self.logger.debug("SET_MEMBERS command on %s", self.display_name)
        try:
            if player_ids_to_add:
                for player_id in player_ids_to_add:
                    await self._wiim_controller.async_join_group(self.player_id, player_id)

            if player_ids_to_remove:
                for player_id in player_ids_to_remove:
                    await self._wiim_controller.async_ungroup_device(player_id)
        except (WiimDeviceException, WiimRequestException) as err:
            self._handle_command_error("set_members", err)

    def _update_ma_state_from_sdk_cache(self) -> None:
        """Update MA state from SDK's cache/HTTP poll attributes.

        This is the main method for updating this entity's MA attributes.
        Crucially, it also handles propagating metadata to followers if this is a leader.
        """
        logger = self.logger
        logger.debug("Device %s: Updating MA state from SDK cache/HTTP poll", self._attr_name)

        self._attr_available = self.device.available

        if self.device.name != self._attr_name:
            self._attr_name = self.device.name

        if not self._attr_available:
            # If device is unavailable, clear media-related attributes
            self._attr_current_media = None
            self._attr_active_source = None
            self.update_state()
            return

        # Update common attributes first
        self._attr_volume_level = self.device.volume if self.device.volume is not None else None
        self._attr_volume_muted = self.device.is_muted

        # Group role and state
        snapshot = self._wiim_controller.get_group_snapshot(self.device.udn)

        if snapshot.role == WiimGroupRole.FOLLOWER:
            self._attr_group_members.clear()
            try:
                leader_device = self._wiim_controller.get_device(snapshot.leader_udn)
                if leader_device.playing_status is not None:
                    self._attr_playback_state = SDK_TO_MA_STATE.get(
                        leader_device.playing_status, PlaybackState.IDLE
                    )
            except ValueError:
                self.logger.debug(
                    "Leader %s not found for follower %s",
                    snapshot.leader_udn,
                    self._attr_name,
                )
                self._attr_playback_state = PlaybackState.IDLE
        else:
            if self.device.playing_status is not None:
                self._attr_playback_state = SDK_TO_MA_STATE.get(
                    self.device.playing_status, PlaybackState.IDLE
                )

            group_members = self._wiim_controller.get_group_members(self.player_id)
            self._attr_group_members = [m.udn for m in group_members if m.udn != self.player_id]

            # Active source detection
            if self.current_uri and self.current_uri == self.device.current_track_uri:
                self._attr_active_source = self.player_id
            elif self.device.current_track_uri == "wiimu_airplay":
                self._attr_active_source = SOURCE_AIRPLAY
            elif self.device.current_track_uri and self.device.current_track_uri.startswith(
                "spotify:"
            ):
                self._attr_active_source = SOURCE_SPOTIFY
            elif self.device.play_mode is not None:
                for mode_name, ps in INPUT_MODE_SOURCES.items():
                    if mode_name == self.device.play_mode:
                        self._attr_active_source = ps.id
                        break
                else:
                    self._attr_active_source = SOURCE_UNKNOWN
            else:
                self._attr_active_source = SOURCE_UNKNOWN

            # Set current media for external sources
            if self._attr_active_source != self.player_id and (media := self.device.current_media):
                self.set_current_media(
                    uri=media.uri or "",
                    title=media.title,
                    artist=media.artist,
                    album=media.album,
                    image_url=media.image_url,
                    source_id=self._attr_active_source,
                    duration=media.duration,
                )

            self._attr_elapsed_time = self.device.current_position
            self._attr_elapsed_time_last_updated = time.time()

        self.update_state()
