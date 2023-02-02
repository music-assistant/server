"""Model/base for a Metadata Provider implementation."""
from __future__ import annotations

from abc import abstractmethod

from music_assistant.common.models.player import Player

from .provider import Provider


class PlayerProvider(Provider):
    """
    Base representation of a Player Provider (controller).

    Player Provider implementations should inherit from this base model.
    """

    @property
    def players(self) -> list[Player]:
        """Return all players belonging to this provider."""
        # pylint: disable=no-member
        return [
            player for player in self.mass.players if player.provider == self.domain
        ]

    @abstractmethod
    async def cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.
            - player_id: player_id of the player to handle the command.
        """

    @abstractmethod
    async def cmd_play(self, player_id: str) -> None:
        """
        Send PLAY command to given player.
            - player_id: player_id of the player to handle the command.
        """

    @abstractmethod
    async def cmd_play_url(self, player_id: str, url: str) -> None:
        """
        Send PLAY MEDIA command to given player.
            - player_id: player_id of the player to handle the command.
            - url: the url to start playing on the player.
        """

    @abstractmethod
    async def cmd_pause(self, player_id: str) -> None:
        """
        Send PAUSE command to given player.
            - player_id: player_id of the player to handle the command.
        """

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """
        Send POWER command to given player.
            - player_id: player_id of the player to handle the command.
            - powered: bool if player should be powered on or off.
        """
        # will only be called for players with Power feature set.
        raise NotImplementedError()

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """
        Send VOLUME_SET command to given player.
            - player_id: player_id of the player to handle the command.
            - volume_level: volume level (0..100) to set on the player.
        """
        # will only be called for players with Volume feature set.
        raise NotImplementedError()

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """
        Send VOLUME MUTE command to given player.
            - player_id: player_id of the player to handle the command.
            - muted: bool if player should be muted.
        """
        # will only be called for players with Mute feature set.
        raise NotImplementedError()
