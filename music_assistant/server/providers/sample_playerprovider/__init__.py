"""Sample Player provider for Music Assistant"""
from __future__ import annotations

from music_assistant.common.models.enums import (
    ContentType,
    PlayerFeature,
    PlayerState,
    PlayerType,
)
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.server.models.player_provider import PlayerProvider

DUMMY_PLAYER_ID = "dummy_player"
PLAYER_CONFIG_ENTRIES = tuple()
PLAYER_SUPPORTED_FEATURES = (PlayerFeature.POWER, PlayerFeature.VOLUME_SET)


class SamplePlayerProvider(PlayerProvider):
    """Sample Player provider."""

    async def setup(self) -> None:
        """Handle async initialization of the provider."""
        # add dummy player to MA Player manager
        dummy_player = Player(
            player_id=DUMMY_PLAYER_ID,
            provider=self.domain,
            type=PlayerType.PLAYER,
            name="Dummy Player",
            available=True,
            powered=False,
            device_info=DeviceInfo(
                model="Dummy Model",
                address="1.2.3.4",
                manufacturer="Dummy Manufacturer",
            ),
            supported_features=PLAYER_SUPPORTED_FEATURES,
        )
        self.mass.players.register(dummy_player)

    async def close(self) -> None:
        """Handle close/cleanup of the provider."""

    async def cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.
            - player_id: player_id of the player to handle the command.
        """
        self.logger.debug("Stop called for %s", player_id)
        # Normally you would send a call to an actual player here
        player = self.mass.players.get(player_id)
        player.state = PlayerState.IDLE
        self.mass.players.update(player_id)

    async def cmd_play(self, player_id: str) -> None:
        """
        Send PLAY command to given player.
            - player_id: player_id of the player to handle the command.
        """
        # Normally you would send a call to an actual player here
        player = self.mass.players.get(player_id)
        player.state = PlayerState.PLAYING
        self.mass.players.update(player_id)

    async def cmd_play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> None:
        """Send PLAY MEDIA command to given player."""
        url = await self.mass.streams.resolve_stream_url(
            queue_item=queue_item,
            player_id=player_id,
            # just let the stream engine handle seek and fade here
            seek_position=seek_position,
            fade_in=fade_in,
            # request content_type that is supported by your player here
            content_type=ContentType.FLAC,
        )
        # Normally you would send a call to an actual player here
        player = self.mass.players.get(player_id)
        player.state = PlayerState.PLAYING
        player.current_item_id = queue_item.queue_item_id
        player.current_url = url
        self.mass.players.update(player_id)

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        # Normally you would send a call to an actual player here
        player = self.mass.players.get(player_id)
        player.state = PlayerState.PAUSED
        self.mass.players.update(player_id)

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player."""
        # Normally you would send a call to an actual player here
        player = self.mass.players.get(player_id)
        player.powered = powered
        self.mass.players.update(player_id)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        # Normally you would send a call to an actual player here
        player = self.mass.players.get(player_id)
        player.volume_level = volume_level
        self.mass.players.update(player_id)

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        # Normally you would send a call to an actual player here
        player = self.mass.players.get(player_id)
        player.volume_muted = muted
        self.mass.players.update(player_id)
