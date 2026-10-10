"""MSX Player implementation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.player import DeviceInfo

from music_assistant.constants import (
    CONF_ENTRY_HTTP_PROFILE_DEFAULT_1,
    CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3,
)
from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

    from .provider import MSXBridgeProvider


class MSXPlayer(Player):
    """Represents a Smart TV running MSX as a Music Assistant player."""

    output_format: str = "mp3"
    _skip_ws_depth: int = 0
    _accepted_position: bool = False
    _playing_from_queue: bool = False
    _queue_source_id: str | None = None
    _media_ready: asyncio.Event
    prepare_lock: asyncio.Lock
    _attr_elapsed_time: float | None = None
    _attr_elapsed_time_last_updated: float | None = None
    _last_ws_position: float | None = None
    _track_started_at: float = 0.0
    _native_completion_token: str | None = None
    playback_generation: str | None = None

    def __init__(
        self,
        provider: MSXBridgeProvider,
        player_id: str,
        name: str = "MSX TV",
        output_format: str = "mp3",
        *,
        ip_address: str | None = None,
    ) -> None:
        """Initialize the MSX Player."""
        super().__init__(provider, player_id)
        self._attr_name = name
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.PAUSE,
            PlayerFeature.SEEK,
            PlayerFeature.VOLUME_SET,
        }
        self._attr_device_info = DeviceInfo(
            model="Smart TV (MSX)",
            manufacturer="MSX Bridge",
        )
        if ip_address:
            self._attr_device_info.ip_address = ip_address
        self._attr_available = True
        self._attr_powered = True
        self._attr_volume_level = 100
        self.output_format = output_format
        self._media_ready = asyncio.Event()
        self.prepare_lock = asyncio.Lock()
        self._skip_ws_depth = 0
        self._accepted_position = False

    @property
    def requires_flow_mode(self) -> bool:
        """MSX plays individual tracks — flow mode breaks progress tracking."""
        return False

    @property
    def needs_poll(self) -> bool:
        """Return if the player needs to be polled for state updates."""
        return True

    @property
    def poll_interval(self) -> int:
        """Return poll interval in seconds."""
        return 5 if self.playback_state == PlaybackState.PLAYING else 30

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return per-player config entries — codec is configurable per TV."""
        return [CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3, CONF_ENTRY_HTTP_PROFILE_DEFAULT_1]

    def mark_available(self) -> None:
        """Mark the player available after proof of life from the TV."""
        if not self._attr_available:
            self._attr_available = True
            self.update_state()

    def on_ws_connected(self) -> None:
        """Mark player as available when a WebSocket client connects."""
        self.mark_available()

    def on_ws_disconnected(self) -> None:
        """Mark a playing player unavailable when its last WebSocket client disconnects."""
        if self._attr_playback_state == PlaybackState.PLAYING:
            self._attr_available = False
            self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA command — store stream URL for the TV to fetch."""
        if not self.config.enabled:
            raise PlayerUnavailableError("Player is disabled")
        self.logger.info("play_media on %s: uri=%s", self.display_name, media.uri)
        self._native_completion_token = None
        self.playback_generation = uuid4().hex
        self._attr_current_media = media
        self._media_ready.set()
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_elapsed_time = 0.0
        self._attr_elapsed_time_last_updated = time.time()
        self._last_ws_position = None
        self._track_started_at = time.monotonic()
        self._accepted_position = False
        self.update_state()

        if self._skip_ws_notify:
            cast("MSXBridgeProvider", self.provider).notify_native_track_started(self.player_id)
        else:
            self._notify_msx_playback(media)

    async def play(self) -> None:
        """Handle PLAY (resume) command."""
        if not self.config.enabled:
            raise PlayerUnavailableError("Player is disabled")
        self.logger.info("play (resume) on %s", self.display_name)
        if self._attr_playback_state == PlaybackState.PAUSED:
            await self._resume_from_pause()
            return
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    async def pause(self) -> None:
        """Handle PAUSE command — pause playback on MSX, keep stream alive for resume."""
        self.logger.info("pause on %s", self.display_name)
        # Snapshot the elapsed time before pausing
        if self._attr_elapsed_time is not None and self._attr_elapsed_time_last_updated is not None:
            self._attr_elapsed_time += time.time() - self._attr_elapsed_time_last_updated
        self._attr_playback_state = PlaybackState.PAUSED
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()
        if not self._skip_ws_notify:
            cast("MSXBridgeProvider", self.provider).notify_play_paused(self.player_id)

    async def stop(self) -> None:
        """Handle STOP command."""
        self.logger.info("stop on %s", self.display_name)
        self.mark_idle()
        provider = cast("MSXBridgeProvider", self.provider)
        provider.notify_play_stopped(self.player_id)

    def mark_idle(self) -> None:
        """Report the player idle and drop its media, without sending a stop to the TV."""
        self._attr_playback_state = PlaybackState.IDLE
        self._native_completion_token = None
        self.playback_generation = None
        self._attr_current_media = None
        self._attr_elapsed_time = None
        self._attr_elapsed_time_last_updated = None
        self._last_ws_position = None
        self._playing_from_queue = False
        self._queue_source_id = None
        self.update_state()

    def clock_context(self) -> dict[str, Any]:
        """Return source metadata separately from the decoder's stream-time clock."""
        media = self.current_media
        offset = 0.0
        if media and media.source_id and media.queue_item_id:
            item = self.mass.player_queues.get_item(media.source_id, media.queue_item_id)
            if item and item.streamdetails:
                offset = item.streamdetails.seek_position
        return {
            "playback_id": self.playback_generation,
            "source_offset": offset,
            "source_duration": media.duration if media else None,
            "served_duration": self._served_duration(),
        }

    def bind_native_completion(self, token: str, media: PlayerMedia | None) -> None:
        """Bind one native decoder request to its current media generation."""
        if media is not None and self.current_media is media and self.config.enabled:
            self._native_completion_token = token

    def claim_native_completion(self, token: str) -> bool:
        """Consume a decoder's completion once, rejecting stopped or replaced playback."""
        if (
            not token
            or token != self._native_completion_token
            or self.playback_state != PlaybackState.PLAYING
            or not self.config.enabled
        ):
            return False
        self._native_completion_token = None
        return True

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command."""
        self._attr_volume_level = volume_level
        self.update_state()

    async def seek(self, position_seconds: int) -> None:
        """Handle SEEK command — send seek action to MSX player via WebSocket."""
        self._attr_elapsed_time = float(position_seconds)
        self._attr_elapsed_time_last_updated = time.time()
        self._last_ws_position = None
        if self._track_started_at > 0:
            self._track_started_at = time.monotonic() - float(position_seconds)
        self.update_state()
        if not self._skip_ws_notify:
            cast("MSXBridgeProvider", self.provider).notify_seek(self.player_id, position_seconds)

    def update_position(self, position: float) -> None:
        """Update elapsed time from a WebSocket position report while playing."""
        # Only accepts updates while PLAYING — late reports arriving after
        # pause() would overwrite the correctly accumulated elapsed_time.
        if self._attr_playback_state != PlaybackState.PLAYING:
            return
        normalized = max(0.0, float(position))
        if self._track_started_at > 0:
            age = time.monotonic() - self._track_started_at
            if normalized > age + 2.0:
                if not self._accepted_position:
                    return
                self._track_started_at = time.monotonic() - normalized
        self._accepted_position = True
        duration = self._served_duration()
        if duration is not None:
            normalized = min(normalized, duration)
        self._attr_elapsed_time = normalized
        # elapsed_time_last_updated is compared against time.time() by MA core
        # (corrected_elapsed_time) — must stay wall-clock. The WS staleness
        # marker is provider-internal — monotonic, immune to NTP steps.
        self._attr_elapsed_time_last_updated = time.time()
        self._last_ws_position = time.monotonic()
        self.update_state()

    def note_tv_seek(self, position: float) -> None:
        """Trust a TV-initiated seek even before the first position report."""
        if self._attr_playback_state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            return
        normalized = max(0.0, float(position))
        self._accepted_position = True
        if self._track_started_at > 0:
            self._track_started_at = time.monotonic() - normalized
        duration = self._served_duration()
        if duration is not None:
            normalized = min(normalized, duration)
        self._attr_elapsed_time = normalized
        self._attr_elapsed_time_last_updated = time.time()
        self._last_ws_position = time.monotonic()
        self.update_state()

    async def poll(self) -> None:
        """
        Update the player state.

        :raises PlayerUnavailableError: If the player is unavailable.
        """
        # If a recent WebSocket position report was received (within 10s),
        # skip wall-clock increment — the WS data is more accurate.
        if not self._attr_available:
            raise PlayerUnavailableError(
                f"MSX TV {self.display_name} is offline (WebSocket disconnected)",
                translation_key="player_offline",
                translation_owner=self.translation_owner,
                translation_args=[self.display_name],
            )
        if (
            self._attr_playback_state == PlaybackState.PLAYING
            and self._attr_elapsed_time is not None
            and self._attr_elapsed_time_last_updated is not None
        ):
            # Skip wall-clock update if WS reported position recently
            if self._last_ws_position and (time.monotonic() - self._last_ws_position) < 10:
                return
            now = time.time()
            delta = now - self._attr_elapsed_time_last_updated
            new_elapsed = max(0.0, float(self._attr_elapsed_time) + float(delta))
            duration = self._served_duration()
            if duration is not None:
                new_elapsed = min(new_elapsed, duration)
            self._attr_elapsed_time = new_elapsed
            self._attr_elapsed_time_last_updated = now
            self.update_state()

    def expect_new_media(self) -> None:
        """
        Arm wait_for_media() for the next play_media() call.

        Call before requesting new playback to avoid returning the previous media.
        """
        self._media_ready.clear()

    async def wait_for_media(self, timeout: float = 10.0) -> PlayerMedia | None:
        """
        Wait for new media after arming, or return the current media.

        :param timeout: Maximum time to wait for new media, in seconds.
        :return: The current media, or None if stopped or the wait times out.
        """
        # Fast path: current_media already set and not armed via expect_new_media()
        # — return immediately. Slow path: wait for the next play_media() to signal.
        # After stop(), _attr_current_media is None — this method returns None even
        # if the event happens to still be set.
        if self._attr_current_media is not None and self._media_ready.is_set():
            return self._attr_current_media
        if not self._media_ready.is_set():
            try:
                await asyncio.wait_for(self._media_ready.wait(), timeout=timeout)
            except TimeoutError:
                return None
        return self._attr_current_media

    @contextmanager
    def suppress_ws_notify(self) -> Iterator[None]:
        """Suppress MA→MSX WebSocket echo while MSX is driving playback."""
        self._skip_ws_depth += 1
        try:
            yield
        finally:
            self._skip_ws_depth = max(0, self._skip_ws_depth - 1)

    def mark_queue_playback(self, queue_id: str) -> None:
        """Remember that MSX is rendering this MA queue as a native playlist."""
        self._playing_from_queue = True
        self._queue_source_id = queue_id

    @property
    def _skip_ws_notify(self) -> bool:
        """True while at least one suppress_ws_notify() context is active."""
        return self._skip_ws_depth > 0

    def _notify_msx_playback(self, media: PlayerMedia) -> None:
        """Send WS notification to MSX about the new playback state."""
        source_id = media.source_id
        is_queue_backed = bool(source_id and media.queue_item_id)
        is_same_queue = self._playing_from_queue and self._queue_source_id == source_id
        provider = cast("MSXBridgeProvider", self.provider)

        if is_queue_backed and is_same_queue and source_id:
            self._notify_same_queue(provider, source_id)
        elif is_queue_backed and source_id:
            self._notify_new_queue(provider, source_id)
        else:
            # Queue-backed playback renders from the MSX native playlist, which carries
            # its own per-track metadata; only standalone media needs it pushed here.
            next_action = f"execute:/api/next/{self.player_id}"
            prev_action = f"execute:/api/previous/{self.player_id}"
            provider.notify_play_started(
                self.player_id,
                title=media.title,
                artist=media.artist,
                image_url=media.image_url,
                duration=media.stream_duration or media.duration,
                next_action=next_action,
                prev_action=prev_action,
            )

    def _notify_same_queue(self, provider: MSXBridgeProvider, source_id: str) -> None:
        """Handle same-queue playback: goto index or re-send if queue changed."""
        queue = self.mass.player_queues.get(source_id)
        ma_index = queue.current_index if queue and queue.current_index is not None else 0
        provider.notify_play_playlist(self.player_id, ma_index, queue_id=source_id)

    def _notify_new_queue(self, provider: MSXBridgeProvider, source_id: str) -> None:
        """Send full MSX native playlist for a new queue."""
        queue = self.mass.player_queues.get(source_id)
        start_index = queue.current_index if queue and queue.current_index is not None else 0
        self._queue_source_id = source_id
        provider.notify_play_playlist(self.player_id, start_index, queue_id=source_id)
        self._playing_from_queue = True

    def _served_duration(self) -> float | None:
        """Return the length in seconds of the audio served to the TV, if known."""
        # The TV reports its position within that audio, which is shorter than the
        # media item itself when playback starts at a seek position.
        if (media := self._attr_current_media) is None:
            return None
        duration = media.stream_duration or media.duration
        if duration is None or duration <= 0:
            return None
        return float(duration)

    async def _resume_from_pause(self) -> None:
        """Resume playback on the native MSX player."""
        # Note: the HTTP audio stream stays open during pause. For short pauses
        # the chunk buffer (maxsize=32) absorbs the gap. Long pauses (minutes)
        # may cause stream starvation — ffmpeg backs up, and MSX may get silence
        # or a playback error on resume. A reconnect mechanism would be needed
        # for reliable long-pause support.
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_elapsed_time_last_updated = time.time()
        self._last_ws_position = None
        self.update_state()
        if not self._skip_ws_notify:
            cast("MSXBridgeProvider", self.provider).notify_play_resumed(self.player_id)
