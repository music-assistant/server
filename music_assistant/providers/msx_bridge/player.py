"""MSX Player implementation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo

from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from .provider import MSXBridgeProvider


class MSXPlayer(Player):
    """Represents a Smart TV running MSX as a Music Assistant player."""

    current_stream_url: str | None = None
    output_format: str = "mp3"
    _skip_ws_notify: bool = False
    _propagating: bool = False
    _playing_from_queue: bool = False
    _queue_source_id: str | None = None
    _playlist_offset: int = 0
    _playlist_size: int = 0
    _media_ready: asyncio.Event
    _last_ws_position: float | None = None

    def __init__(
        self,
        provider: MSXBridgeProvider,
        player_id: str,
        name: str = "MSX TV",
        output_format: str = "mp3",
        *,
        grouping_enabled: bool = True,
    ) -> None:
        """Initialize the MSX Player."""
        super().__init__(provider, player_id)
        self._attr_name = name
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.PAUSE,
            PlayerFeature.VOLUME_SET,
        }
        if grouping_enabled:
            self._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
            self._attr_can_group_with = {provider.instance_id}
        self._attr_device_info = DeviceInfo(
            model="Smart TV (MSX)",
            manufacturer="MSX Bridge",
        )
        self._attr_available = True
        self._attr_powered = True
        self._attr_volume_level = 100
        self.output_format = output_format
        self._media_ready = asyncio.Event()

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

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA command — store stream URL for the TV to fetch."""
        self.logger.info("play_media on %s: uri=%s", self.display_name, media.uri)
        self.current_stream_url = media.uri
        self._attr_current_media = media
        self._media_ready.set()
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_elapsed_time = 0.0
        self._attr_elapsed_time_last_updated = time.time()
        self._last_ws_position = None
        self.update_state()

        if not self._skip_ws_notify:
            self._notify_msx_playback(media)

        await self._propagate_to_group_members("play_media", media=media)

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
            title, artist, image_url, duration = self._resolve_media_metadata(media)
            next_action = f"request:interaction:/api/next/{self.player_id}"
            prev_action = f"request:interaction:/api/previous/{self.player_id}"
            provider.notify_play_started(
                self.player_id,
                title=title,
                artist=artist,
                image_url=image_url,
                duration=duration,
                next_action=next_action,
                prev_action=prev_action,
            )

    def _notify_same_queue(self, provider: MSXBridgeProvider, source_id: str) -> None:
        """Handle same-queue playback: goto index or re-send if queue changed."""
        queue = self.mass.player_queues.get(source_id)
        ma_index = getattr(queue, "current_index", 0) if queue else 0
        try:
            queue_items = self.mass.player_queues.items(source_id)
            current_size = len(list(queue_items))
        except Exception:
            current_size = self._playlist_size
        if current_size != self._playlist_size:
            self._playlist_size = current_size
            self._playlist_offset = ma_index
            provider.notify_play_playlist(self.player_id, ma_index)
        else:
            if self._playlist_size > 0:
                msx_index = (ma_index - self._playlist_offset) % self._playlist_size
            else:
                msx_index = ma_index
            provider.notify_goto_index(self.player_id, msx_index)

    def _notify_new_queue(self, provider: MSXBridgeProvider, source_id: str) -> None:
        """Send full MSX native playlist for a new queue."""
        queue = self.mass.player_queues.get(source_id)
        start_index = getattr(queue, "current_index", 0) if queue else 0
        try:
            queue_items = self.mass.player_queues.items(source_id)
            self._playlist_size = len(list(queue_items))
        except Exception:
            self._playlist_size = 0
        self._playlist_offset = start_index
        self._queue_source_id = source_id
        provider.notify_play_playlist(self.player_id, start_index)
        self._playing_from_queue = True

    def _resolve_media_metadata(
        self, media: PlayerMedia
    ) -> tuple[str | None, str | None, str | None, int | None]:
        """Resolve detailed metadata from the queue item when available."""
        title = media.title
        artist = media.artist
        image_url = media.image_url
        duration = media.duration
        if media.source_id and media.queue_item_id:
            queue_item = self.mass.player_queues.get_item(media.source_id, media.queue_item_id)
            if queue_item:
                if queue_item.media_item:
                    title = getattr(queue_item.media_item, "name", None) or title
                    artist = getattr(queue_item.media_item, "artist_str", None) or artist
                    duration = getattr(queue_item.media_item, "duration", None) or duration
                if queue_item.image:
                    image_url = self.mass.metadata.get_image_url(
                        queue_item.image, size=500, prefer_stream_server=True
                    )
                if duration is None and queue_item.duration:
                    duration = queue_item.duration
                if title is None and queue_item.name:
                    title = queue_item.name
        return title, artist, image_url, duration

    def _get_group_member_ids(self) -> list[str]:
        """Get IDs of group members (excluding self).

        Only returns members when this player is the sync leader.
        MA's SyncGroupPlayer forwards play_media to the sync leader,
        whose group_members already contains all SyncGroup members.
        """
        if self.synced_to is not None:
            return []
        return [x for x in self.group_members if x != self.player_id]

    async def _propagate_to_group_members(self, command: str, **kwargs: Any) -> None:
        """Propagate command to group members when we are the leader."""
        # Skip if grouping is disabled at provider level
        provider = cast("MSXBridgeProvider", self.provider)
        if not provider.grouping_enabled:
            return
        # Prevent infinite recursion if member.play_media triggers propagation back
        if self._propagating:
            return
        self._propagating = True
        try:
            for member_id in self._get_group_member_ids():
                member = self.mass.players.get(member_id)
                if not member or not isinstance(member, MSXPlayer) or not member.available:
                    continue
                try:
                    if command == "play_media":
                        media = kwargs.get("media")
                        if media:
                            # Call member.play_media directly — mass.players.play_media
                            # would redirect synced/grouped players back to the leader
                            await member.play_media(media)
                    elif command == "stop":
                        await member.stop()
                    elif command == "pause":
                        await member.pause()
                    elif command == "play":
                        await member.play()
                except Exception:
                    self.logger.warning(
                        "Failed to propagate %s to member %s",
                        command,
                        member_id,
                        exc_info=True,
                    )
        finally:
            self._propagating = False

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS — update group membership."""
        for pid in player_ids_to_remove or []:
            if pid in self._attr_group_members:
                self._attr_group_members.remove(pid)
        for pid in player_ids_to_add or []:
            if pid != self.player_id and pid not in self._attr_group_members:
                other = self.mass.players.get(pid)
                if other and isinstance(other, MSXPlayer):
                    self._attr_group_members.append(pid)
        self.update_state()

    async def play(self) -> None:
        """Handle PLAY (resume) command."""
        self.logger.info("play (resume) on %s", self.display_name)
        if self._attr_playback_state == PlaybackState.PAUSED:
            await self._resume_from_pause()
            return
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()
        await self._propagate_to_group_members("play")

    async def _resume_from_pause(self) -> None:
        """Resume playback after pause — tell MSX to unpause its native player.

        Note: the HTTP audio stream stays open during pause. For short pauses
        the chunk buffer (maxsize=32) absorbs the gap. Long pauses (minutes)
        may cause stream starvation — ffmpeg backs up, and MSX may get silence
        or a playback error on resume. A reconnect mechanism would be needed
        for reliable long-pause support.
        """
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_elapsed_time_last_updated = time.time()
        self._last_ws_position = None
        self.update_state()
        if not self._skip_ws_notify:
            cast("MSXBridgeProvider", self.provider).notify_play_resumed(self.player_id)
        await self._propagate_to_group_members("play")

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
        await self._propagate_to_group_members("pause")

    async def stop(self) -> None:
        """Handle STOP command."""
        self.logger.info("stop on %s", self.display_name)
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self._media_ready.clear()
        self._attr_elapsed_time = None
        self._attr_elapsed_time_last_updated = None
        self._last_ws_position = None
        self.current_stream_url = None
        self._playing_from_queue = False
        self._queue_source_id = None
        self._playlist_offset = 0
        self._playlist_size = 0
        self.update_state()
        provider = cast("MSXBridgeProvider", self.provider)
        provider.notify_play_stopped(self.player_id)
        await self._propagate_to_group_members("stop")

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command."""
        self._attr_volume_level = volume_level
        self.update_state()

    def update_position(self, position: float) -> None:
        """Update elapsed time from a WebSocket position report.

        Only accepts updates while PLAYING — late reports arriving after
        pause() would overwrite the correctly accumulated elapsed_time.
        """
        if self._attr_playback_state != PlaybackState.PLAYING:
            return
        self._attr_elapsed_time = position
        self._attr_elapsed_time_last_updated = time.time()
        self._last_ws_position = time.time()
        self.update_state()

    async def poll(self) -> None:
        """Poll player for state updates.

        If a recent WebSocket position report was received (within 10s),
        skip wall-clock increment — the WS data is more accurate.
        """
        if (
            self._attr_playback_state == PlaybackState.PLAYING
            and self._attr_elapsed_time is not None
            and self._attr_elapsed_time_last_updated is not None
        ):
            # Skip wall-clock update if WS reported position recently
            if self._last_ws_position and (time.time() - self._last_ws_position) < 10:
                return
            now = time.time()
            self._attr_elapsed_time += now - self._attr_elapsed_time_last_updated
            self._attr_elapsed_time_last_updated = now
            self.update_state()

    async def wait_for_media(self, timeout: float = 10.0) -> PlayerMedia | None:
        """Wait for play_media() to set current_media, with timeout.

        Fast path: if play_media() already ran (e.g. during queue.play_media),
        current_media is set — return immediately without waiting.
        Slow path: clear the event and wait for play_media() to signal.
        """
        if self._attr_current_media is not None and self._media_ready.is_set():
            return self._attr_current_media
        self._media_ready.clear()
        try:
            await asyncio.wait_for(self._media_ready.wait(), timeout=timeout)
        except TimeoutError:
            return None
        return self._attr_current_media
