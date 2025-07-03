"""Squeezelite Player implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aioslimproto.client import PlayerState as SlimPlayerState
from aioslimproto.client import SlimClient
from aioslimproto.models import EventType as SlimEventType
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import (
    CONF_ENTRY_DEPRECATED_EQ_BASS,
    CONF_ENTRY_DEPRECATED_EQ_MID,
    CONF_ENTRY_DEPRECATED_EQ_TREBLE,
    CONF_ENTRY_HTTP_PROFILE_FORCED_2,
    CONF_ENTRY_OUTPUT_CODEC,
    CONF_ENTRY_SYNC_ADJUST,
    DEFAULT_PCM_FORMAT,
)
from music_assistant.helpers.util import TaskManager
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

if TYPE_CHECKING:
    from .provider import SqueezelitePlayerProvider


STATE_MAP = {
    SlimPlayerState.BUFFERING: PlaybackState.PLAYING,
    SlimPlayerState.BUFFER_READY: PlaybackState.PLAYING,
    SlimPlayerState.PAUSED: PlaybackState.PAUSED,
    SlimPlayerState.PLAYING: PlaybackState.PLAYING,
    SlimPlayerState.STOPPED: PlaybackState.IDLE,
}


class SqueezelitePlayer(Player):
    """Squeezelite Player implementation."""

    def __init__(
        self,
        provider: SqueezelitePlayerProvider,
        player_id: str,
        slimplayer: SlimClient,
    ) -> None:
        """Initialize the Squeezelite Player."""
        super().__init__(provider, player_id)
        self.slimplayer = slimplayer

        # Set player attributes
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.POWER,
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.MULTI_DEVICE_DSP,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.PAUSE,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.ENQUEUE,
            PlayerFeature.GAPLESS_PLAYBACK,
        }
        self._attr_name = slimplayer.name
        self._attr_available = True
        self._attr_powered = slimplayer.powered
        self._attr_device_info = DeviceInfo(
            model=slimplayer.device_model,
            ip_address=slimplayer.device_address,
            manufacturer=slimplayer.device_type,
        )
        self._attr_can_group_with = {provider.instance_id}

    async def setup(self) -> None:
        """Set up the player."""
        await self.mass.players.register_or_update(self)

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        return [
            *await super().get_config_entries(),
            CONF_ENTRY_HTTP_PROFILE_FORCED_2,
            CONF_ENTRY_OUTPUT_CODEC,
            CONF_ENTRY_SYNC_ADJUST,
            CONF_ENTRY_DEPRECATED_EQ_BASS,
            CONF_ENTRY_DEPRECATED_EQ_MID,
            CONF_ENTRY_DEPRECATED_EQ_TREBLE,
        ]

    async def handle_slim_event(self, event: SlimEventType) -> None:
        """Handle player update from slimproto server."""
        # Update player state from slim player
        self._attr_available = True
        self._attr_name = self.slimplayer.name
        self._attr_powered = self.slimplayer.powered
        self._attr_playback_state = STATE_MAP[self.slimplayer.state]
        self._attr_volume_level = self.slimplayer.volume_level
        self._attr_volume_muted = self.slimplayer.muted
        self._attr_active_source = self.player_id

        # Update current media if available
        if self.slimplayer.current_media and (metadata := self.slimplayer.current_media.metadata):
            self._attr_current_media = PlayerMedia(
                uri=metadata.get("item_id"),
                title=metadata.get("title"),
                album=metadata.get("album"),
                artist=metadata.get("artist"),
                image_url=metadata.get("image_url"),
                duration=metadata.get("duration"),
                queue_id=metadata.get("queue_id"),
                queue_item_id=metadata.get("queue_item_id"),
            )
        else:
            self._attr_current_media = None

        self.update_state()

    async def power(self, powered: bool) -> None:
        """Handle POWER command on the player."""
        if powered:
            await self.slimplayer.power_on()
        else:
            await self.slimplayer.power_off()

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        await self.slimplayer.volume_set(volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        await self.slimplayer.volume_mute(muted)

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        async with TaskManager(self.mass) as tg:
            for slimplayer in self.provider._get_sync_clients(self.player_id):
                tg.create_task(slimplayer.stop())

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        async with TaskManager(self.mass) as tg:
            for slimplayer in self.provider._get_sync_clients(self.player_id):
                tg.create_task(slimplayer.play())

    async def pause(self) -> None:
        """Handle PAUSE command on the player."""
        async with TaskManager(self.mass) as tg:
            for slimplayer in self.provider._get_sync_clients(self.player_id):
                tg.create_task(slimplayer.pause())

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on the player."""
        if self.synced_to:
            msg = "A synced player cannot receive play commands directly"
            raise RuntimeError(msg)

        if not self.group_members:
            # Simple, single-player playback
            await self._handle_play_url(
                self.slimplayer,
                url=media.uri,
                media=media,
                send_flush=True,
                auto_play=False,
            )
            return

        # This is a syncgroup, we need to handle this with a multi client stream
        master_audio_format = AudioFormat(
            content_type=DEFAULT_PCM_FORMAT.content_type,
            sample_rate=48000,  # Default for squeezelite
            bit_depth=16,
            channels=2,
        )

        # Start multi-client stream for sync group
        await self._handle_multi_client_stream(media, master_audio_format)

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing next media item."""
        if self.synced_to:
            msg = "A synced player cannot receive enqueue commands directly"
            raise RuntimeError(msg)

        # Handle enqueue for single player or sync group
        if not self.group_members:
            await self._handle_play_url(
                self.slimplayer,
                url=media.uri,
                media=media,
                send_flush=False,
                auto_play=True,
            )
        else:
            # Handle multi-client enqueue
            await self._handle_multi_client_enqueue(media)

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        if player_ids_to_add:
            for player_id in player_ids_to_add:
                if slave_player := self.mass.players.get(player_id):
                    slave_player._attr_synced_to = self.player_id
                    slave_player.update_state()

        if player_ids_to_remove:
            for player_id in player_ids_to_remove:
                if slave_player := self.mass.players.get(player_id):
                    slave_player._attr_synced_to = None
                    slave_player.update_state()

    async def poll(self) -> None:
        """Poll player for state updates."""
        # Squeezelite players are event-driven, no polling needed

    async def _handle_play_url(
        self,
        slimplayer: SlimClient,
        url: str,
        media: PlayerMedia,
        send_flush: bool = True,
        auto_play: bool = True,
    ) -> None:
        """Handle playing a URL on a slimplayer."""
        if send_flush:
            await slimplayer.flush()

        # Send play command with metadata
        metadata = {
            "item_id": media.uri,
            "title": media.title,
            "album": media.album,
            "artist": media.artist,
            "image_url": media.image_url,
            "duration": media.duration,
            "queue_id": media.queue_id,
            "queue_item_id": media.queue_item_id,
        }

        await slimplayer.play_url(url, metadata=metadata, auto_play=auto_play)

    async def _handle_multi_client_stream(
        self, media: PlayerMedia, master_audio_format: AudioFormat
    ) -> None:
        """Handle multi-client stream for sync groups."""
        # This would need implementation of the multi-client streaming logic
        # For now, simplified implementation
        sync_clients = list(self.provider._get_sync_clients(self.player_id))

        # Play on all sync clients
        async with TaskManager(self.mass) as tg:
            for slimclient in sync_clients:
                tg.create_task(
                    self._handle_play_url(
                        slimclient,
                        media.uri,
                        media,
                        send_flush=True,
                        auto_play=False,
                    )
                )

    async def _handle_multi_client_enqueue(self, media: PlayerMedia) -> None:
        """Handle multi-client enqueue for sync groups."""
        sync_clients = list(self.provider._get_sync_clients(self.player_id))

        # Enqueue on all sync clients
        async with TaskManager(self.mass) as tg:
            for slimclient in sync_clients:
                tg.create_task(
                    self._handle_play_url(
                        slimclient,
                        media.uri,
                        media,
                        send_flush=False,
                        auto_play=True,
                    )
                )
