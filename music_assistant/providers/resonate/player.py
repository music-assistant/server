"""Resonate Player implementation."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from aioresonate.models.types import RepeatMode as ResonateRepeatMode
from aioresonate.server import (
    AudioFormat as ResonateAudioFormat,
)
from aioresonate.server import (
    PlayerEvent,
    VolumeChangedEvent,
)
from aioresonate.server.group import Metadata
from aioresonate.server.player import (
    DisconnectBehaviour,
    StreamPauseEvent,
    StreamStartEvent,
    StreamStopEvent,
)
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    ContentType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    RepeatMode,
)
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import DeviceInfo

from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from aioresonate.server import Player as PlayerAPI

    from .provider import ResonateProvider


class ResonatePlayer(Player):
    """A resonate audio player in Music Assistant."""

    api: PlayerAPI
    unsub_event_cb: Callable[[], None]

    def __init__(self, provider: ResonateProvider, player_id: str) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)
        resonate_player = provider.server_api.get_player(player_id)
        assert resonate_player is not None
        self.api = resonate_player
        self.api.disconnect_behaviour = DisconnectBehaviour.STOP
        self.unsub_event_cb = resonate_player.add_event_listener(self.event_cb)

        self.logger = self.provider.logger.getChild(player_id)
        # init some static variables
        self._attr_name = resonate_player.name
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
        }
        self._attr_can_group_with = {provider.lookup_key}
        self._attr_power_control = PLAYER_CONTROL_NONE
        self._attr_device_info = DeviceInfo()
        self._attr_volume_level = resonate_player.volume
        self._attr_volume_muted = resonate_player.muted
        self._attr_available = True
        self._attr_poll_interval = 5  # For metadata updates
        self._attr_needs_poll = False

    async def event_cb(self, event: PlayerEvent) -> None:
        """Event callback registered to the resonate server."""
        self.logger.debug("Received PlayerEvent: %s", event)
        match event:
            case VolumeChangedEvent(volume=volume, muted=muted):
                self._attr_volume_level = volume
                self._attr_volume_muted = muted
                self.update_state()
            case StreamStartEvent():
                synced_to = self.synced_to
                await self.mass.player_queues.play(
                    synced_to if synced_to is not None else self.player_id
                )
            case StreamStopEvent():
                synced_to = self.synced_to
                await self.mass.player_queues.stop(
                    synced_to if synced_to is not None else self.player_id
                )
            case StreamPauseEvent():
                synced_to = self.synced_to
                await self.mass.player_queues.pause(
                    synced_to if synced_to is not None else self.player_id
                )
            case _:
                self.logger.error("Unknown resonate player event: %s", event)

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        self.api.set_volume(min(max(1, volume_level), 100))

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        if muted:
            self.api.mute()
        else:
            self.api.unmute()
        self.update_state()

    async def stop(self) -> None:
        """Stop command."""
        self.logger.info("Received STOP command on player %s", self.display_name)
        self._attr_playback_state = PlaybackState.IDLE
        _ = self.api.group.stop()
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        self.logger.info(
            "Received PLAY_MEDIA command on player %s with uri %s", self.display_name, media.uri
        )
        self._attr_current_media = media
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_active_source = media.queue_id
        self._attr_needs_poll = True  # Watch for metadata changes

        # TODO: Don't just use 48kHz since it may not be optimal depending of the grouped players
        pcm_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            sample_rate=48000,
            bit_depth=16,
            channels=2,
        )

        queue = self.mass.player_queues.get(self.player_id)
        assert queue
        assert media.queue_id
        queue_item = self.mass.player_queues.get_item(media.queue_id, media.queue_item_id)
        assert queue_item

        await self.api.group.play_media(
            self.mass.streams.get_queue_flow_stream(
                queue=queue, start_queue_item=queue_item, pcm_format=pcm_format
            ),
            ResonateAudioFormat(pcm_format.sample_rate, pcm_format.bit_depth, pcm_format.channels),
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
            self.api.group.remove_player(player.api)
            player.api.disconnect_behaviour = DisconnectBehaviour.STOP
            self._attr_group_members.remove(player_id)
        for player_id in player_ids_to_add or []:
            player = self.mass.players.get(player_id, True)
            player = cast("ResonatePlayer", player)  # For type checking
            player.api.disconnect_behaviour = DisconnectBehaviour.UNGROUP
            self.api.group.add_player(player.api)
            self._attr_group_members.append(player_id)
        self.update_state()

    async def poll(self) -> None:
        """Poll player for metadata updates and send to resonate players."""
        # TODO: finish this once spec is finalized
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

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        self.unsub_event_cb()
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
