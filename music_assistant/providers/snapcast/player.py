"""Snapcast Player implementation."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

import aiofiles
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_OUTPUT_CODEC,
    create_sample_rates_config_entry,
)
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .constants import SnapCastStreamType

if TYPE_CHECKING:
    from snapcast.control.client import Snapclient
    from snapcast.control.group import Snapgroup

    from .provider import SnapcastPlayerProvider


SNAPCAST_FORMAT = AudioFormat(
    content_type=ContentType.FLAC,
    sample_rate=48000,
    bit_depth=16,
    channels=2,
)


class SnapcastPlayer(Player):
    """Snapcast Player implementation."""

    def __init__(
        self,
        provider: SnapcastPlayerProvider,
        client: Snapclient,
        group: Snapgroup,
    ) -> None:
        """Initialize SnapcastPlayer."""
        super().__init__(provider, f"snapcast_{client.identifier}")
        self.client = client
        self.group = group
        self._current_stream_task: asyncio.Task | None = None

        # Set player attributes
        self._attr_type = PlayerType.PLAYER
        self._attr_name = client.name or f"Snapcast {client.identifier}"
        self._attr_available = client.connected
        self._attr_device_info = DeviceInfo(
            model="Snapcast Client",
            manufacturer="Snapcast",
            ip_address=client.host.host if client.host else None,
        )
        self._attr_supported_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.SET_MEMBERS,
        }
        self._attr_can_group_with = {provider.instance_id}
        self._attr_volume_level = client.volume.percent if client.volume else 50
        self._attr_volume_muted = client.volume.muted if client.volume else False
        self._attr_playback_state = PlaybackState.IDLE

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        return [
            *await super().get_config_entries(),
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            CONF_ENTRY_OUTPUT_CODEC,
            create_sample_rates_config_entry(
                supported_sample_rates=[44100, 48000],
                supported_bit_depths=[16, 24],
                hidden=False,
            ),
            ConfigEntry(
                key="snapcast_latency",
                type=ConfigEntryType.INTEGER,
                default_value=0,
                label="Latency compensation (ms)",
                description="Latency compensation for this client in milliseconds",
                range=(-2000, 2000),
            ),
        ]

    async def stop(self) -> None:
        """Send STOP command to player."""
        # Stop any current streaming task
        if self._current_stream_task and not self._current_stream_task.done():
            self._current_stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._current_stream_task
            self._current_stream_task = None

        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self.update_state()

    async def play(self) -> None:
        """Send PLAY command to player."""
        # Snapcast doesn't have traditional play/pause - it's stream-based
        # We just update the state to indicate we're ready to play
        if self._attr_current_media:
            self._attr_playback_state = PlaybackState.PLAYING
        else:
            self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    async def pause(self) -> None:
        """Send PAUSE command to player."""
        # Snapcast doesn't support traditional pause
        # We treat pause as a temporary stop
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to player."""
        try:
            await self.client.set_volume(volume_level)
            self._attr_volume_level = volume_level
            self._attr_volume_muted = volume_level == 0
            self.update_state()
        except Exception as err:
            raise PlayerCommandFailed(f"Failed to set volume: {err}") from err

    async def volume_mute(self, muted: bool) -> None:
        """Send VOLUME MUTE command to player."""
        try:
            await self.client.set_muted(muted)
            self._attr_volume_muted = muted
            self.update_state()
        except Exception as err:
            raise PlayerCommandFailed(f"Failed to set mute: {err}") from err

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on the player."""
        # Stop any current stream
        await self.stop()

        # Determine which stream to use
        if media.media_type == MediaType.ANNOUNCEMENT:
            stream_type = SnapCastStreamType.ANNOUNCEMENT
            target_stream = await self.provider.get_announcement_stream()
        else:
            stream_type = SnapCastStreamType.MUSIC
            target_stream = await self.provider.get_music_stream()

        if not target_stream:
            raise PlayerCommandFailed(f"No {stream_type} stream available")

        # Set the group to use the appropriate stream
        try:
            await self.group.set_stream(target_stream.identifier)
        except Exception as err:
            raise PlayerCommandFailed(f"Failed to set stream: {err}") from err

        # Get the pipe path for this stream type
        pipe_path = self.provider.get_stream_pipe_path(stream_type)
        if not pipe_path:
            raise PlayerCommandFailed(f"No pipe available for {stream_type} stream")

        # Start streaming to the pipe
        self._current_stream_task = asyncio.create_task(self._stream_to_pipe(media, pipe_path))

        # Update player state
        self._attr_current_media = media
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def _stream_to_pipe(self, media: PlayerMedia, pipe_path: str) -> None:
        """Stream audio to the snapcast pipe."""
        try:
            # Select audio source
            if media.media_type == MediaType.ANNOUNCEMENT:
                # For announcements, get the audio stream directly
                assert media.custom_data
                audio_source = self.mass.streams.get_announcement_stream(
                    media.custom_data["url"],
                    output_format=SNAPCAST_FORMAT,
                    use_pre_announce=media.custom_data.get("use_pre_announce", True),
                )
            elif media.queue_id and media.queue_item_id:
                # Regular queue stream
                queue = self.mass.player_queues.get(media.queue_id)
                assert queue
                start_queue_item = self.mass.player_queues.get_item(
                    media.queue_id, media.queue_item_id
                )
                assert start_queue_item
                audio_source = self.mass.streams.get_queue_flow_stream(
                    queue=queue,
                    start_queue_item=start_queue_item,
                    pcm_format=SNAPCAST_FORMAT,
                )
            else:
                # Direct URL/file
                audio_source = get_ffmpeg_stream(
                    audio_input=media.uri,
                    input_format=AudioFormat(content_type=ContentType.try_parse(media.uri)),
                    output_format=SNAPCAST_FORMAT,
                )

            # Stream to pipe
            async with aiofiles.open(pipe_path, "wb") as pipe:
                async for chunk in audio_source:
                    await pipe.write(chunk)
                    await pipe.flush()

                    # Check if we should stop
                    if self._current_stream_task and self._current_stream_task.cancelled():
                        break

        except asyncio.CancelledError:
            # Stream was cancelled
            self.logger.debug("Stream to pipe cancelled")
        except Exception as err:
            self.logger.error("Error streaming to pipe %s: %s", pipe_path, err)
            # Update state to indicate error
            self._attr_playback_state = PlaybackState.IDLE
            self.update_state()
        finally:
            # Clean up
            if self._current_stream_task and not self._current_stream_task.cancelled():
                self._attr_playback_state = PlaybackState.IDLE
                self._attr_current_media = None
                self.update_state()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        try:
            # Process removals first
            if player_ids_to_remove:
                for player_id in player_ids_to_remove:
                    client_id = player_id.replace("snapcast_", "")
                    if client_id != self.client.identifier:
                        # Find the client and move it to its own group
                        target_client = None
                        for group in self.provider.snapcast.groups:
                            for client in group.clients:
                                if client.identifier == client_id:
                                    target_client = client
                                    break
                            if target_client:
                                break

                        if target_client:
                            # Create or find a single-client group for this client
                            await target_client.set_group(client_id)

            # Process additions
            if player_ids_to_add:
                for player_id in player_ids_to_add:
                    client_id = player_id.replace("snapcast_", "")
                    if client_id != self.client.identifier:
                        # Find the client and move it to this group
                        target_client = None
                        for group in self.provider.snapcast.groups:
                            for client in group.clients:
                                if client.identifier == client_id:
                                    target_client = client
                                    break
                            if target_client:
                                break

                        if target_client:
                            await target_client.set_group(self.group.identifier)

        except Exception as err:
            raise PlayerCommandFailed(f"Failed to set members: {err}") from err

    async def poll(self) -> None:
        """Poll player for state updates."""
        try:
            # Update basic client state
            self._attr_available = self.client.connected
            if self.client.volume:
                self._attr_volume_level = self.client.volume.percent
                self._attr_volume_muted = self.client.volume.muted

            # Update playback state based on stream activity and our streaming task
            if self._current_stream_task and not self._current_stream_task.done():
                self._attr_playback_state = PlaybackState.PLAYING
            elif self.group.stream:
                # Check if there's activity on the stream
                if hasattr(self.group.stream, "status"):
                    if self.group.stream.status == "playing":
                        self._attr_playback_state = PlaybackState.PLAYING
                    elif self.group.stream.status == "idle":
                        self._attr_playback_state = PlaybackState.IDLE
                    else:
                        self._attr_playback_state = PlaybackState.PAUSED
                else:
                    # No status info, assume idle if no active stream task
                    self._attr_playback_state = PlaybackState.IDLE
            else:
                self._attr_playback_state = PlaybackState.IDLE

            self.update_state()

        except Exception as err:
            self.logger.debug("Error polling snapcast client: %s", err)

    def update_from_client(self, client: Snapclient, group: Snapgroup) -> None:
        """Update player from snapcast client data."""
        self.client = client
        self.group = group

        # Update attributes
        self._attr_name = client.name or f"Snapcast {client.identifier}"
        self._attr_available = client.connected

        if client.volume:
            self._attr_volume_level = client.volume.percent
            self._attr_volume_muted = client.volume.muted

        # Update device info if IP changed
        if client.host and client.host.host != self.device_info.ip_address:
            self._attr_device_info = DeviceInfo(
                model="Snapcast Client",
                manufacturer="Snapcast",
                ip_address=client.host.host,
            )

        self.update_state()
