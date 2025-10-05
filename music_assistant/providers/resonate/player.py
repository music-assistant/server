"""Resonate Player implementation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from io import BytesIO
from typing import TYPE_CHECKING, cast

from aioresonate.models import MediaCommand
from aioresonate.models.types import PlaybackStateType
from aioresonate.models.types import RepeatMode as ResonateRepeatMode
from aioresonate.server import (
    AudioFormat as ResonateAudioFormat,
)
from aioresonate.server import (
    ClientEvent,
    GroupCommandEvent,
    GroupEvent,
    GroupStateChangedEvent,
    VolumeChangedEvent,
)
from aioresonate.server.client import (
    ClientGroupChangedEvent,
    DisconnectBehaviour,
)
from aioresonate.server.group import (
    AudioCodec,
    GroupDeletedEvent,
    GroupMemberAddedEvent,
    GroupMemberRemovedEvent,
    Metadata,
)
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    ContentType,
    EventType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    RepeatMode,
)
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import DeviceInfo
from PIL import Image

from music_assistant.constants import CONF_ENTRY_OUTPUT_CODEC, CONF_OUTPUT_CODEC
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.models.player import Player, PlayerMedia
from music_assistant.providers.universal_group.constants import UGP_PREFIX
from music_assistant.providers.universal_group.player import UniversalGroupPlayer

if TYPE_CHECKING:
    from aioresonate.server.client import Client
    from music_assistant_models.event import MassEvent

    from .provider import ResonateProvider


class ResonatePlayer(Player):
    """A resonate audio player in Music Assistant."""

    api: Client
    unsub_event_cb: Callable[[], None]
    unsub_group_event_cb: Callable[[], None]
    last_sent_artwork_url: str | None = None

    def __init__(self, provider: ResonateProvider, player_id: str) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)
        resonate_client = provider.server_api.get_client(player_id)
        assert resonate_client is not None
        self.api = resonate_client
        self.api.disconnect_behaviour = DisconnectBehaviour.STOP
        self.unsub_event_cb = resonate_client.add_event_listener(self.event_cb)
        self.unsub_group_event_cb = resonate_client.group.add_event_listener(self.group_event_cb)

        self.logger = self.provider.logger.getChild(player_id)
        # init some static variables
        self._attr_name = resonate_client.name
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.SET_MEMBERS,
        }
        self._attr_can_group_with = {provider.lookup_key}
        self._attr_power_control = PLAYER_CONTROL_NONE
        self._attr_device_info = DeviceInfo()
        self._attr_volume_level = resonate_client.volume
        self._attr_volume_muted = resonate_client.muted
        self._attr_available = True
        self._on_unload_callbacks.append(
            self.mass.subscribe(
                self._on_queue_update,
                (EventType.QUEUE_UPDATED),
            )
        )

    async def event_cb(self, event: ClientEvent) -> None:
        """Event callback registered to the resonate server."""
        self.logger.debug("Received PlayerEvent: %s", event)
        match event:
            case VolumeChangedEvent(volume=volume, muted=muted):
                self._attr_volume_level = volume
                self._attr_volume_muted = muted
                self.update_state()
            case ClientGroupChangedEvent(new_group=new_group):
                self.unsub_group_event_cb()
                self.unsub_group_event_cb = new_group.add_event_listener(self.group_event_cb)

    async def group_event_cb(self, event: GroupEvent) -> None:
        """Event callback registered to the resonate group this player belongs to."""
        if self.synced_to is not None:
            # Only handle group events as the leader
            return
        self.logger.debug("Received GroupEvent: %s", event)

        match event:
            case GroupCommandEvent(command=command, volume=volume, mute=mute):
                self.logger.debug("Group command received: %s", command)
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
                    case MediaCommand.SEEK:
                        raise NotImplementedError("TODO: not supported by spec yet")
                    case MediaCommand.VOLUME:
                        assert volume is not None
                        await self.mass.players.cmd_group_volume(self.player_id, volume)
                    case MediaCommand.MUTE:
                        assert mute is not None
                        for member in self.mass.players.iter_group_members(
                            self, active_only=True, exclude_self=True
                        ):
                            await member.volume_mute(mute)
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
            case GroupMemberAddedEvent(client_id=_):
                pass
            case GroupMemberRemovedEvent(client_id=_):
                pass
            case GroupDeletedEvent():
                pass

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        self.api.set_volume(volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        if muted:
            self.api.mute()
        else:
            self.api.unmute()

    async def stop(self) -> None:
        """Stop command."""
        self.logger.debug("Received STOP command on player %s", self.display_name)
        # We don't care if we stopped the stream or it was already stopped
        self.api.group.stop()
        self._attr_active_source = None
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
        self._attr_active_source = media.source_id
        # playback_state will be set by the group state change event

        pcm_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            sample_rate=48000,
            bit_depth=16,
            channels=2,
        )

        # select audio source
        if media.media_type == MediaType.PLUGIN_SOURCE:
            # special case: plugin source stream
            assert media.custom_data is not None  # for type checking
            audio_source = self.mass.streams.get_plugin_source_stream(
                plugin_source_id=media.custom_data["provider"],
                output_format=pcm_format,
                player_id=self.player_id,
            )
        elif media.source_id and media.source_id.startswith(UGP_PREFIX):
            # special case: UGP stream
            ugp_player = cast("UniversalGroupPlayer", self.mass.players.get(media.source_id))
            ugp_stream = ugp_player.stream
            assert ugp_stream is not None  # for type checker
            pcm_format.bit_depth = ugp_stream.base_pcm_format.bit_depth
            pcm_format.bit_rate = ugp_stream.base_pcm_format.bit_rate
            pcm_format.channels = ugp_stream.base_pcm_format.channels
            audio_source = ugp_stream.subscribe_raw()
        elif media.source_id and media.queue_item_id:
            # regular queue (flow) stream request
            queue = self.mass.player_queues.get(media.source_id)
            start_queue_item = self.mass.player_queues.get_item(
                media.source_id, media.queue_item_id
            )
            assert queue is not None  # for type checking
            assert start_queue_item is not None  # for type checking
            audio_source = self.mass.streams.get_queue_flow_stream(
                queue=queue, start_queue_item=start_queue_item, pcm_format=pcm_format
            )
        else:
            # assume url or some other direct path
            audio_source = get_ffmpeg_stream(
                audio_input=media.uri,
                input_format=AudioFormat(content_type=ContentType.try_parse(media.uri)),
                output_format=pcm_format,
            )

        output_codec = cast("str", self.config.get_value(CONF_OUTPUT_CODEC, "pcm"))

        # Convert string codec to AudioCodec enum
        audio_codec = AudioCodec(output_codec)

        await self.api.group.play_media(
            audio_source,
            ResonateAudioFormat(pcm_format.sample_rate, pcm_format.bit_depth, pcm_format.channels),
            preferred_stream_codec=audio_codec,
        )
        self.update_state()

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
            player = cast("ResonatePlayer", player)  # For type checking
            self.api.group.remove_client(player.api)
            player.api.disconnect_behaviour = DisconnectBehaviour.STOP
            self._attr_group_members.remove(player_id)
        for player_id in player_ids_to_add or []:
            player = self.mass.players.get(player_id, True)
            player = cast("ResonatePlayer", player)  # For type checking
            player.api.disconnect_behaviour = DisconnectBehaviour.UNGROUP
            self.api.group.add_client(player.api)
            self._attr_group_members.append(player_id)
        self.update_state()

    def _update_media_art(self, image_data: bytes) -> None:
        image = Image.open(BytesIO(image_data))
        self.api.group.set_media_art(image)

    async def _on_queue_update(self, event: MassEvent) -> None:
        """Extract and send current media metadata to resonate players on queue updates."""
        queue = self.mass.player_queues.get_active_queue(self.player_id)
        if not queue or not queue.current_item:
            return

        current_item = queue.current_item

        title = current_item.name
        artist = None
        album_artist = None
        album = None
        track = None
        artwork_url = None
        year = None

        if (streamdetails := current_item.streamdetails) and streamdetails.stream_title:
            # stream title/metadata from radio/live stream
            if " - " in streamdetails.stream_title:
                artist, title = streamdetails.stream_title.split(" - ", 1)
            else:
                title = streamdetails.stream_title
                artist = ""
            # set album to radio station name
            album = current_item.name
        elif media_item := current_item.media_item:
            title = media_item.name
            if artist_str := getattr(media_item, "artist_str", None):
                artist = artist_str
            if _album := getattr(media_item, "album", None):
                album = _album.name
                year = _album.year
                album_artist = getattr(_album, "artist_str", None)
            if _track_number := getattr(media_item, "track_number", None):
                track = _track_number

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
                    await asyncio.to_thread(self._update_media_art, image_data)
            # TODO: null media art if not set?

        track_duration = current_item.duration

        repeat = ResonateRepeatMode.OFF
        if queue.repeat_mode == RepeatMode.ALL:
            repeat = ResonateRepeatMode.ALL
        elif queue.repeat_mode == RepeatMode.ONE:
            repeat = ResonateRepeatMode.ONE

        shuffle = queue.shuffle_enabled

        metadata = Metadata(
            title=title,
            artist=artist,
            album_artist=album_artist,
            album=album,
            artwork_url=artwork_url,
            year=year,
            track=track,
            track_duration=track_duration,
            playback_speed=1,
            repeat=repeat,
            shuffle=shuffle,
        )

        # Send metadata to the group
        self.api.group.set_metadata(metadata)

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        default_entries = await super().get_config_entries()
        return [
            *default_entries,
            ConfigEntry.from_dict(
                {
                    **CONF_ENTRY_OUTPUT_CODEC.to_dict(),
                    "default_value": "pcm",
                    "options": [
                        {"title": "PCM (lossless, uncompressed)", "value": "pcm"},
                        {"title": "FLAC (lossless, compressed)", "value": "flac"},
                        {"title": "OPUS (lossy)", "value": "opus"},
                    ],
                }
            ),
        ]

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        self.unsub_event_cb()
        self.unsub_group_event_cb()
        await self.api.disconnect()
