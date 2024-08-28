"""Handle PlayerQueues related endpoints for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.common.models.enums import EventType, QueueOption, RepeatMode
from music_assistant.common.models.player_queue import PlayerQueue
from music_assistant.common.models.queue_item import QueueItem

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant.common.models.event import MassEvent
    from music_assistant.common.models.media_items import MediaItemType

    from .client import MusicAssistantClient


class PlayerQueues:
    """PlayerQueue related endpoints/data for Music Assistant."""

    def __init__(self, client: MusicAssistantClient) -> None:
        """Handle Initialization."""
        self.client = client
        # subscribe to player events
        client.subscribe(
            self._handle_event,
            (
                EventType.QUEUE_ADDED,
                EventType.QUEUE_UPDATED,
            ),
        )
        # the initial items are retrieved after connect
        self._queues: dict[str, PlayerQueue] = {}

    @property
    def player_queues(self) -> list[PlayerQueue]:
        """Return all player queues."""
        return list(self._queues.values())

    def __iter__(self) -> Iterator[PlayerQueue]:
        """Iterate over (available) PlayerQueues."""
        return iter(self._queues.values())

    def get(self, queue_id: str) -> PlayerQueue | None:
        """Return PlayerQueue by ID (or None if not found)."""
        return self._queues.get(queue_id)

    #  PlayerQueue related endpoints/commands

    async def get_player_queue_items(
        self, queue_id: str, limit: int = 500, offset: int = 0
    ) -> list[QueueItem]:
        """Get all QueueItems for given PlayerQueue."""
        return [
            QueueItem.from_dict(obj)
            for obj in await self.client.send_command(
                "player_queues/items", queue_id=queue_id, limit=limit, offset=offset
            )
        ]

    async def get_active_queue(self, player_id: str) -> PlayerQueue:
        """Return the current active/synced queue for a player."""
        return PlayerQueue.from_dict(
            await self.client.send_command("player_queues/get_active_queue", player_id=player_id)
        )

    async def queue_command_play(self, queue_id: str) -> None:
        """Send PLAY command to given queue."""
        await self.client.send_command("player_queues/play", queue_id=queue_id)

    async def queue_command_pause(self, queue_id: str) -> None:
        """Send PAUSE command to given queue."""
        await self.client.send_command("player_queues/pause", queue_id=queue_id)

    async def queue_command_stop(self, queue_id: str) -> None:
        """Send STOP command to given queue."""
        await self.client.send_command("player_queues/stop", queue_id=queue_id)

    async def queue_command_resume(self, queue_id: str, fade_in: bool | None = None) -> None:
        """Handle RESUME command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        """
        await self.client.send_command("player_queues/resume", queue_id=queue_id, fade_in=fade_in)

    async def queue_command_next(self, queue_id: str) -> None:
        """Send NEXT TRACK command to given queue."""
        await self.client.send_command("player_queues/next", queue_id=queue_id)

    async def queue_command_previous(self, queue_id: str) -> None:
        """Send PREVIOUS TRACK command to given queue."""
        await self.client.send_command("player_queues/previous", queue_id=queue_id)

    async def queue_command_clear(self, queue_id: str) -> None:
        """Send CLEAR QUEUE command to given queue."""
        await self.client.send_command("player_queues/clear", queue_id=queue_id)

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

        NOTE: Fails if the given QueueItem is already playing or loaded in the buffer.
        """
        await self.client.send_command(
            "player_queues/move_item",
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
            "player_queues/delete_item", queue_id=queue_id, item_id_or_index=item_id_or_index
        )

    async def queue_command_seek(self, queue_id: str, position: int) -> None:
        """
        Handle SEEK command for given queue.

        Parameters:
        - position: position in seconds to seek to in the current playing item.
        """
        await self.client.send_command("player_queues/seek", queue_id=queue_id, position=position)

    async def queue_command_skip(self, queue_id: str, seconds: int) -> None:
        """
        Handle SKIP command for given queue.

        Parameters:
        - seconds: number of seconds to skip in track. Use negative value to skip back.
        """
        await self.client.send_command("player_queues/skip", queue_id=queue_id, seconds=seconds)

    async def queue_command_shuffle(self, queue_id: str, shuffle_enabled: bool) -> None:
        """Configure shuffle mode on the the queue."""
        await self.client.send_command(
            "player_queues/shuffle", queue_id=queue_id, shuffle_enabled=shuffle_enabled
        )

    async def queue_command_repeat(self, queue_id: str, repeat_mode: RepeatMode) -> None:
        """Configure repeat mode on the the queue."""
        await self.client.send_command(
            "player_queues/repeat", queue_id=queue_id, repeat_mode=repeat_mode
        )

    async def play_index(
        self,
        queue_id: str,
        index: int | str,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> None:
        """Play item at index (or item_id) X in queue."""
        await self.client.send_command(
            "player_queues/repeat",
            queue_id=queue_id,
            index=index,
            seek_position=seek_position,
            fade_in=fade_in,
        )

    async def play_media(
        self,
        queue_id: str,
        media: MediaItemType | list[MediaItemType] | str | list[str],
        option: QueueOption | None = None,
        radio_mode: bool = False,
        start_item: str | None = None,
    ) -> None:
        """
        Play media item(s) on the given queue.

        - media: Media that should be played (MediaItem(s) or uri's).
        - queue_opt: Which enqueue mode to use.
        - radio_mode: Enable radio mode for the given item(s).
        - start_item: Optional item to start the playlist or album from.
        """
        await self.client.send_command(
            "player_queues/play_media",
            queue_id=queue_id,
            media=media,
            option=option,
            radio_mode=radio_mode,
            start_item=start_item,
        )

    async def transfer_queue(
        self,
        source_queue_id: str,
        target_queue_id: str,
        auto_play: bool | None = None,
    ) -> None:
        """Transfer queue to another queue."""
        await self.client.send_command(
            "player_queues/transfer",
            source_queue_id=source_queue_id,
            target_queue_id=target_queue_id,
            auto_play=auto_play,
            require_schema=25,
        )

    # Other endpoints/commands

    async def _get_player_queues(self) -> list[PlayerQueue]:
        """Fetch all PlayerQueues from the server."""
        return [
            PlayerQueue.from_dict(item)
            for item in await self.client.send_command("player_queues/all")
        ]

    async def fetch_state(self) -> None:
        """Fetch initial state once the server is connected."""
        for queue in await self._get_player_queues():
            self._queues[queue.queue_id] = queue

    def _handle_event(self, event: MassEvent) -> None:
        """Handle incoming player(queue) event."""
        if event.event in (EventType.QUEUE_ADDED, EventType.QUEUE_UPDATED):
            # Queue events always have an object_id
            assert event.object_id
            self._queues[event.object_id] = PlayerQueue.from_dict(event.data)
