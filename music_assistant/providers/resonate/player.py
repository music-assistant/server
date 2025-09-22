"""Resonate Player implementation."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from aioresonate.models.types import PlaybackStateType
from aioresonate.models.types import RepeatMode as ResonateRepeatMode
from aioresonate.server import (
    AudioFormat as ResonateAudioFormat,
)
from aioresonate.server import (
    ClientEvent,
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
    GroupMemberAddedEvent,
    GroupMemberRemovedEvent,
    Metadata,
)
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    RepeatMode,
)
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import DeviceInfo

from music_assistant.constants import CONF_ENTRY_OUTPUT_CODEC, CONF_OUTPUT_CODEC
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.models.player import Player, PlayerMedia
from music_assistant.providers.universal_group.constants import UGP_PREFIX
from music_assistant.providers.universal_group.player import UniversalGroupPlayer

if TYPE_CHECKING:
    from aioresonate.server.client import Client

    from .provider import ResonateProvider


class ResonatePlayer(Player):
    """A resonate audio player in Music Assistant."""

    api: Client
    unsub_event_cb: Callable[[], None]
    unsub_group_event_cb: Callable[[], None]

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
        self._attr_poll_interval = 5  # For metadata updates
        self._attr_needs_poll = False

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
            case GroupStateChangedEvent(state=state):
                self.logger.info("Group state changed to: %s", state)
                match state:
                    case PlaybackStateType.PLAYING:
                        self._attr_playback_state = PlaybackState.PLAYING
                        self._attr_needs_poll = True
                    case PlaybackStateType.PAUSED:
                        self._attr_playback_state = PlaybackState.PAUSED
                        self._attr_needs_poll = False
                    case PlaybackStateType.STOPPED:
                        self._attr_playback_state = PlaybackState.IDLE
                        self._attr_needs_poll = False
                        self._attr_elapsed_time = 0
                        self._attr_elapsed_time_last_updated = time.time()
                self.update_state()
            case GroupMemberAddedEvent(client_id=_):
                pass
            case GroupMemberRemovedEvent(client_id=_):
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
        self.logger.info("Received STOP command on player %s", self.display_name)
        # We don't care if we stopped the stream or it was already stopped
        _ = self.api.group.stop()

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        self.logger.info(
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
        codec_mapping = {
            "pcm": AudioCodec.PCM,
            "flac": AudioCodec.FLAC,
            "opus": AudioCodec.OPUS,
        }
        audio_codec = codec_mapping.get(output_codec, AudioCodec.PCM)

        await self.api.group.play_media(
            audio_source,
            ResonateAudioFormat(pcm_format.sample_rate, pcm_format.bit_depth, pcm_format.channels),
            preferred_stream_codec=audio_codec,
        )
        self.update_state()
        await self.update_metadata()

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

    async def poll(self) -> None:
        """Poll player for metadata updates and send to resonate players."""
        if self.playback_state != PlaybackState.PLAYING:
            self._attr_needs_poll = False
            return
        await self.update_metadata()

    async def update_metadata(self) -> None:
        """Extract and send current media metadata to resonate players."""
        # Get current media from active queue
        queue = self.mass.player_queues.get_active_queue(self.player_id)
        if not queue or not queue.current_item:
            return

        current_item = queue.current_item

        # Extract metadata following the same pattern as RAOP implementation
        title = current_item.name
        artist = None
        album = None
        track = None
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
            if _track_number := getattr(media_item, "track_number", None):
                track = _track_number

        repeat = ResonateRepeatMode.OFF
        if queue.repeat_mode == RepeatMode.ALL:
            repeat = ResonateRepeatMode.ALL
        elif queue.repeat_mode == RepeatMode.ONE:
            repeat = ResonateRepeatMode.ONE

        shuffle = queue.shuffle_enabled

        metadata = Metadata(
            title=title or "",
            artist=artist or "",
            album=album or "",
            year=year or 0,
            track=track or 0,
            repeat=repeat,
            shuffle=shuffle,
        )

        # Send metadata to the group
        self.api.group.set_metadata(metadata)

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        # OPTIONAL
        # this method is optional and should be implemented if you need player specific
        # configuration entries. If you do not need player specific configuration entries,
        # you can leave this method out completely to accept the default implementation.
        # Please note that you need to call the super() method to get the default entries.
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
        # Clear group members
        synced_to_id = self.synced_to
        if synced_to_id and (synced_to := self.mass.players.get(synced_to_id)):
            synced_to = cast("ResonatePlayer", synced_to)  # For type checking
            synced_to._attr_group_members.remove(self.player_id)
            synced_to.update_state()
        if len(self._attr_group_members) > 0:
            self._attr_group_members.clear()
            self.update_state()
