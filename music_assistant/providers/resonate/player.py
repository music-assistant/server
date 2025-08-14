"""Resonate Player implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast, override

from aioresonate.group import AudioFormat as ResonateAudioFormat
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import ContentType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import DeviceInfo

from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from aioresonate.instance import PlayerInstance

    from .provider import ResonateProvider


class ResonatePlayer(Player):
    """A resonate audio player in Music Assistant."""

    player: PlayerInstance

    def __init__(self, provider: ResonateProvider, player_id: str) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)
        player = provider.server.get_player(player_id)
        assert player is not None
        self.player = player

        self.logger = self.provider.logger.getChild(player_id)
        # init some static variables
        self._attr_name = player.name
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {PlayerFeature.SET_MEMBERS}
        self._attr_can_group_with = {provider.lookup_key}
        self._attr_power_control = PLAYER_CONTROL_NONE
        self._attr_device_info = DeviceInfo()
        self._set_attributes()

    @override
    async def stop(self) -> None:
        """Stop command."""
        self.logger.info("Received STOP command on player %s", self.display_name)
        self._attr_playback_state = PlaybackState.IDLE
        self.player.group.stop()
        self.update_state()

    @override
    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        self.logger.info(
            "Received PLAY_MEDIA command on player %s with uri %s", self.display_name, media.uri
        )
        self._attr_current_media = media
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_active_source = media.queue_id

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

        await self.player.group.play_media(
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
        if player_ids_to_remove is not None:
            for player_id in player_ids_to_remove:
                player = self.mass.players.get(player_id, True)
                player = cast("ResonatePlayer", player)  # For type checking
                self.player.group.remove_player(player.player)
        if player_ids_to_add is not None:
            for player_id in player_ids_to_add:
                player = self.mass.players.get(player_id, True)
                player = cast("ResonatePlayer", player)  # For type checking
                self.player.group.add_player(player.player)

    @override
    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        # OPTIONAL
        # this method is optional and should be implemented if you need to handle
        # any logic when the player is unloaded from the Player controller.
        # This is called when the player is removed from the Player controller.
        self.logger.info("Player %s unloaded", self.name)

    def _set_attributes(self) -> None:
        """Update/set (dynamic) properties."""
        self._attr_powered = True
        self._attr_volume_muted = False
        self._attr_volume_level = 50
