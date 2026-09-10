"""HEOS Player implementation."""

from __future__ import annotations

import asyncio
import logging
from copy import copy
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import PlayerCommandFailed, SetupFailedError
from music_assistant_models.player import DeviceInfo, PlayerSource
from pyheos import Heos, HeosError, const
from pyheos import PlayState as HeosPlayState

from music_assistant.constants import EXTERNAL_PAUSE_IDLE_TIMEOUT, VERBOSE_LOG_LEVEL
from music_assistant.models.player import Player, PlayerMedia
from music_assistant.providers.heos.helpers import media_uri_from_now_playing_media

from .constants import (
    CONF_PLAYBACK_TRANSITION_TIMEOUT,
    DEFAULT_PLAYBACK_TRANSITION_TIMEOUT,
    HEOS_MEDIA_TYPE_TO_MEDIA_TYPE,
    HEOS_PLAY_STATE_TO_PLAYBACK_STATE,
    NON_HIRES_HEOS_MODELS,
)

if TYPE_CHECKING:
    from pyheos import HeosPlayer as PyHeosPlayer

    from .provider import HeosPlayerProvider


PLAYER_FEATURES = {
    PlayerFeature.VOLUME_SET,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.PAUSE,
    PlayerFeature.NEXT_PREVIOUS,
    PlayerFeature.SELECT_SOURCE,
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.PLAY_MEDIA,
}


class HeosPlayer(Player):
    """HeosPlayer in Music Assistant."""

    # HEOS keeps a source it loaded itself reported as paused once the app walked away,
    # and pushes no event when that session goes stale.
    _attr_external_pause_idle_timeout = EXTERNAL_PAUSE_IDLE_TIMEOUT

    _heos: Heos
    _heos_queue: Heos
    _device: PyHeosPlayer

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player requires flow mode."""
        return True

    def __init__(self, provider: HeosPlayerProvider, device: PyHeosPlayer) -> None:
        """Initialize the Player."""
        super().__init__(provider, str(device.player_id))

        self._device: PyHeosPlayer = device
        self._ma_controls_playback = False
        self._ma_playback_starting = False
        self._ma_playback_transition_timer_id = f"heos_playback_transition_{self.player_id}"
        self._on_unload_callbacks.append(self._cancel_ma_playback_transition)
        self._queue_cleanup_lock = asyncio.Lock()
        self._queue_cleanup_pending = False

        if self._device.heos is None:
            raise SetupFailedError("HEOS device has no controller assigned")

        if provider._heos_queue is None:
            raise SetupFailedError("HEOS queue controller is not set up")

        # Keep internal reference so we don't need to check None on each call
        self._heos = self._device.heos
        self._heos_queue = provider._heos_queue

        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = PLAYER_FEATURES
        self._attr_can_group_with = {self.provider.instance_id}

    async def setup(self) -> None:
        """Set up the player."""
        self.set_device_info()
        self.set_dynamic_attributes(update_media=True)

        await self.mass.players.register_or_update(self)

        self._on_unload_callbacks.append(
            self._device.add_on_player_event(self._player_event_received)
        )

        await self.build_group_list()
        await self.build_source_list()

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return HEOS-specific player configuration entries."""
        return [
            ConfigEntry(
                key=CONF_PLAYBACK_TRANSITION_TIMEOUT,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_PLAYBACK_TRANSITION_TIMEOUT,
                range=(1, 30),
                required=True,
                advanced=True,
            )
        ]

    def set_device_info(self) -> None:
        """Set all device info attributes."""
        # Extract manufacturer and model from device model string, if available
        model_parts = self._device.model.split(maxsplit=1)
        manufacturer = model_parts[0] if len(model_parts) == 2 else "HEOS"
        model = model_parts[1] if len(model_parts) == 2 else self._device.model

        _device_info = DeviceInfo(
            model=model,
            software_version=self._device.version,
            manufacturer=manufacturer,
        )
        _device_info.ip_address = self._device.ip_address
        self._attr_device_info = _device_info
        self._attr_available = self._device.available
        self._attr_name = self._device.name

        # Gen 1 HEOS hardware is capped at 48kHz/16-bit; HS2 and newer models
        # are hi-res capable up to 192kHz/24-bit
        if model in NON_HIRES_HEOS_MODELS:
            self._attr_supported_sample_rates = [(44100, 16), (48000, 16)]
        else:
            self._attr_supported_sample_rates = [
                (sr, bd) for sr in (44100, 48000, 88200, 96000, 176400, 192000) for bd in (16, 24)
            ]

    async def build_group_list(self) -> None:
        """Build group list based on group info from controller."""
        # Group IDs are the player ID of the leader
        if self._device.group_id is not None and str(self._device.group_id) == self.player_id:
            group_info = await self._heos.get_group_info(self._device.group_id)
            self._attr_group_members = [
                str(group_info.lead_player_id),
                *(str(member) for member in group_info.member_player_ids),
            ]
        else:
            self._attr_group_members.clear()

        self.update_state()

    async def build_source_list(self) -> None:
        """Build source list based on music source list, combined with player specific inputs."""
        prov = cast("HeosPlayerProvider", self.provider)
        self._attr_source_list = prov.music_source_list[:]  # copy so we can modify

        for input_source in prov.input_source_list:
            # Only add input sources that belong to this player
            if str(input_source.source_id) != self.player_id or input_source.media_id is None:
                continue

            self._attr_source_list.append(
                PlayerSource(
                    id=input_source.media_id,
                    name=input_source.name,
                    can_play_pause=True,
                )
            )

        self.update_state()

    async def _player_event_received(self, event: str) -> None:
        """Handle player device events."""
        self.logger.log(
            (
                VERBOSE_LOG_LEVEL
                if event == const.EVENT_PLAYER_NOW_PLAYING_PROGRESS
                else logging.DEBUG
            ),
            "[%s] Event received: %s",
            self._device.name,
            event,
        )
        match event:
            case const.EVENT_PLAYER_STATE_CHANGED:
                self._update_player_state()
                self._update_player_current_media()
                self._schedule_queue_cleanup()

            case const.EVENT_PLAYER_NOW_PLAYING_CHANGED:
                self._update_player_current_media()
                self._update_player_playing_progress()
                self._schedule_queue_cleanup()

            case const.EVENT_PLAYER_QUEUE_CHANGED:
                self._schedule_queue_cleanup()

            case const.EVENT_PLAYER_NOW_PLAYING_PROGRESS:
                self._update_player_playing_progress()

            case const.EVENT_PLAYER_VOLUME_CHANGED:
                self._update_player_volume()

            case const.EVENT_PLAYER_PLAYBACK_ERROR:
                self.logger.error(
                    "[%s] Playback error: %s", self._device.name, self._device.playback_error
                )
                self._queue_cleanup_pending = False
                self.set_dynamic_attributes()

            case _:
                # Update everything on other events
                self.set_dynamic_attributes()

        self.update_state()

    def _update_player_volume(self) -> None:
        """Update volume properties."""
        self._attr_volume_level = self._device.volume
        self._attr_volume_muted = self._device.is_muted

    def _update_player_state(self) -> None:
        """Update playback state."""
        self._attr_playback_state = HEOS_PLAY_STATE_TO_PLAYBACK_STATE.get(
            self._device.state, PlaybackState.UNKNOWN
        )

    def _update_player_current_media(self) -> None:
        """Update current media properties."""
        now_playing = self._device.now_playing_media
        if self._device.state == HeosPlayState.STOP:
            self.logger.debug(
                "[%s] Ignoring now playing change while stopped: %s",
                self._device.name,
                now_playing,
            )
            return

        if self._ma_playback_starting:
            self.logger.debug(
                "[%s] Ignoring now playing change while MA playback starts: %s",
                self._device.name,
                now_playing,
            )
            return

        # Only update if we're not playing from our queue
        # HEOS does not make a distinction on source ID when playing from a DLNA server, USB stick,
        # generic URL (like MA), or other local source.
        # We can only know we're playing from MA if we started this session.
        # When MA controls playback it serves a generic URL stream whose metadata HEOS
        # cannot parse (it reports "Url Stream"). Ignore that unreliable now-playing even
        # when active_source is momentarily stale (e.g. the play_url race before play_media
        # sets it) so MA's own, correct current_media is preserved. See support #5614.
        if (now_playing.source_id != const.MUSIC_SOURCE_LOCAL_MUSIC) or (
            self._attr_active_source != self.player_id and not self._ma_controls_playback
        ):
            self._ma_controls_playback = False
            self._queue_cleanup_pending = False
            self.logger.debug(
                "[%s] Now playing changed externally: %s", self._device.name, now_playing
            )

            if now_playing.source_id == const.MUSIC_SOURCE_AUX_INPUT:
                self._attr_active_source = str(now_playing.media_id)
            else:
                self._attr_active_source = str(now_playing.source_id)

            # HEOS reports position and duration in milliseconds, PlayerMedia expects seconds
            self._attr_current_media = PlayerMedia(
                uri=now_playing.media_id or media_uri_from_now_playing_media(now_playing),
                media_type=HEOS_MEDIA_TYPE_TO_MEDIA_TYPE.get(
                    now_playing.type,
                    MediaType.UNKNOWN,
                ),
                title=now_playing.song,
                artist=now_playing.artist,
                album=now_playing.album,
                image_url=now_playing.image_url,
                duration=int(now_playing.duration / 1000) if now_playing.duration else None,
                source_id=str(now_playing.source_id),
                elapsed_time=(
                    int(now_playing.current_position / 1000)
                    if now_playing.current_position is not None
                    else None
                ),
                elapsed_time_last_updated=(
                    now_playing.current_position_updated.timestamp()
                    if now_playing.current_position_updated
                    else None
                ),
                # TODO: We can use custom_data to set the IDs
            )

    def _update_player_playing_progress(self) -> None:
        """Update current media progress properties."""
        now_playing = self._device.now_playing_media

        self._attr_elapsed_time = (
            now_playing.current_position / 1000
            if now_playing.current_position is not None
            else None
        )
        self._attr_elapsed_time_last_updated = (
            now_playing.current_position_updated.timestamp()
            if now_playing.current_position_updated
            else None
        )

    def set_dynamic_attributes(self, update_media: bool = False) -> None:
        """Update all player dynamic attributes."""
        self._update_player_volume()
        self._update_player_state()

        if update_media:
            self._update_player_current_media()

        self._update_player_playing_progress()

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self._device.set_volume(volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        if muted:
            await self._device.mute()
        else:
            await self._device.unmute()

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        await self._device.play()

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        await self._device.stop()

    async def pause(self) -> None:
        """Handle PAUSE command on the player."""
        await self._device.pause()

    async def next_track(self) -> None:
        """Handle NEXT_TRACK command on the player."""
        await self._device.play_next()

    async def previous_track(self) -> None:
        """Handle PREVIOUS_TRACK command on the player."""
        await self._device.play_previous()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA command on given player."""
        self.logger.debug(
            "[%s] Received PLAY_MEDIA command with media_type=%s uri=%s",
            self._device.name,
            media.media_type,
            media.uri,
        )

        url = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
        self._cancel_ma_playback_transition()
        self._ma_playback_starting = True
        self._ma_controls_playback = True
        try:
            await self._device.play_url(url)
        except HeosError as err:
            self._cancel_ma_playback_transition()
            self._ma_controls_playback = False
            self._queue_cleanup_pending = False
            raise PlayerCommandFailed("Failed to start playback.") from err

        self._attr_current_media = media
        self._attr_active_source = self.player_id
        self._queue_cleanup_pending = True

        self.mass.call_later(
            self.get_config_value(
                CONF_PLAYBACK_TRANSITION_TIMEOUT,
                DEFAULT_PLAYBACK_TRANSITION_TIMEOUT,
                return_type=int,
            ),
            self._finish_ma_playback_transition,
            task_id=self._ma_playback_transition_timer_id,
        )

        self.update_state()

    def _schedule_queue_cleanup(self) -> None:
        """Debounce queue cleanup so rapid queue changes only trigger one follow-up."""
        if (
            not self._ma_controls_playback
            or not self._queue_cleanup_pending
            or self._attr_playback_state != PlaybackState.PLAYING
        ):
            return

        self.mass.call_later(
            1,
            self._start_queue_cleanup_task,
            task_id=f"heos_queue_cleanup_timer_{self.player_id}",
        )

    def _start_queue_cleanup_task(self) -> None:
        """Start the queue cleanup task if not already running."""
        if (
            not self._ma_controls_playback
            or not self._queue_cleanup_pending
            or self._queue_cleanup_lock.locked()
        ):
            return

        self.mass.create_task(
            self._cleanup_heos_queue(),
            task_id=f"heos_queue_cleanup_task_{self.player_id}",
        )

    async def _cleanup_heos_queue(self) -> None:
        async with self._queue_cleanup_lock:
            if not self._ma_controls_playback:
                self._queue_cleanup_pending = False
                return
            if not self._queue_cleanup_pending:
                return
            if self._attr_playback_state != PlaybackState.PLAYING:
                self.logger.debug(
                    "[%s] Queue cleanup postponed (state=%s)",
                    self._device.name,
                    self._attr_playback_state,
                )
                return
            try:
                self.logger.debug("[%s] Queue cleanup started", self._device.name)
                queue_items = await self._heos_queue.player_get_queue(self._device.player_id)
                now_playing = await self._heos_queue.get_now_playing_media(self._device.player_id)
                current_queue_id = now_playing.queue_id
                if current_queue_id is None:
                    self.logger.debug(
                        "[%s] Queue cleanup postponed (no current qid yet)",
                        self._device.name,
                    )
                    self._schedule_queue_cleanup()
                    return

                queue_ids_to_remove = [
                    item.queue_id for item in queue_items if item.queue_id != current_queue_id
                ]
                self.logger.debug(
                    "[%s] Queue cleanup removing %s (current qid=%s)",
                    self._device.name,
                    queue_ids_to_remove,
                    current_queue_id,
                )
                if queue_ids_to_remove:
                    await self._heos_queue.player_remove_from_queue(
                        self._device.player_id, queue_ids_to_remove
                    )
                self._queue_cleanup_pending = False

            except HeosError as err:
                self.logger.warning(
                    "[%s] Failed to handle HEOS queue after queue change: %s",
                    self._device.name,
                    err,
                )

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET MEMBERS command on player."""
        if player_ids_to_add is None and player_ids_to_remove is None:
            return

        members: list[str] = copy(self._attr_group_members)

        #  Make sure we are always in the group
        if self.player_id not in members:
            members = [self.player_id, *members]

        for added_player_id in player_ids_to_add or []:
            members.append(added_player_id)

        for removed_player_id in player_ids_to_remove or []:
            members.remove(removed_player_id)

        if len(members) <= 1:
            await self._heos.remove_group(self._device.player_id)
        else:
            await self._heos.set_group([int(player) for player in members])
        # group_members will be updated when group_changed event is handled

    async def select_source(self, source: str) -> None:
        """Handle SELECT SOURCE command on the player."""
        self.logger.debug("[%s] Selecting source %s", self._device.name, source)
        self._cancel_ma_playback_transition()
        self._ma_controls_playback = False
        self._queue_cleanup_pending = False
        await self._device.play_input_source(source)

    def _cancel_ma_playback_transition(self) -> None:
        """Cancel the transition to MA-controlled playback."""
        self.mass.cancel_timer(self._ma_playback_transition_timer_id)
        self._ma_playback_starting = False

    def _finish_ma_playback_transition(self) -> None:
        """Apply the latest HEOS state after MA playback starts."""
        self._ma_playback_starting = False
        self._update_player_current_media()
        self.update_state()
