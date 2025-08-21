"""Resonate Player implementation."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, cast, override

from aioresonate.server import AudioFormat as ResonateAudioFormat
from aioresonate.server import PlayerEvent, VolumeChangedEvent
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import ContentType, PlaybackState, PlayerFeature, PlayerType
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
        self._attr_needs_poll = False

    async def event_cb(self, event: PlayerEvent) -> None:
        """Event callback registered to the resonate server."""
        self.logger.debug("Received PlayerEvent: %s", event)
        match event:
            case VolumeChangedEvent(volume=volume, muted=muted):
                self._attr_volume_level = volume
                self._attr_volume_muted = muted
                self.update_state()
            case _:
                self.logger.error("Unknown resonate player event: %s", event)

    @override
    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        # TODO: what if volume_level is 0?
        self.api.set_volume(volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        if muted:
            self.api.mute()
        else:
            self.api.unmute()
        self.update_state()

    @override
    async def stop(self) -> None:
        """Stop command."""
        self.logger.info("Received STOP command on player %s", self.display_name)
        self._attr_playback_state = PlaybackState.IDLE
        self.api.group.stop()
        self.update_state()

    @override
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

    @override
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
            self._attr_group_members.remove(player_id)
        for player_id in player_ids_to_add or []:
            player = self.mass.players.get(player_id, True)
            player = cast("ResonatePlayer", player)  # For type checking
            self.api.group.add_player(player.api)
            self._attr_group_members.append(player_id)
        self.update_state()

    @override
    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        # OPTIONAL
        # this method is optional and should be implemented if you need to handle
        # any logic when the player is unloaded from the Player controller.
        # This is called when the player is removed from the Player controller.
        self.logger.info("Player %s unloaded", self.name)
        self.unsub_event_cb()
