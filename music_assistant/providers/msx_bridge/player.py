"""MSX Player implementation."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

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
            PlayerFeature.VOLUME_SET,
        }
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

    async def play(self) -> None:
        """Handle PLAY (resume) command."""
        self.logger.info("play (resume) on %s", self.display_name)
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    async def pause(self) -> None:
        """Handle PAUSE command."""
        self.logger.info("pause on %s", self.display_name)
        # Snapshot the elapsed time before pausing
        if self._attr_elapsed_time is not None and self._attr_elapsed_time_last_updated is not None:
            self._attr_elapsed_time += time.time() - self._attr_elapsed_time_last_updated
        self._attr_playback_state = PlaybackState.PAUSED
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    async def stop(self) -> None:
        """Handle STOP command."""
        self.logger.info("stop on %s", self.display_name)
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self._attr_elapsed_time = None
        self._attr_elapsed_time_last_updated = None
        self.current_stream_url = None
        self.update_state()
        cast("MSXBridgeProvider", self.provider).notify_play_stopped(self.player_id)

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
