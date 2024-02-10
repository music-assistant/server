"""Handle player related endpoints for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.common.models.enums import EventType, QueueOption, RepeatMode
from music_assistant.common.models.player import Player
from music_assistant.common.models.player_queue import PlayerQueue
from music_assistant.common.models.queue_item import QueueItem

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant.common.models.event import MassEvent
    from music_assistant.common.models.media_items import MediaItemType

    from .client import MusicAssistantClient


class Players:
    """Player related endpoints/data for Music Assistant."""

    def __init__(self, client: MusicAssistantClient) -> None:
        """Handle Initialization."""
        self.client = client
        # subscribe to player events
        client.subscribe(
            self._handle_event,
            (
                EventType.PLAYER_ADDED,
                EventType.PLAYER_REMOVED,
                EventType.PLAYER_UPDATED,
                EventType.QUEUE_ADDED,
                EventType.QUEUE_UPDATED,
            ),
        )
        # below items are retrieved after connect
        self._players: dict[str, Player] = {}
        self._queues: dict[str, PlayerQueue] = {}

    @property
    def players(self) -> list[Player]:
        """Return all players."""
        return list(self._players.values())

    @property
    def player_queues(self) -> list[PlayerQueue]:
        """Return all player queues."""
        return list(self._queues.values())

    def __iter__(self) -> Iterator[Player]:
        """Iterate over (available) players."""
        return iter(self._players.values())

    def get_player(self, player_id: str) -> Player | None:
        """Return Player by ID (or None if not found)."""
        return self._players.get(player_id)

    def get_player_queue(self, queue_id: str) -> PlayerQueue | None:
        """Return PlayerQueue by ID (or None if not found)."""
        return self._queues.get(queue_id)

    #  Player related endpoints/commands

    async def get_players(self) -> list[Player]:
        """Fetch all Players from the server."""
        return [Player.from_dict(item) for item in await self.client.send_command("players/all")]

    async def player_command_stop(self, player_id: str) -> None:
        """Send STOP command to given player (directly)."""
        await self.client.send_command("players/cmd/stop", player_id=player_id)

    async def player_command_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player."""
        await self.client.send_command("players/cmd/power", player_id=player_id, powered=powered)

    async def player_command_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME SET command to given player."""
        await self.client.send_command(
            "players/cmd/volume_set", player_id=player_id, volume_level=volume_level
        )

    async def player_command_volume_up(self, player_id: str) -> None:
        """Send VOLUME UP command to given player."""
        await self.client.send_command("players/cmd/volume_up", player_id=player_id)

    async def player_command_volume_down(self, player_id: str) -> None:
        """Send VOLUME DOWN command to given player."""
        await self.client.send_command("players/cmd/volume_down", player_id=player_id)

    async def player_command_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        await self.client.send_command("players/cmd/volume_mute", player_id=player_id, muted=muted)

    async def player_command_sync(self, player_id: str, target_player: str) -> None:
        """
        Handle SYNC command for given player.

        Join/add the given player(id) to the given (master) player/sync group.
        If the player is already synced to another player, it will be unsynced there first.
        If the target player itself is already synced to another player, this will fail.
        If the player can not be synced with the given target player, this will fail.

          - player_id: player_id of the player to handle the command.
          - target_player: player_id of the syncgroup master or group player.
        """
        await self.client.send_command(
            "players/cmd/sync", player_id=player_id, target_player=target_player
        )

    async def player_command_unsync(self, player_id: str) -> None:
        """
        Handle UNSYNC command for given player.

        Remove the given player from any syncgroups it currently is synced to.
        If the player is not currently synced to any other player,
        this will silently be ignored.

          - player_id: player_id of the player to handle the command.
        """
        await self.client.send_command("players/cmd/unsync", player_id=player_id)

    #  PlayerGroup related endpoints/commands

    async def set_player_group_volume(self, player_id: str, volume_level: int) -> None:
        """
        Send VOLUME_SET command to given playergroup.

        Will send the new (average) volume level to group child's.
        - player_id: player_id of the playergroup to handle the command.
        - volume_level: volume level (0..100) to set on the player.
        """
        await self.client.send_command(
            "players/cmd/group_volume", player_id=player_id, volume_level=volume_level
        )

    async def set_player_group_members(self, player_id: str, members: list[str]) -> None:
        """
        Update the memberlist of the given PlayerGroup.

          - player_id: player_id of the groupplayer to handle the command.
          - members: list of player ids to set as members.
        """
        await self.client.send_command(
            "players/cmd/set_members", player_id=player_id, members=members
        )

    #  PlayerQueue related endpoints/commands

    async def get_player_queues(self) -> list[PlayerQueue]:
        """Fetch all PlayerQueues from the server."""
        return [
            PlayerQueue.from_dict(item)
            for item in await self.client.send_command("players/queue/all")
        ]

    async def get_player_queue_items(self, queue_id: str) -> list[QueueItem]:
        """Get all QueueItems for given PlayerQueue."""
        return [
            QueueItem.from_dict(item)
            for item in await self.client.send_command("players/queue/items", queue_id=queue_id)
        ]

    async def queue_command_play(self, queue_id: str) -> None:
        """Send PLAY command to given queue."""
        await self.client.send_command("players/queue/play", queue_id=queue_id)

    async def queue_command_pause(self, queue_id: str) -> None:
        """Send PAUSE command to given queue."""
        await self.client.send_command("players/queue/pause", queue_id=queue_id)

    async def queue_command_stop(self, queue_id: str) -> None:
        """Send STOP command to given queue."""
        await self.client.send_command("players/queue/stop", queue_id=queue_id)

    async def queue_command_next(self, queue_id: str) -> None:
        """Send NEXT TRACK command to given queue."""
        await self.client.send_command("players/queue/next", queue_id=queue_id)

    async def queue_command_previous(self, queue_id: str) -> None:
        """Send PREVIOUS TRACK command to given queue."""
        await self.client.send_command("players/queue/previous", queue_id=queue_id)

    async def queue_command_clear(self, queue_id: str) -> None:
        """Send CLEAR QUEUE command to given queue."""
        await self.client.send_command("players/queue/clear", queue_id=queue_id)

    async def queue_command_move_item(
        self, queue_id: str, queue_item_id: str, pos_shift: int = 1
    ) -> None:
        """
        Move queue item x up/down the queue.

        Parameters:
        - queue_id: id of the queue to process this request.
        - queue_item_id: the item_id of the queueitem that needs to be moved.
        - pos_shift: move item x positions down if positive value
        - pos_shift: move item x positions up if negative value
        - pos_shift:  move item to top of queue as next item if 0

        NOTE: Fails if the given QueueItem is already player or loaded in the buffer.
        """
        await self.client.send_command(
            "players/queue/move_item",
            queue_id=queue_id,
            queue_item_id=queue_item_id,
            pos_shift=pos_shift,
        )

    async def queue_command_move_up(self, queue_id: str, queue_item_id: str) -> None:
        """Move given queue item one place up in the queue."""
        await self.queue_command_move_item(
            queue_id=queue_id, queue_item_id=queue_item_id, pos_shift=-1
        )

    async def queue_command_move_down(self, queue_id: str, queue_item_id: str) -> None:
        """Move given queue item one place down in the queue."""
        await self.queue_command_move_item(
            queue_id=queue_id, queue_item_id=queue_item_id, pos_shift=1
        )

    async def queue_command_move_next(self, queue_id: str, queue_item_id: str) -> None:
        """Move given queue item as next up in the queue."""
        await self.queue_command_move_item(
            queue_id=queue_id, queue_item_id=queue_item_id, pos_shift=0
        )

    async def queue_command_delete(self, queue_id: str, item_id_or_index: int | str) -> None:
        """Delete item (by id or index) from the queue."""
        await self.client.send_command(
            "players/queue/delete_item", queue_id=queue_id, item_id_or_index=item_id_or_index
        )

    async def queue_command_seek(self, queue_id: str, position: int) -> None:
        """
        Handle SEEK command for given queue.

        Parameters:
        - position: position in seconds to seek to in the current playing item.
        """
        await self.client.send_command("players/queue/seek", queue_id=queue_id, position=position)

    async def queue_command_skip(self, queue_id: str, seconds: int) -> None:
        """
        Handle SKIP command for given queue.

        Parameters:
        - seconds: number of seconds to skip in track. Use negative value to skip back.
        """
        await self.client.send_command("players/queue/skip", queue_id=queue_id, seconds=seconds)

    async def queue_command_shuffle(self, queue_id: str, shuffle_enabled=bool) -> None:
        """Configure shuffle mode on the the queue."""
        await self.client.send_command(
            "players/queue/shuffle", queue_id=queue_id, shuffle_enabled=shuffle_enabled
        )

    async def queue_command_repeat(self, queue_id: str, repeat_mode: RepeatMode) -> None:
        """Configure repeat mode on the the queue."""
        await self.client.send_command(
            "players/queue/repeat", queue_id=queue_id, repeat_mode=repeat_mode
        )

    async def play_media(
        self,
        queue_id: str,
        media: MediaItemType | list[MediaItemType] | str | list[str],
        option: QueueOption | None = None,
        radio_mode: bool = False,
    ) -> None:
        """
        Play media item(s) on the given queue.

        - media: Media that should be played (MediaItem(s) or uri's).
        - queue_opt: Which enqueue mode to use.
        - radio_mode: Enable radio mode for the given item(s).
        """
        await self.client.send_command(
            "players/queue/play_media",
            queue_id=queue_id,
            media=media,
            option=option,
            radio_mode=radio_mode,
        )

    # Other endpoints/commands

    async def fetch_state(self) -> None:
        """Fetch initial state once the server is connected."""
        for player in await self.get_players():
            self._players[player.player_id] = player
        for queue in await self.get_player_queues():
            self._queues[queue.queue_id] = queue

    def _handle_event(self, event: MassEvent) -> None:
        """Handle incoming player(queue) event."""
        if event.event in (EventType.PLAYER_ADDED, EventType.PLAYER_UPDATED):
            self._players[event.object_id] = Player.from_dict(event.data)
            return
        if event.event == EventType.PLAYER_REMOVED:
            self._players.pop(event.object_id, None)
        if event.event in (EventType.QUEUE_ADDED, EventType.QUEUE_UPDATED):
            self._queues[event.object_id] = PlayerQueue.from_dict(event.data)
