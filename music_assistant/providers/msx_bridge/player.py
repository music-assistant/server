"""MSX Player implementation."""

from __future__ import annotations

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

    def __init__(
        self,
        provider: MSXBridgeProvider,
        player_id: str,
        name: str = "MSX TV",
        output_format: str = "mp3",
    ) -> None:
        """Initialize the MSX Player."""
        super().__init__(provider, player_id)
        self._attr_name = name
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.PAUSE,
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.VOLUME_SET,
        }
        self._attr_can_group_with = {provider.instance_id}
        self._attr_device_info = DeviceInfo(
            model="Smart TV (MSX)",
            manufacturer="MSX Bridge",
        )
        self._attr_available = True
        self._attr_powered = True
        self._attr_volume_level = 100
        self.output_format = output_format

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
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

        # In flow_mode, PlayerMedia has title="Music Assistant" and no metadata.
        # Resolve real metadata from the queue item when available.
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

        cast("MSXBridgeProvider", self.provider).notify_play_started(
            self.player_id,
            title=title,
            artist=artist,
            image_url=image_url,
            duration=duration,
        )

        await self._propagate_to_group_members("play_media", media=media)

    def _get_group_member_ids(self) -> list[str]:
        """
        Get IDs of all players in our group (excluding self).

        Handles both direct grouping (our group_members) and SyncGroup (active_group).
        """
        if self.synced_to is not None:
            return []
        member_ids: list[str] = []
        if self.active_group:
            group_player = self.mass.players.get(self.active_group)
            if group_player:
                member_ids = list(group_player.group_members)
        if not member_ids:
            member_ids = list(self.group_members)
        return [x for x in member_ids if x != self.player_id]

    async def _propagate_to_group_members(self, command: str, **kwargs: Any) -> None:
        """Propagate command to group members when we are the leader."""
        for member_id in self._get_group_member_ids():
            member = self.mass.players.get(member_id)
            if not member or not isinstance(member, MSXPlayer) or not member.available:
                continue
            try:
                if command == "play_media":
                    media = kwargs.get("media")
                    if media:
                        # Call member.play_media directly — mass.players.play_media would
                        # redirect synced/grouped players back to the leader
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
        """Resume playback after pause — queue re-sends current track to MSX."""
        try:
            await self.mass.player_queues.resume(self.player_id)
        except Exception:
            self.logger.warning(
                "resume from pause failed, falling back to play state only",
                exc_info=True,
            )
            self._attr_playback_state = PlaybackState.PLAYING
            self._attr_elapsed_time_last_updated = time.time()
            self.update_state()

    async def pause(self) -> None:
        """Handle PAUSE command — stop playback on MSX but keep queue/position for resume."""
        self.logger.info("pause on %s", self.display_name)
        # Snapshot the elapsed time before pausing
        if self._attr_elapsed_time is not None and self._attr_elapsed_time_last_updated is not None:
            self._attr_elapsed_time += time.time() - self._attr_elapsed_time_last_updated
        self._attr_playback_state = PlaybackState.PAUSED
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()
        cast("MSXBridgeProvider", self.provider).notify_play_stopped(self.player_id)
        await self._propagate_to_group_members("pause")

    async def stop(self) -> None:
        """Handle STOP command."""
        self.logger.info("stop on %s", self.display_name)
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self._attr_elapsed_time = None
        self._attr_elapsed_time_last_updated = None
        self.current_stream_url = None
        self.update_state()
        provider = cast("MSXBridgeProvider", self.provider)
        provider.notify_play_stopped(self.player_id)
        await self._propagate_to_group_members("stop")

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command."""
        self._attr_volume_level = volume_level
        self.update_state()

    async def poll(self) -> None:
        """Poll player for state updates."""
        # For now, update elapsed time during playback
        if (
            self._attr_playback_state == PlaybackState.PLAYING
            and self._attr_elapsed_time is not None
            and self._attr_elapsed_time_last_updated is not None
        ):
            now = time.time()
            self._attr_elapsed_time += now - self._attr_elapsed_time_last_updated
            self._attr_elapsed_time_last_updated = now
            self.update_state()
