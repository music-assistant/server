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

from music_assistant.constants import create_sample_rates_config_entry
from music_assistant.helpers.upnp import create_didl_metadata
from music_assistant.models.player import Player, PlayerMedia

from .constants import (
    INPUT_MODE_SOURCES,
    PASSIVE_SOURCES,
    PLAYER_ID_PREFIX,
    SOURCE_AIRPLAY,
    SOURCE_ID_TO_INPUT_MODE,
    SOURCE_SPOTIFY,
    SOURCE_UNKNOWN,
)

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpService, UpnpStateVariable
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType

    from .provider import WiimProvider

SDK_TO_MA_STATE: dict[PlayingStatus, PlaybackState] = {
    PlayingStatus.PLAYING: PlaybackState.PLAYING,
    PlayingStatus.PAUSED: PlaybackState.PAUSED,
    PlayingStatus.STOPPED: PlaybackState.IDLE,
    PlayingStatus.LOADING: PlaybackState.IDLE,
}


class WiimPlayer(Player):
    """Wiim Player in Music Assistant."""

    def __init__(
        self,
        provider: WiimProvider,
        player_id: str,
        device: WiimDevice,
        mac_address: str | None = None,
    ) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)

        self._attr_name = device.name
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.ENQUEUE,
            PlayerFeature.SEEK,
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
        if mac_address:
            self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac_address)

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
        self.logger.debug(
            "AVTransport event on %s: TransportState=%s, URI=%s, Duration=%s, Position=%s",
            self._attr_name,
            event_data.get("TransportState"),
            event_data.get("CurrentTrackURI"),
            event_data.get("CurrentTrackDuration"),
            event_data.get("RelativeTimePosition"),
        )
        if transport_state := event_data.get("TransportState"):
            try:
                sdk_status = PlayingStatus(transport_state)
            except ValueError:
                self.logger.warning("Unknown TransportState: %s", transport_state)
            else:
                if sdk_status == PlayingStatus.STOPPED:
                    self._attr_elapsed_time = None
                    self._attr_elapsed_time_last_updated = None

        # Sync position on every AVTransport event — not just state transitions.
        # Gapless track changes don't change TransportState but do update
        # the URI, duration, and position.
        asyncio.create_task(self._sync_position())
        self._update_ma_state_from_sdk_cache()

    async def _sync_position(self) -> None:
        """Fetch fresh position from the device and update state."""
        try:
            await self.device.sync_device_duration_and_position()
        except (WiimDeviceException, WiimRequestException) as err:
            self.logger.debug("Failed to sync position for %s: %s", self._attr_name, err)
            return
        device_pos = self.device.current_position
        self.logger.debug(
            "_sync_position on %s: device_pos=%s, duration=%s, uri=%s",
            self._attr_name,
            device_pos,
            self.device.current_track_duration,
            self.device.current_track_uri,
        )
        if device_pos is not None:
            self._attr_elapsed_time = device_pos
            self._attr_elapsed_time_last_updated = time.time()
        self._update_ma_state_from_sdk_cache()

    def _handle_sdk_rendering_control_event(
        self, service: UpnpService, state_variables: list[UpnpStateVariable[typing.Any]]
    ) -> None:
        """Handle RenderingControl events from the SDK."""
        self._update_ma_state_from_sdk_cache()
        # Check if this event contains a Slave element (group membership change)
        for sv in state_variables:
            if sv.name == "LastChange" and sv.value and "Slave" in str(sv.value):
                self.mass.create_task(self._refresh_multiroom())
                break

    async def _refresh_multiroom(self) -> None:
        """Refresh multiroom status from devices, then update all WiiM players."""
        try:
            await self._wiim_controller.async_update_all_multiroom_status()
        except (WiimDeviceException, WiimRequestException) as err:
            self.logger.debug("Failed to refresh multiroom status: %s", err)
        for player in self.provider.players:
            if isinstance(player, WiimPlayer):
                player._update_ma_state_from_sdk_cache()

    def _handle_sdk_play_queue_event(
        self, service: UpnpService, state_variables: list[UpnpStateVariable[typing.Any]]
    ) -> None:
        """Handle PlayQueue events from the SDK."""
        self._update_ma_state_from_sdk_cache()

    def _mark_unavailable(
        self, action: str, err: WiimDeviceException | WiimRequestException
    ) -> None:
        """Handle a command error by marking the device unavailable."""
        self.logger.warning("Command '%s' failed on %s: %s", action, self._attr_name, err)
        self._attr_available = False
        self.update_state()

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return player-specific config entries."""
        return [
            create_sample_rates_config_entry(
                max_sample_rate=192000,
                safe_max_sample_rate=192000,
                max_bit_depth=24,
                safe_max_bit_depth=24,
            ),
        ]

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing of the next queue item on the player."""
        stream_url = await self.mass.streams.resolve_stream_url(self.player_id, media)
        didl_metadata = create_didl_metadata(media, url=stream_url)
        try:
            await self.device._invoke_upnp_action(
                "AVTransport",
                "SetNextAVTransportURI",
                NextURI=stream_url,
                NextURIMetaData=didl_metadata,
            )
        except (WiimDeviceException, WiimRequestException) as err:
            self.logger.warning("Enqueue failed on %s: %s", self._attr_name, err)

    async def select_source(self, source: str) -> None:
        """Handle SELECT SOURCE command on the player.

        :param source: The source(id) to select, as defined in the source_list.
        """
        sdk_mode = SOURCE_ID_TO_INPUT_MODE.get(source)
        if not sdk_mode:
            self.logger.warning("Unknown source '%s' for %s", source, self.display_name)
            return
        try:
            await self.device.async_set_play_mode(sdk_mode)
        except (WiimDeviceException, WiimRequestException) as err:
            self._mark_unavailable("select_source", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        try:
            await self.device.async_set_volume(volume_level)
        except (WiimDeviceException, WiimRequestException) as err:
            self._mark_unavailable("volume_set", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        try:
            await self.device.async_set_mute(muted)
        except (WiimDeviceException, WiimRequestException) as err:
            self._mark_unavailable("volume_mute", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def play(self) -> None:
        """Play command."""
        try:
            await self.device.async_play()
        except (WiimDeviceException, WiimRequestException) as err:
            self._mark_unavailable("play", err)
            return
        await self._sync_position()

    async def stop(self) -> None:
        """Stop command."""
        self._attr_active_source = None
        self._attr_current_media = None
        try:
            await self.device.async_stop()
        except (WiimDeviceException, WiimRequestException) as err:
            self._mark_unavailable("stop", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def pause(self) -> None:
        """Pause command."""
        try:
            await self.device.async_pause()
        except (WiimDeviceException, WiimRequestException) as err:
            self._mark_unavailable("pause", err)
            return
        await self._sync_position()

    async def seek(self, position: int) -> None:
        """Seek to position in seconds."""
        try:
            await self.device.async_seek(position)
        except (WiimDeviceException, WiimRequestException) as err:
            self._mark_unavailable("seek", err)

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        stream_url = await self.mass.streams.resolve_stream_url(self.player_id, media)
        didl_metadata = create_didl_metadata(media, url=stream_url)
        self._attr_active_source = self.player_id
        self.set_current_media(
            uri=stream_url,
            title=media.title,
            artist=media.artist,
            album=media.album,
            image_url=media.image_url,
            duration=media.duration,
            source_id=media.source_id,
            queue_item_id=media.queue_item_id,
            clear_all=True,
        )
        try:
            await self.device.async_play(uri=stream_url, metadata=didl_metadata)
        except (WiimDeviceException, WiimRequestException) as err:
            self._mark_unavailable("play_media", err)
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
        try:
            if player_ids_to_add:
                for member_id in player_ids_to_add:
                    if member := self.mass.players.get_player(member_id):
                        await self._wiim_controller.async_join_group(
                            self.device.udn, member.device.udn
                        )

            if player_ids_to_remove:
                for member_id in player_ids_to_remove:
                    if member := self.mass.players.get_player(member_id):
                        await self._wiim_controller.async_ungroup_device(member.device.udn)
        except (WiimDeviceException, WiimRequestException) as err:
            self._mark_unavailable("set_members", err)

    def _update_ma_state_from_sdk_cache(self) -> None:
        """Update MA state from SDK's cache/HTTP poll attributes."""
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
                new_state = SDK_TO_MA_STATE.get(self.device.playing_status, PlaybackState.IDLE)
                if (
                    new_state == PlaybackState.PLAYING
                    and self._attr_playback_state != PlaybackState.PLAYING
                ):
                    # Ensure elapsed_time is not None so the frontend can
                    # extrapolate using elapsed_time + (now - last_updated).
                    if self._attr_elapsed_time is None:
                        self._attr_elapsed_time = 0
                    self._attr_elapsed_time_last_updated = time.time()
                self._attr_playback_state = new_state

            group_members = self._wiim_controller.get_group_members(self.device.udn)
            self._attr_group_members = [
                f"{PLAYER_ID_PREFIX}{m.udn}" for m in group_members if m.udn != self.device.udn
            ]

            # Active source detection: use the SDK's play_mode (set from both UPnP
            # PlaybackStorageMedium events and HTTP polling) as the primary signal.
            # Within Network mode, refine using the track URI to distinguish
            # AirPlay / Spotify Connect from MA's own streams.
            play_mode = self.device.play_mode
            if play_mode and play_mode != "Network" and play_mode in INPUT_MODE_SOURCES:
                self._attr_active_source = INPUT_MODE_SOURCES[play_mode].id
            elif play_mode == "Network":
                device_uri = self.device.current_track_uri
                if device_uri == "wiimu_airplay":
                    self._attr_active_source = SOURCE_AIRPLAY
                elif device_uri and device_uri.startswith("spotify:"):
                    self._attr_active_source = SOURCE_SPOTIFY

            # Update current media from device state.
            # For MA-sourced playback, keep the URI in sync so the queue controller
            # can parse the queue_item_id from the stream URL on gapless transitions.
            # For external sources, set full metadata from the device.
            if self._attr_active_source == self.player_id:
                device_uri = self.device.current_track_uri
                prev_uri = self._attr_current_media.uri if self._attr_current_media else None
                if device_uri and prev_uri != device_uri:
                    self.logger.debug(
                        "URI changed on %s: %s -> %s",
                        self._attr_name,
                        prev_uri,
                        device_uri,
                    )
                    self.set_current_media(uri=device_uri)
            elif media := self.device.current_media:
                self.set_current_media(
                    uri=media.uri or "",
                    title=media.title,
                    artist=media.artist,
                    album=media.album,
                    image_url=media.image_url,
                    source_id=self._attr_active_source,
                    duration=media.duration,
                )
        self.update_state()
