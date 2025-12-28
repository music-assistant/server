"""Sendspin Player implementation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from io import BytesIO
from typing import TYPE_CHECKING, cast

from aiosendspin.models import MediaCommand
from aiosendspin.models.types import ArtworkSource, PlaybackStateType
from aiosendspin.models.types import RepeatMode as SendspinRepeatMode
from aiosendspin.server import AudioFormat as SendspinAudioFormat
from aiosendspin.server import (
    ClientEvent,
    GroupCommandEvent,
    GroupEvent,
    GroupStateChangedEvent,
    SendspinGroup,
    VolumeChangedEvent,
)
from aiosendspin.server.client import DisconnectBehaviour
from aiosendspin.server.events import ClientGroupChangedEvent
from aiosendspin.server.group import (
    GroupDeletedEvent,
    GroupMemberAddedEvent,
    GroupMemberRemovedEvent,
)
from aiosendspin.server.metadata import Metadata
from aiosendspin.server.stream import AudioCodec, MediaStream
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    RepeatMode,
)
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import DeviceInfo
from PIL import Image

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_HTTP_PROFILE_HIDDEN,
    CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
    CONF_ENTRY_SAMPLE_RATES,
    CONF_OUTPUT_CHANNELS,
    CONF_OUTPUT_CODEC,
    INTERNAL_PCM_FORMAT,
)
from music_assistant.helpers.audio import get_player_filter_params
from music_assistant.models.player import Player, PlayerMedia

from .timed_client_stream import TimedClientStream

# Supported group commands for Sendspin players
SUPPORTED_GROUP_COMMANDS = [
    MediaCommand.PLAY,
    MediaCommand.PAUSE,
    MediaCommand.STOP,
    MediaCommand.NEXT,
    MediaCommand.PREVIOUS,
    MediaCommand.REPEAT_OFF,
    MediaCommand.REPEAT_ONE,
    MediaCommand.REPEAT_ALL,
    MediaCommand.SHUFFLE,
    MediaCommand.UNSHUFFLE,
]

if TYPE_CHECKING:
    from aiosendspin.server.client import SendspinClient
    from music_assistant_models.config_entries import ConfigValueType
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem

    from .provider import SendspinProvider


class MusicAssistantMediaStream(MediaStream):
    """MediaStream implementation for Music Assistant with per-player DSP support."""

    player_instance: SendspinPlayer
    internal_format: AudioFormat
    output_format: AudioFormat

    def __init__(
        self,
        *,
        main_channel_source: AsyncGenerator[bytes, None],
        main_channel_format: SendspinAudioFormat,
        player_instance: SendspinPlayer,
        internal_format: AudioFormat,
        output_format: AudioFormat,
    ) -> None:
        """
        Initialise the media stream with audio source and format for main_channel().

        Args:
            main_channel_source: Audio source generator for the main channel.
            main_channel_format: Audio format for the main channel (includes codec).
            player_instance: The SendspinPlayer instance for accessing mass and streams.
            internal_format: Internal processing format (float32 for headroom).
            output_format: Output PCM format (16-bit for player output).
        """
        super().__init__(
            main_channel_source=main_channel_source,
            main_channel_format=main_channel_format,
        )
        self.player_instance = player_instance
        self.internal_format = internal_format
        self.output_format = output_format

    async def player_channel(
        self,
        player_id: str,
        preferred_format: SendspinAudioFormat | None = None,
        position_us: int = 0,
    ) -> tuple[AsyncGenerator[bytes, None], SendspinAudioFormat, int] | None:
        """
        Get a player-specific audio stream with per-player DSP.

        Args:
            player_id: Identifier for the player requesting the stream.
            preferred_format: The player's preferred native format for the stream.
                The implementation may return a different format; the library
                will handle any necessary conversion.
            position_us: Position in microseconds relative to the main_stream start.
                Used for late-joining players to sync with the main stream.

        Returns:
            A tuple of (audio generator, audio format, actual position in microseconds)
            or None if unavailable. If None, the main_stream is used as fallback.
        """
        mass = self.player_instance.mass
        multi_client_stream = self.player_instance.timed_client_stream
        assert multi_client_stream is not None

        dsp = mass.config.get_player_dsp_config(player_id)
        output_channels = mass.config.get_raw_player_config_value(
            player_id, CONF_OUTPUT_CHANNELS, "stereo"
        )
        if not dsp.enabled and output_channels == "stereo":
            # DSP is disabled and output is stereo, use main_stream
            return None

        # Get per-player DSP filter parameters
        # Convert from internal format to output format
        filter_params = get_player_filter_params(
            mass, player_id, self.internal_format, self.output_format
        )

        # Get the stream with position (in seconds)
        stream_gen, actual_position = await multi_client_stream.get_stream(
            output_format=self.output_format,
            filter_params=filter_params,
        )

        # Convert position from seconds to microseconds for aiosendspin API
        actual_position_us = int(actual_position * 1_000_000)

        # Return actual position in microseconds relative to main_stream start
        self.player_instance.logger.debug(
            "Providing channel stream for player %s at position %d us",
            player_id,
            actual_position_us,
        )
        return (
            stream_gen,
            SendspinAudioFormat(
                sample_rate=self.output_format.sample_rate,
                bit_depth=self.output_format.bit_depth,
                channels=self.output_format.channels,
                codec=self._main_channel_format.codec,
            ),
            actual_position_us,
        )


class SendspinPlayer(Player):
    """A sendspin audio player in Music Assistant."""

    api: SendspinClient
    unsub_event_cb: Callable[[], None]
    unsub_group_event_cb: Callable[[], None]
    last_sent_artwork_url: str | None = None
    last_sent_artist_artwork_url: str | None = None
    _playback_task: asyncio.Task[None] | None = None
    timed_client_stream: TimedClientStream | None = None
    is_web_player: bool = False

    def __init__(self, provider: SendspinProvider, player_id: str) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)
        sendspin_client = provider.server_api.get_client(player_id)
        assert sendspin_client is not None
        self.api = sendspin_client
        self.api.disconnect_behaviour = DisconnectBehaviour.STOP
        self.unsub_event_cb = sendspin_client.add_event_listener(self.event_cb)
        self.unsub_group_event_cb = sendspin_client.group.add_event_listener(self.group_event_cb)
        sendspin_client.group.set_supported_commands(SUPPORTED_GROUP_COMMANDS)

        self.logger = self.provider.logger.getChild(player_id)
        # init some static variables
        self._attr_name = sendspin_client.name
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.MULTI_DEVICE_DSP,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
        }
        self._attr_can_group_with = {provider.instance_id}
        self._attr_power_control = PLAYER_CONTROL_NONE
        if device_info := sendspin_client.info.device_info:
            self._attr_device_info = DeviceInfo(
                model=device_info.product_name or "Unknown model",
                manufacturer=device_info.manufacturer or "Unknown Manufacturer",
                software_version=device_info.software_version,
            )
        else:
            self._attr_device_info = DeviceInfo()
        if player_client := sendspin_client.player:
            self._attr_volume_level = player_client.volume
            self._attr_volume_muted = player_client.muted
        self._attr_available = True
        self.is_web_player = sendspin_client.name.startswith(
            "Web ("  # The regular Web Interface
        ) or sendspin_client.name.startswith(
            "PWA ("  # The PWA App
        )
        self._attr_expose_to_ha_by_default = not self.is_web_player

    async def event_cb(self, client: SendspinClient, event: ClientEvent) -> None:
        """Event callback registered to the sendspin server."""
        self.logger.debug("Received PlayerEvent: %s", event)
        match event:
            case VolumeChangedEvent(volume=volume, muted=muted):
                self._attr_volume_level = volume
                self._attr_volume_muted = muted
                self.update_state()
            case ClientGroupChangedEvent(new_group=new_group):
                self.unsub_group_event_cb()
                self.unsub_group_event_cb = new_group.add_event_listener(self.group_event_cb)
                # Sync playback state from the new group
                match new_group.state:
                    case PlaybackStateType.PLAYING:
                        self._attr_playback_state = PlaybackState.PLAYING
                    case PlaybackStateType.PAUSED:
                        self._attr_playback_state = PlaybackState.PAUSED
                    case PlaybackStateType.STOPPED:
                        self._attr_playback_state = PlaybackState.IDLE
                # Update in case this is a newly created group
                new_group.set_supported_commands(SUPPORTED_GROUP_COMMANDS)
                # GroupMemberAddedEvent or GroupMemberRemovedEvent will be fired before this
                # so group members are already up to date at this point
                if self.synced_to is None:
                    # We are the leader, stop on disconnect
                    self.api.disconnect_behaviour = DisconnectBehaviour.STOP
                else:
                    self.api.disconnect_behaviour = DisconnectBehaviour.UNGROUP
                self.update_state()

    async def _handle_group_command(self, command: MediaCommand) -> None:
        """Handle a group command from aiosendspin."""
        queue = self.mass.player_queues.get_active_queue(self.player_id)
        match command:
            case MediaCommand.PLAY:
                await self.mass.players.cmd_play(self.player_id)
            case MediaCommand.PAUSE:
                await self.mass.players.cmd_pause(self.player_id)
            case MediaCommand.STOP:
                await self.mass.players.cmd_stop(self.player_id)
            case MediaCommand.NEXT:
                await self.mass.players.cmd_next_track(self.player_id)
            case MediaCommand.PREVIOUS:
                await self.mass.players.cmd_previous_track(self.player_id)
            case MediaCommand.REPEAT_OFF if queue:
                self.mass.player_queues.set_repeat(queue.queue_id, RepeatMode.OFF)
            case MediaCommand.REPEAT_ONE if queue:
                self.mass.player_queues.set_repeat(queue.queue_id, RepeatMode.ONE)
            case MediaCommand.REPEAT_ALL if queue:
                self.mass.player_queues.set_repeat(queue.queue_id, RepeatMode.ALL)
            case MediaCommand.SHUFFLE if queue:
                await self.mass.player_queues.set_shuffle(queue.queue_id, shuffle_enabled=True)
            case MediaCommand.UNSHUFFLE if queue:
                await self.mass.player_queues.set_shuffle(queue.queue_id, shuffle_enabled=False)

    async def group_event_cb(self, group: SendspinGroup, event: GroupEvent) -> None:
        """Event callback registered to the sendspin group this player belongs to."""
        if self.synced_to is not None:
            # Only handle group events as the leader, except for:
            # - GroupMemberRemovedEvent: to handle being removed from a group
            # - GroupStateChangedEvent: to update playback state when leader stops/disconnects
            if not isinstance(event, (GroupMemberRemovedEvent, GroupStateChangedEvent)):
                return
        self.logger.debug("Received GroupEvent: %s", event)

        match event:
            case GroupCommandEvent(command=command):
                self.logger.debug("Group command received: %s", command)
                await self._handle_group_command(command)
            case GroupStateChangedEvent(state=state):
                self.logger.debug("Group state changed to: %s", state)
                match state:
                    case PlaybackStateType.PLAYING:
                        self._attr_playback_state = PlaybackState.PLAYING
                    case PlaybackStateType.PAUSED:
                        self._attr_playback_state = PlaybackState.PAUSED
                    case PlaybackStateType.STOPPED:
                        self._attr_playback_state = PlaybackState.IDLE
                        self._attr_elapsed_time = 0
                        self._attr_elapsed_time_last_updated = time.time()
                self.update_state()
            case GroupMemberAddedEvent(client_id=client_id):
                self.logger.debug("Group member added: %s", client_id)
                if client_id not in self._attr_group_members:
                    self._attr_group_members.append(client_id)
                    self.update_state()
            case GroupMemberRemovedEvent(client_id=client_id):
                self.logger.debug("Group member removed: %s", client_id)
                if client_id == self.player_id:
                    if len(self._attr_group_members) > 0:
                        # We were just removed as a leader:
                        # 1. stop playback on the old group
                        await group.stop()
                        # 2. clear our members (since we are now alone)
                        group_members = [
                            member for member in self._attr_group_members if member != client_id
                        ]
                        self._attr_group_members = []
                        # 3. assign new leader if there are members left
                        if len(group_members) > 0 and (
                            new_leader := self.mass.players.get(group_members[0])
                        ):
                            new_leader = cast("SendspinPlayer", new_leader)
                            new_leader._attr_group_members = group_members[1:]
                            new_leader.api.disconnect_behaviour = DisconnectBehaviour.STOP
                            new_leader.update_state()
                    self.update_state()
                elif client_id in self._attr_group_members:
                    # Someone else left our group
                    self._attr_group_members.remove(client_id)
                    self.update_state()
            case GroupDeletedEvent():
                pass

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        if player_client := self.api.player:
            player_client.set_volume(volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        if player_client := self.api.player:
            if muted:
                player_client.mute()
            else:
                player_client.unmute()

    async def stop(self) -> None:
        """Stop command."""
        self.logger.debug("Received STOP command on player %s", self.display_name)
        # We don't care if we stopped the stream or it was already stopped
        await self.api.group.stop()
        # Clear the playback task reference (group.stop() handles stopping the stream)
        self._playback_task = None
        self._attr_current_media = None
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        self.logger.debug(
            "Received PLAY_MEDIA command on player %s with uri %s", self.display_name, media.uri
        )

        # Update player state optimistically
        self._attr_current_media = media
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        # playback_state will be set by the group state change event

        # Stop previous stream in case we were already playing something
        await self.api.group.stop()
        # Run playback in background task to immediately return
        self._playback_task = asyncio.create_task(self._run_playback(media))
        self.update_state()

    async def _run_playback(self, media: PlayerMedia) -> None:
        """Run the actual playback in a background task."""
        try:
            pcm_format = AudioFormat(
                content_type=ContentType.PCM_S16LE,
                sample_rate=48000,
                bit_depth=16,
                channels=2,
            )
            flow_pcm_format = AudioFormat(
                content_type=INTERNAL_PCM_FORMAT.content_type,
                sample_rate=pcm_format.sample_rate,
                bit_depth=INTERNAL_PCM_FORMAT.bit_depth,
                channels=pcm_format.channels,
            )

            output_codec = cast("str", self.config.get_value(CONF_OUTPUT_CODEC, "pcm"))

            # Convert string codec to AudioCodec enum
            audio_codec = AudioCodec(output_codec)

            # Get clean audio source in flow format (high quality internal format)
            # Format conversion and per-player DSP will be applied via player_channel
            audio_source = self.mass.streams.get_stream(media, flow_pcm_format)

            # Create TimedClientStream to wrap the clean audio source
            # This distributes the audio to multiple subscribers without DSP
            self.timed_client_stream = TimedClientStream(
                audio_source=audio_source,
                audio_format=flow_pcm_format,
            )

            # Setup the main channel subscription
            # aiosendspin only really supports 16-bit for now TODO: upgrade later to 32-bit
            main_channel_gen, main_position = await self.timed_client_stream.get_stream(
                output_format=pcm_format,
                filter_params=None,  # TODO: this should probably still include the safety limiter
            )
            assert main_position == 0.0  # first subscriber, should be zero
            media_stream = MusicAssistantMediaStream(
                main_channel_source=main_channel_gen,
                main_channel_format=SendspinAudioFormat(
                    sample_rate=pcm_format.sample_rate,
                    bit_depth=pcm_format.bit_depth,
                    channels=pcm_format.channels,
                    codec=audio_codec,
                ),
                player_instance=self,
                internal_format=flow_pcm_format,
                output_format=pcm_format,
            )

            stop_time = await self.api.group.play_media(media_stream)
            await self.api.group.stop(stop_time)
        except asyncio.CancelledError:
            self.logger.debug("Playback cancelled for player %s", self.display_name)
            raise
        except Exception:
            self.logger.exception("Error during playback for player %s", self.display_name)
            raise
        finally:
            self.timed_client_stream = None

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        self.logger.debug(
            "set_members called: adding %s, removing %s", player_ids_to_add, player_ids_to_remove
        )
        for player_id in player_ids_to_remove or []:
            player = self.mass.players.get(player_id, True)
            player = cast("SendspinPlayer", player)  # For type checking
            await self.api.group.remove_client(player.api)
        for player_id in player_ids_to_add or []:
            player = self.mass.players.get(player_id, True)
            player = cast("SendspinPlayer", player)  # For type checking
            await self.api.group.add_client(player.api)
        # self.group_members will be updated by the group event callback

    async def _send_album_artwork(self, current_item: QueueItem) -> str | None:
        """
        Send album artwork to the sendspin group.

        Args:
            current_item: The current queue item.
        """
        artwork_url = None
        if current_item.image is not None:
            artwork_url = self.mass.metadata.get_image_url(current_item.image)

        if artwork_url != self.last_sent_artwork_url:
            # Image changed, resend the artwork
            self.last_sent_artwork_url = artwork_url
            if artwork_url is not None and current_item.media_item is not None:
                image_data = await self.mass.metadata.get_image_data_for_item(
                    current_item.media_item
                )
                if image_data is not None:
                    image = await asyncio.to_thread(Image.open, BytesIO(image_data))
                    await self.api.group.set_media_art(image, source=ArtworkSource.ALBUM)
            else:
                # Clear artwork if none available
                await self.api.group.set_media_art(None, source=ArtworkSource.ALBUM)

        return artwork_url

    async def _send_artist_artwork(self, current_item: QueueItem) -> None:
        """
        Send artist artwork to the sendspin group.

        Args:
            current_item: The current queue item.
        """
        # Extract primary artist if available
        artist_artwork_url = None
        if current_item.media_item is not None and hasattr(current_item.media_item, "artists"):
            artists = getattr(current_item.media_item, "artists", None)
            if artists and len(artists) > 0:
                primary_artist = artists[0]
                if hasattr(primary_artist, "image"):
                    artist_image = getattr(primary_artist, "image", None)
                    if artist_image is not None:
                        artist_artwork_url = self.mass.metadata.get_image_url(artist_image)

        if artist_artwork_url != self.last_sent_artist_artwork_url:
            # Artist image changed, resend the artwork
            self.last_sent_artist_artwork_url = artist_artwork_url
            if artist_artwork_url is not None:
                artist_image_data = await self.mass.metadata.get_image_data_for_item(
                    primary_artist, img_type=ImageType.THUMB
                )
                if artist_image_data is not None:
                    artist_image = await asyncio.to_thread(Image.open, BytesIO(artist_image_data))
                    await self.api.group.set_media_art(artist_image, source=ArtworkSource.ARTIST)
            else:
                # Clear artist artwork if none available
                await self.api.group.set_media_art(None, source=ArtworkSource.ARTIST)

    def _on_player_media_updated(self) -> None:
        """Handle callback when the current media of the player is updated."""
        if self.synced_to is not None:
            # Only leader sends metadata
            return

        if self.current_media is None:
            # Clear metadata when no media loaded
            self.api.group.set_metadata(Metadata())
            return
        self.mass.create_task(self.send_current_media_metadata())

    async def send_current_media_metadata(self) -> None:
        """Send the current media metadata to the sendspin group."""
        if not self.available:
            return
        current_media = self.current_media
        if current_media is None:
            return
        # check if we are playing a MA queue item
        queue_item: QueueItem | None = None
        queue: PlayerQueue | None = None
        if current_media.source_id and current_media.queue_item_id:
            queue = self.mass.player_queues.get(current_media.source_id)
            queue_item = self.mass.player_queues.get_item(
                current_media.source_id, current_media.queue_item_id
            )

        # Send album and artist artwork
        if queue_item:
            await self._send_album_artwork(queue_item)
            await self._send_artist_artwork(queue_item)

        track_duration = current_media.duration or 0
        repeat = SendspinRepeatMode.OFF
        if queue and queue.repeat_mode == RepeatMode.ALL:
            repeat = SendspinRepeatMode.ALL
        elif queue and queue.repeat_mode == RepeatMode.ONE:
            repeat = SendspinRepeatMode.ONE

        shuffle = queue.shuffle_enabled if queue else False

        metadata = Metadata(
            title=current_media.title,
            artist=current_media.artist,
            album_artist=None,  # TODO: extract from optional queue item
            album=current_media.album,
            artwork_url=current_media.image_url,
            year=None,  # TODO: extract from optional queue item
            track=None,  # TODO: extract from optional queue item
            track_duration=track_duration * 1000 if track_duration is not None else None,
            track_progress=int(current_media.corrected_elapsed_time * 1000)
            if current_media.corrected_elapsed_time
            else 0,
            playback_speed=1000,
            repeat=repeat,
            shuffle=shuffle,
        )

        # Send metadata to the group
        self.api.group.set_metadata(metadata)

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        default_entries = await super().get_config_entries(action=action, values=values)
        return [
            *default_entries,
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
            CONF_ENTRY_HTTP_PROFILE_HIDDEN,
            ConfigEntry.from_dict({**CONF_ENTRY_SAMPLE_RATES.to_dict(), "hidden": True}),
        ]

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        self.unsub_event_cb()
        self.unsub_group_event_cb()
