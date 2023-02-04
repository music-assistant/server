"""Model/base for a Metadata Provider implementation."""
from __future__ import annotations

from abc import abstractmethod
from music_assistant.common.models.enums import PlayerState

from music_assistant.common.models.player import Player
from music_assistant.common.models.queue_item import QueueItem

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
        Send PLAY (unpause) command to given player.
            - player_id: player_id of the player to handle the command.
        """

    @abstractmethod
    async def cmd_play_media(self, player_id: str, media: QueueItem) -> None:
        """
        Send PLAY MEDIA command to given player.

        This is called when the Queue wants the player to start playing a specific QueueItem.
        The player implementation can decide how to process the request, such as playing
        queue items one-by-one or enqueue all/some items.

            - player_id: player_id of the player to handle the command.
            - media: the QueueItem to start playing on the player.
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

    async def cmd_seek(self, player_id: str, position: int) -> None:
        """
        Handle SEEK command for given queue.

            - player_id: player_id of the player to handle the command.
            - position: position in seconds to seek to in the current playing item.
        """
        # will only be called for players with Seek feature set.
        raise NotImplementedError()

    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """
        Handle SYNC command for given player.

        Join/add the given player(id) to the given (master) player/sync group.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup master or group player.
        """
        # will only be called for players with SYNC feature set.
        raise NotImplementedError()

    async def cmd_unsync(self, player_id: str) -> None:
        """
        Handle UNSYNC command for given player.

        Remove the given player from any syncgroups it currently is synced to.

            - player_id: player_id of the player to handle the command.
        """
        # will only be called for players with SYNC feature set.
        raise NotImplementedError()

    async def cmd_set_members(self, player_id: str, members: list[str]) -> None:
        """
        Handle SET_MEMBERS command for given playergroup.

        Update the memberlist of the given PlayerGroup.

            - player_id: player_id of the groupplayer to handle the command.
            - members: list of player ids to set as members.
        """
        # will only be called for players of type GROUP with SET_MEMBERS feature set.
        raise NotImplementedError()

    