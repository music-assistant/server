"""Model/base for a Metadata Provider implementation."""
from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING

from music_assistant.common.models.enums import ContentType
from music_assistant.common.models.player import Player
from music_assistant.common.models.queue_item import QueueItem

from .provider import Provider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ConfigEntry


class PlayerProvider(Provider):
    """
    Base representation of a Player Provider (controller).

    Player Provider implementations should inherit from this base model.
    """

    def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        return tuple()

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
    async def cmd_pause(self, player_id: str) -> None:
        """
        Send PAUSE command to given player.
            - player_id: player_id of the player to handle the command.
        """

    @abstractmethod
    async def cmd_play_url(self, player_id: str, url: str) -> None:
        """
        Send PLAY URL command to given player.

        This is called when the Queue wants the player to start playing a specific url.

            - player_id: player_id of the player to handle the command.
            - media: the QueueItem to start playing on the player.
        """

    async def cmd_play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> None:
        """
        Send PLAY MEDIA command to given player.

        This is called when the Queue wants the player to start playing a specific QueueItem.
        The player implementation can decide how to process the request, such as playing
        queue items one-by-one or enqueue all/some items.

            - player_id: player_id of the player to handle the command.
            - queue_item: the QueueItem to start playing on the player.
            - seek_position: start playing from this specific position.
            - fade_in: fade in the music at start (e.g. at resume).
        """
        # default implementation is to simply resolve the url and send the url to the player
        # player/provider implementations may override this default.
        url = await self.mass.streams.resolve_stream_url(
            queue_item=queue_item,
            player_id=player_id,
            seek_position=seek_position,
            fade_in=fade_in,
            content_type=ContentType.FLAC,
        )
        self.logger.info("Starting playback of %s", queue_item.name)
        await self.cmd_play_url(player_id, url)

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

    async def cmd_set_group_members(self, player_id: str, members: list[str]) -> None:
        """
        Handle SET_MEMBERS command for given playergroup.

        Update the memberlist of the given PlayerGroup.

            - player_id: player_id of the groupplayer to handle the command.
            - members: list of player ids to set as members.
        """
        # will only be called for players of type GROUP with SET_MEMBERS feature set.
        raise NotImplementedError()

    async def cmd_create_group(self, name: str) -> Player:
        """
        Handle CREATE_GROUP command for this player provider.

            - name: name for the new group.

        Returns the newly created PlayerGroup.
        """
        # will only be called if the provider has the CREATE_GROUP feature set.
        raise NotImplementedError()

    async def cmd_delete_group(self, player_id: str) -> None:
        """
        Handle DELETE_GROUP command for this player provider.

            - player_id: id of the group player to remove
        """
        # will only be called if the provider has the DELETE_GROUP feature set.
        raise NotImplementedError()

    # DO NOT OVERRIDE BELOW

    @property
    def players(self) -> list[Player]:
        """Return all players belonging to this provider."""
        # pylint: disable=no-member
        return [
            player for player in self.mass.players if player.provider == self.domain
        ]
