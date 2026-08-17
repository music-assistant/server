"""Wiim Player implementation."""

from __future__ import annotations

import time
import typing
from typing import TYPE_CHECKING

from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerCommandFailed, UnsupportedFeaturedException
from music_assistant_models.player import DeviceInfo
from wiim import PlayingStatus, WiimDevice
from wiim.exceptions import WiimException
from wiim.models import WiimGroupRole

from music_assistant.constants import (
    EXTERNAL_PAUSE_IDLE_TIMEOUT,
    create_sample_rates_config_entry,
)
from music_assistant.helpers.upnp import create_didl_metadata
from music_assistant.models.player import Player, PlayerMedia

from .constants import (
    BACKEND_OFFICIAL,
    INPUT_MODE_SOURCES,
    PASSIVE_SOURCES,
    PLAYER_ID_PREFIX,
    SOURCE_AIRPLAY,
    SOURCE_ID_TO_INPUT_MODE,
    SOURCE_NETWORK,
    SOURCE_SPOTIFY,
    SOURCE_UNKNOWN,
)
from .helpers import is_in_mixed_group

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpService, UpnpStateVariable
    from music_assistant_models.config_entries import ConfigEntry

    from .provider import WiimProvider

SDK_TO_MA_STATE: dict[PlayingStatus, PlaybackState] = {
    PlayingStatus.PLAYING: PlaybackState.PLAYING,
    PlayingStatus.PAUSED: PlaybackState.PAUSED,
    PlayingStatus.STOPPED: PlaybackState.IDLE,
    PlayingStatus.LOADING: PlaybackState.PLAYING,
}


class WiimPlayer(Player):
    """Wiim Player in Music Assistant."""

    linkplay_backend = BACKEND_OFFICIAL

    # the device reports a source the app released as plain 'paused', indistinguishable
    # from a real pause, so only the grace period can tell us the session is over.
    _attr_external_pause_idle_timeout = EXTERNAL_PAUSE_IDLE_TIMEOUT

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
            PlayerFeature.GAPLESS_PLAYBACK,
            PlayerFeature.SEEK,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PAUSE,
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.SELECT_SOURCE,
        }
        self.device = device
        self._wiim_controller = provider.wiim_controller

        self._attr_device_info = DeviceInfo(
            model=device.model_name,
            manufacturer=device.manufacturer or "WiiM",
            software_version=device.firmware_version,
        )
        self._attr_device_info.add_identifier(IdentifierType.UUID, device.udn.removeprefix("uuid:"))
        if device.ip_address:
            self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, device.ip_address)
        if mac_address:
            self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac_address)

        device.general_event_callback = self._handle_sdk_general_device_update
        device.rendering_control_event_callback = self._handle_sdk_rendering_control_event
        device.av_transport_event_callback = self._handle_sdk_av_transport_event
        device.play_queue_event_callback = self._handle_sdk_play_queue_event

        self._last_logged_sdk_uri: str | None = None
        self._last_logged_sdk_status: PlayingStatus | None = None
        self._ma_stream_uri: str | None = None

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return supported features; grouping is withdrawn in a read-only mixed group."""
        if self._in_mixed_group:
            return self._attr_supported_features - {PlayerFeature.SET_MEMBERS}
        return self._attr_supported_features

    @property
    def grouping_locked(self) -> bool:
        """Suppress grouping while in a read-only externally-created mixed group."""
        return self._in_mixed_group

    @property
    def can_group_with(self) -> set[str]:
        """Return the ids of the other official WiiM players this player can group with."""
        if self._in_mixed_group:
            return set()
        return {
            player.player_id
            for player in self.provider.players
            if isinstance(player, WiimPlayer)
            and player.available
            and player.player_id != self.player_id
            and not player._in_mixed_group
        }

    def is_native_group_compatible(self, other: Player) -> bool:
        """Only other official WiiM players group natively (never a generic LinkPlay shell)."""
        return other.player_id in self.can_group_with

    # --- Lifecycle ---

    async def setup(self) -> None:
        """Handle logic when the player is set up in the Player controller."""
        for mode_name in self.device.supported_input_modes:
            if mode_name in INPUT_MODE_SOURCES and mode_name != SOURCE_NETWORK:
                self._attr_source_list.append(INPUT_MODE_SOURCES[mode_name])
        self._attr_source_list.append(PASSIVE_SOURCES[SOURCE_AIRPLAY])
        self._attr_source_list.append(PASSIVE_SOURCES[SOURCE_SPOTIFY])
        self._attr_source_list.append(PASSIVE_SOURCES[SOURCE_UNKNOWN])
        self._attr_needs_poll = True
        self._attr_poll_interval = 5

    async def poll(self) -> None:
        """Poll player for transport state and position updates."""
        await self._sync_position()
        await self._refresh_device_status()
        self._attr_poll_interval = 5 if self._attr_playback_state == PlaybackState.PLAYING else 30

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        self.device.general_event_callback = None
        self.device.av_transport_event_callback = None
        self.device.rendering_control_event_callback = None
        self.device.play_queue_event_callback = None
        try:
            await self._wiim_controller.remove_device(self.device.udn)
            await self.device.disconnect()
        except Exception:
            self.logger.exception("Error tearing down WiiM device %s", self.name)
        self.logger.debug("Player %s unloaded, SDK resources released", self.name)

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return player-specific config entries."""
        return [
            create_sample_rates_config_entry(
                max_sample_rate=192000,
                safe_max_sample_rate=192000,
                max_bit_depth=24,
                safe_max_bit_depth=24,
            ),
        ]

    # --- Player commands ---

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        stream_url = await self.mass.streams.resolve_stream_url(self.player_id, media)
        didl_metadata = create_didl_metadata(media, url=stream_url)
        self.set_current_media(
            uri=stream_url,
            title=media.title,
            artist=media.artist,
            album=media.album,
            image_url=media.image_url,
            duration=media.duration,
            source_id=media.source_id,
            clear_all=True,
        )
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self._ma_stream_uri = stream_url
        try:
            await self.device.async_play(uri=stream_url, metadata=didl_metadata)
        except WiimException as err:
            # The device never took our stream, so the guard must not outlive the attempt.
            self._ma_stream_uri = None
            self._handle_command_error("play_media", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing of the next queue item on the player."""
        stream_url = await self.mass.streams.resolve_stream_url(self.player_id, media)
        didl_metadata = create_didl_metadata(media, url=stream_url)
        self.logger.debug(
            "enqueue_next_media on %s: queue_item_id=%s, uri=%s",
            self._attr_name,
            media.queue_item_id,
            stream_url,
        )
        try:
            await self.device._invoke_upnp_action(
                "AVTransport",
                "SetNextAVTransportURI",
                NextURI=stream_url,
                NextURIMetaData=didl_metadata,
            )
        except WiimException as err:
            self.logger.warning("Enqueue failed on %s: %s", self._attr_name, err)

    async def play(self) -> None:
        """Play command."""
        try:
            await self.device.async_play()
        except WiimException as err:
            self._handle_command_error("play", err)
            return
        await self._sync_position()

    async def pause(self) -> None:
        """Pause command."""
        try:
            await self.device.async_pause()
        except WiimException as err:
            self._handle_command_error("pause", err)
            return
        await self._sync_position()

    async def stop(self) -> None:
        """Stop command."""
        self._attr_active_source = None
        self._attr_current_media = None
        self._ma_stream_uri = None
        try:
            await self.device.async_stop()
        except WiimException as err:
            self._handle_command_error("stop", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def seek(self, position: int) -> None:
        """Seek to position in seconds."""
        try:
            await self.device.async_seek(position)
        except WiimException as err:
            self._handle_command_error("seek", err)

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        try:
            await self.device.async_set_volume(volume_level)
        except WiimException as err:
            self._handle_command_error("volume_set", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        try:
            await self.device.async_set_mute(muted)
        except WiimException as err:
            self._handle_command_error("volume_mute", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def select_source(self, source: str) -> None:
        """
        Handle SELECT SOURCE command on the player.

        :param source: The source(id) to select, as defined in the source_list.
        """
        sdk_mode = SOURCE_ID_TO_INPUT_MODE.get(source)
        if not sdk_mode:
            self.logger.warning("Unknown source '%s' for %s", source, self.display_name)
            return
        try:
            await self.device.async_set_play_mode(sdk_mode)
        except WiimException as err:
            self._handle_command_error("select_source", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        if self._in_mixed_group:
            raise UnsupportedFeaturedException(
                f"{self._attr_name} is in an externally-created mixed group and cannot be regrouped"
            )
        add_ids = player_ids_to_add or []
        remove_ids = player_ids_to_remove or []
        if len(set(add_ids)) != len(add_ids) or len(set(remove_ids)) != len(remove_ids):
            raise PlayerCommandFailed(f"Duplicate member id in grouping request on {self.name}")
        if conflicting := set(add_ids) & set(remove_ids):
            raise PlayerCommandFailed(
                f"Cannot add and remove the same member on {self.name}: {conflicting}"
            )
        # Resolve and validate the whole batch before any hardware call, so a stale, moved,
        # mixed or cross-backend target can never leave the group partially mutated.
        to_add = [self._require_wiim_member(mid) for mid in add_ids]
        to_remove = [self._require_wiim_member(mid) for mid in remove_ids]
        if to_remove:
            # Only ungroup members this leader still owns per the freshest SDK snapshot: a
            # target that has since moved to another (possibly read-only mixed) group must
            # not be torn out of it.
            current_member_udns = set(
                self._wiim_controller.get_group_snapshot(self.device.udn).member_udns
            )
            for member in to_remove:
                if member.device.udn not in current_member_udns:
                    raise PlayerCommandFailed(
                        f"{member.player_id} is no longer a member of {self.name}"
                    )
        queue = self.mass.player_queues.get(self.player_id)
        pq_data = self.mass.player_queues.queue_data_or_none(self.player_id) if queue else None
        entry_sdk_uri = self.device.current_media.uri if self.device.current_media else None
        self.logger.debug(
            "set_members entry on %s: add=%s, remove=%s | "
            "queue.current_item_id=%s, queue.next_item_id=%s, "
            "queue.next_item_id_enqueued=%s | "
            "sdk.current_uri=%s, sdk.playing_status=%s",
            self._attr_name,
            player_ids_to_add,
            player_ids_to_remove,
            queue.current_item.queue_item_id if queue and queue.current_item else None,
            queue.next_item.queue_item_id if queue and queue.next_item else None,
            pq_data.next_item_id_enqueued if pq_data else None,
            entry_sdk_uri,
            self.device.playing_status,
        )
        try:
            if to_add:
                await self._wiim_controller.async_join_group(
                    self.device.udn, [member.device.udn for member in to_add]
                )
            for member in to_remove:
                await self._wiim_controller.async_ungroup_device(member.device.udn)
        except WiimException as err:
            # Log and refresh state as for any command, but surface grouping failures to the
            # caller: a swallowed error would report a join/leave as succeeded.
            self._handle_command_error("set_members", err)
            raise PlayerCommandFailed(f"set_members failed on {self.name}: {err}") from err
        finally:
            exit_sdk_uri = self.device.current_media.uri if self.device.current_media else None
            self.logger.debug(
                "set_members exit on %s: sdk.current_uri=%s, sdk.playing_status=%s, "
                "queue.current_item_id=%s",
                self._attr_name,
                exit_sdk_uri,
                self.device.playing_status,
                queue.current_item.queue_item_id if queue and queue.current_item else None,
            )

    # --- SDK event handlers ---

    def _handle_sdk_general_device_update(self, device: WiimDevice) -> None:
        """Handle general updates from the SDK (availability changes)."""
        if not device.available:
            self.logger.debug("Device %s became unavailable", self._attr_name)
            self._update_ma_state_from_sdk_cache()
            return
        if device.supports_http_api:
            self.logger.debug("Device %s available, ensuring subscriptions", self._attr_name)
            self.mass.create_task(self._ensure_subscriptions_and_update())
        else:
            self._update_ma_state_from_sdk_cache()

    def _handle_sdk_av_transport_event(
        self, service: UpnpService, state_variables: list[UpnpStateVariable[typing.Any]]
    ) -> None:
        """Handle AVTransport events from the SDK."""
        event_data = self.device.event_data
        if transport_state := event_data.get("TransportState"):
            try:
                sdk_status = PlayingStatus(transport_state)
            except ValueError:
                pass
            else:
                if sdk_status in (
                    PlayingStatus.PLAYING,
                    PlayingStatus.PAUSED,
                    PlayingStatus.LOADING,
                ):
                    self.mass.create_task(self._sync_position())
        self._update_ma_state_from_sdk_cache()

    def _handle_sdk_rendering_control_event(
        self, service: UpnpService, state_variables: list[UpnpStateVariable[typing.Any]]
    ) -> None:
        """Handle RenderingControl events from the SDK."""
        self._update_ma_state_from_sdk_cache()
        for sv in state_variables:
            if sv.name == "LastChange" and sv.value and "Slave" in str(sv.value):
                self.mass.create_task(self._refresh_multiroom())
                break

    def _handle_sdk_play_queue_event(
        self, service: UpnpService, state_variables: list[UpnpStateVariable[typing.Any]]
    ) -> None:
        """Handle PlayQueue events from the SDK."""
        self._update_ma_state_from_sdk_cache()

    # --- Private helpers ---

    def _update_ma_state_from_sdk_cache(self) -> None:
        """Update MA state from SDK's cache/HTTP poll attributes."""
        self._attr_available = self.device.available
        if self.device.name != self._attr_name:
            self._attr_name = self.device.name

        if not self._attr_available:
            self._attr_current_media = None
            self._attr_active_source = None
            self.update_state()
            return

        self._attr_volume_level = self.device.volume
        self._attr_volume_muted = self.device.is_muted

        # Followers have their state derived by MA from the leader.
        snapshot = self._wiim_controller.get_group_snapshot(self.device.udn)
        if snapshot.role == WiimGroupRole.FOLLOWER:
            self.update_state()
            return

        media = self.device.current_media
        device_uri = media.uri if media and media.uri else ""
        play_mode = self.device.play_mode

        # Playback state
        if (new_state := self._resolve_playback_state(device_uri, play_mode)) is not None:
            self._attr_playback_state = new_state

        # Group members
        self._update_group_state(snapshot.member_udns)

        if play_mode and play_mode != SOURCE_NETWORK and play_mode in INPUT_MODE_SOURCES:
            self._attr_active_source = INPUT_MODE_SOURCES[play_mode].id
        elif play_mode == SOURCE_NETWORK:
            ma_queue = self.mass.player_queues.get(self.player_id)
            assert ma_queue is not None
            if device_uri == "wiimu_airplay":
                self._attr_active_source = SOURCE_AIRPLAY
            elif device_uri.startswith("spotify:"):
                self._attr_active_source = SOURCE_SPOTIFY
            elif ma_queue.current_item:
                self._attr_active_source = self.player_id
            else:
                self._attr_active_source = SOURCE_UNKNOWN
        else:
            self._attr_active_source = None

        source_display_name: str | None = None
        if play_mode and play_mode in INPUT_MODE_SOURCES:
            source_display_name = INPUT_MODE_SOURCES[play_mode].name
        elif self._attr_active_source in PASSIVE_SOURCES:
            source_display_name = PASSIVE_SOURCES[self._attr_active_source].name

        is_ma_source = self._attr_active_source == self.player_id
        sdk_has_metadata = bool(media and (media.title or media.artist or media.album))

        if self._attr_active_source is None:
            self._attr_current_media = None
        elif is_ma_source and not sdk_has_metadata:
            # Keep the metadata play_media set until the SDK catches up.
            pass
        else:
            self._attr_current_media = PlayerMedia(
                uri=(media.uri if media else None) or "",
                title=(media.title if media else None) or source_display_name,
                artist=media.artist if media else None,
                album=media.album if media else None,
                image_url=media.image_url if media else None,
                duration=media.duration if media else None,
                source_id=self._attr_active_source,
            )

        self._log_sdk_state_change()
        self.update_state()

    def _resolve_playback_state(
        self, device_uri: str, play_mode: str | None
    ) -> PlaybackState | None:
        """
        Map the SDK playing status to a MA playback state.

        Returns ``None`` when the current state should be kept: either no status
        is known yet, or the report is implausible and should not propagate.

        :param device_uri: The media URI currently loaded on the device ("" if none).
        :param play_mode: The device's current play mode (input source).
        """
        # widened annotation: the SDK types playing_status as non-optional, but
        # we stay tolerant of a None from mocks/edge paths (as the code always has)
        sdk_status: PlayingStatus | None = self.device.playing_status
        if sdk_status is None:
            return None
        new_state = SDK_TO_MA_STATE.get(sdk_status, PlaybackState.IDLE)
        # The device acks transport commands with a transient (false) PLAYING
        # report while no media is loaded yet (observed during group session
        # setup). Nothing can actually play in network mode without a URI, so
        # keep the previous state instead of propagating the false start and
        # the PLAYING->IDLE->PLAYING flicker it causes downstream. External
        # inputs (line-in, Bluetooth, ...) legitimately play without a URI and
        # are not affected.
        if new_state == PlaybackState.PLAYING and play_mode == SOURCE_NETWORK and not device_uri:
            return None
        return new_state

    def _log_sdk_state_change(self) -> None:
        """Log a debug line whenever the SDK-reported URI or playing_status changes."""
        new_sdk_uri = self.device.current_media.uri if self.device.current_media else None
        new_sdk_status = self.device.playing_status
        if (
            new_sdk_uri == self._last_logged_sdk_uri
            and new_sdk_status == self._last_logged_sdk_status
        ):
            return
        ma_queue = self.mass.player_queues.get(self.player_id)
        self.logger.debug(
            "WiiM state changed on %s: uri=%r->%r, status=%s->%s | queue.current_item_id=%s",
            self._attr_name,
            self._last_logged_sdk_uri,
            new_sdk_uri,
            self._last_logged_sdk_status,
            new_sdk_status,
            ma_queue.current_item.queue_item_id if ma_queue and ma_queue.current_item else None,
        )
        self._last_logged_sdk_uri = new_sdk_uri
        self._last_logged_sdk_status = new_sdk_status

    async def _ensure_subscriptions_and_update(self) -> None:
        """Re-subscribe to UPnP events and update state."""
        try:
            await self.device.ensure_subscriptions()
        except WiimException as err:
            self.logger.warning("Failed to re-subscribe for %s: %s", self._attr_name, err)
        self._update_ma_state_from_sdk_cache()

    async def _refresh_device_status(self) -> None:
        """Fetch fresh transport state, play mode and volume from the device."""
        if not self.device.available:
            # the SDK owns availability detection and recovery; querying a device it
            # already gave up on only holds up the poll of every other player.
            return
        try:
            await self.device.async_update_http_status()
        except WiimException as err:
            self.logger.debug("Failed to refresh status for %s: %s", self._attr_name, err)
            return
        self._update_ma_state_from_sdk_cache()

    async def _sync_position(self) -> None:
        """Fetch fresh position from the device and update state."""
        try:
            await self.device.sync_device_duration_and_position()
        except WiimException as err:
            self.logger.debug("Failed to sync position for %s: %s", self._attr_name, err)
            return
        media = self.device.current_media
        device_uri = media.uri if media else None
        if device_uri and device_uri == self._ma_stream_uri:
            self._ma_stream_uri = None
        # The device reports its previous content's position until it loads our stream URI.
        if self._ma_stream_uri is None and media is not None and media.position is not None:
            self._attr_elapsed_time = media.position
            self._attr_elapsed_time_last_updated = time.time()
        self._update_ma_state_from_sdk_cache()

    async def _refresh_multiroom(self) -> None:
        """Refresh multiroom status from devices, then update all WiiM players."""
        try:
            await self._wiim_controller.async_update_all_multiroom_status()
        except WiimException as err:
            self.logger.debug("Failed to refresh multiroom status: %s", err)
        for player in self.provider.players:
            if isinstance(player, WiimPlayer):
                player._update_ma_state_from_sdk_cache()

    def _handle_command_error(self, action: str, err: WiimException) -> None:
        """Handle a command error by logging and refreshing state."""
        self.logger.warning("Command '%s' failed on %s: %s", action, self._attr_name, err)
        self._update_ma_state_from_sdk_cache()

    def _update_group_state(self, member_udns: tuple[str, ...]) -> None:
        """Map the SDK's group snapshot to the managed group members."""
        managed_member_ids = [
            f"{PLAYER_ID_PREFIX}{member_udn}"
            for member_udn in member_udns
            if member_udn != self.device.udn
            and self.mass.players.get_player(f"{PLAYER_ID_PREFIX}{member_udn}")
        ]
        self._attr_group_members = (
            [self.player_id, *managed_member_ids] if managed_member_ids else []
        )

    @property
    def _in_mixed_group(self) -> bool:
        """Whether this device is in an externally-created mixed (cross-backend) group."""
        return is_in_mixed_group(self)

    def _require_wiim_member(self, player_id: str) -> WiimPlayer:
        """
        Return a same-backend, groupable member for a grouping command.

        A request targeting this player itself, an unknown player, another backend
        (e.g. a generic LinkPlay shell) or a device locked in an externally-created
        mixed group is rejected with a typed error rather than applied.

        :param player_id: The id of the member to resolve and validate.
        """
        if player_id == self.player_id:
            raise UnsupportedFeaturedException(f"Cannot group {self.name} with itself")
        member = self.mass.players.get_player(player_id)
        if member is None:
            raise PlayerCommandFailed(
                f"{player_id} is not a known player for grouping on {self.name}"
            )
        if not isinstance(member, WiimPlayer):
            raise UnsupportedFeaturedException(
                f"Cannot group {player_id} with {self.name}: cross-backend grouping is unsupported"
            )
        if member._in_mixed_group:
            raise PlayerCommandFailed(
                f"{player_id} is in an externally-created mixed group and cannot be regrouped"
            )
        return member
