"""Logic to play music from MusicProviders to supported players."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Dict, Iterable, Iterator, Tuple, cast
from music_assistant.common.helpers.util import get_changed_keys

from music_assistant.common.models.enums import (
    EventType,
    MediaType,
    PlayerState,
    PlayerType,
    RepeatMode,
)
from music_assistant.common.models.errors import AlreadyRegisteredError, QueueEmpty
from music_assistant.common.models.player import Player
from music_assistant.common.models.player_queue import PlayerQueue
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import ROOT_LOGGER_NAME
from music_assistant.server.helpers.api import api_command
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from .players import PlayerController

LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.players.queue")


class PlayerQueueController:
    """Controller holding all logic to play music from MusicProviders to supported players."""

    def __init__(self, players: PlayerController) -> None:
        """Initialize class."""
        self.players = players
        self.mass = players.mass
        self._queues: dict[str, PlayerQueue] = {}
        self._queue_items: dict[str, list[QueueItem]] = {}
        self._prev_states: dict[str, dict] = {}

    def __iter__(self) -> Iterator[PlayerQueue]:
        """Iterate over (available) players."""
        return iter(self._queues.values())

    @api_command("players/queue/all")
    def all(self) -> Tuple[PlayerQueue]:
        """Return all registered PlayerQueues."""
        return tuple(self._queues.values())

    @api_command("players/queue/get")
    def get(self, queue_id: str) -> PlayerQueue | None:
        """Return PlayerQueue by queue_id or None if not found."""
        return self._queues.get(queue_id)

    @api_command("players/queue/items")
    def items(self, queue_id: str) -> list[QueueItem]:
        """Return all QueueItems for given PlayerQueue."""
        return self._queue_items.get(queue_id, [])

    async def register(self, queue_id: str) -> None:
        """Register PlayerQueue for given player/queue id."""

        # try to restore previous state
        cache_key = f"queue_state_{queue_id}"
        if prev_state := await self.mass.cache.get(cache_key):
            queue = PlayerQueue.from_dict(prev_state)
        else:
            queue = PlayerQueue(
                queue_id=queue_id,
                active=False,
                shuffle_enabled=False,
            )

        self._queues[queue_id] = queue
        # always call update to calculate state etc
        await self.update(queue_id)

    def update(self, queue_id: str) -> None:
        """Call when a PlayerQueue needs to be updated (e.g. when player updates)."""
        if queue_id not in self._queues:
            self.mass.create_task(self.register(queue_id))
            return

        player = self.players.get(queue_id)
        queue = self._queues[queue_id]
        # queue is active when underlying player has the queue_id loaded as stream url
        queue.active = f"/{queue_id}/" in player.current_url

        if queue.active:
            queue.state = player.state
            queue.elapsed_time = player.corrected_elapsed_time
            queue.elapsed_time_last_updated = time.time()
            queue.current_item = self._get_item_by_index(queue.current_index)
            next_index = self.get_next_index(queue.current_index)
            queue.next_item = self._get_item_by_index(next_index)
        else:
            queue.state = PlayerState.IDLE
            queue.current_item = None
            queue.next_item = None

        # basic throttle: do not send state changed events if queue did not actually change
        prev_state = self._prev_states.get(queue_id, {})
        new_state = self._queues[queue_id].to_dict()
        changed_keys = get_changed_keys(prev_state, new_state)

        if len(changed_keys) == 0:
            return

        if "elapsed_time" in changed_keys:
            self.mass.signal_event(
                EventType.QUEUE_TIME_UPDATED, object_id=queue_id, data=queue
            )
        elif changed_keys != {"elapsed_time"}:
            self.mass.signal_event(
                EventType.QUEUE_UPDATED, object_id=self.player_id, data=self
            )

    # Queue commands

    @api_command("players/queue/cmd/stop")
    async def stop(self, queue_id: str) -> None:
        """
        Handle STOP command for given queue.
            - queue_id: queue_id of the playerqueue to handle the command.
        """
        if self._queues[queue_id].announcement_in_progress:
            LOGGER.warning(
                "Ignore queue command for %s because an announcement is in progress."
            )
            return
        # simply forward the command to underlying player
        await self.players.cmd_stop(queue_id)

    @api_command("players/queue/cmd/play")
    async def play(self, queue_id: str) -> None:
        """
        Handle PLAY command for given queue.
            - queue_id: queue_id of the playerqueue to handle the command.
        """
        if self._queues[queue_id].announcement_in_progress:
            LOGGER.warning(
                "Ignore queue command for %s because an announcement is in progress."
            )
            return
        if self._queues[queue_id].state == PlayerState.PAUSED:
            # simply forward the command to underlying player
            await self.players.cmd_play(queue_id)
        else:
            await self.resume(queue_id)

    @api_command("players/queue/cmd/pause")
    async def pause(self, queue_id: str) -> None:
        """
        Handle PAUSE command for given queue.
            - queue_id: queue_id of the playerqueue to handle the command.
        """
        if self._queues[queue_id].announcement_in_progress:
            LOGGER.warning(
                "Ignore queue command for %s because an announcement is in progress."
            )
            return
        # simply forward the command to underlying player
        await self.players.cmd_pause(queue_id)

    @api_command("players/queue/cmd/play_pause")
    async def play_pause(self, queue_id: str) -> None:
        """
        Toggle play/pause on given playerqueue.
            - queue_id: queue_id of the queue to handle the command.
        """
        if self._queues[queue_id].state == PlayerState.PLAYING:
            await self.pause()
            return
        await self.play()

    @api_command("players/queue/cmd/next")
    async def next(self, queue_id: str) -> None:
        """
        Handle NEXT TRACK command for given queue.
            - queue_id: queue_id of the queue to handle the command.
        """
        current_index = self._queues[queue_id].current_index
        next_index = self.get_next_index(queue_id, current_index, True)
        if next_index is None:
            return None
        await self.play_index(queue_id, next_index)

    @api_command("players/queue/cmd/previous")
    async def previous(self, queue_id: str) -> None:
        """
        Handle PREVIOUS TRACK command for given queue.
            - queue_id: queue_id of the queue to handle the command.
        """
        current_index = self._queues[queue_id].current_index
        if current_index is None:
            return
        await self.play_index(queue_id, max(current_index - 1, 0))

    @api_command("players/queue/cmd/skip")
    async def skip(self, queue_id: str, seconds: int = 10) -> None:
        """
        Handle SKIP command for given queue.

            - queue_id: queue_id of the queue to handle the command.
            - seconds: number of seconds to skip in track. Use negative value to skip back.
        """
        await self.seek(queue_id, self._queues[queue_id].elapsed_time + seconds)

    @api_command("players/queue/cmd/seek")
    async def seek(self, queue_id: str, position: int = 10) -> None:
        """
        Handle SEEK command for given queue.

            - queue_id: queue_id of the queue to handle the command.
            - position: position in seconds to seek to in the track.
        """
        queue = self._queues[queue_id]
        assert queue.current_item, "No item loaded"
        assert queue.current_item.media_item.media_type == MediaType.TRACK
        assert queue.current_item.duration
        assert position < queue.current_item.duration
        await self.play_index(queue_id, queue.current_index, position)

    @api_command("players/queue/cmd/resume")
    async def resume(self, queue_id: str) -> None:
        """
        Handle RESUME command for given queue.

            - queue_id: queue_id of the queue to handle the command.
        """
        queue = self._queues[queue_id]
        queue_items = self._queue_items[queue_id]
        resume_item = queue.current_item
        next_item = queue.next_item
        resume_pos = queue.elapsed_time
        if (
            resume_item
            and next_item
            and resume_item.duration
            and resume_pos > (resume_item.duration * 0.9)
        ):
            # track is already played for > 90% - skip to next
            resume_item = next_item
            resume_pos = 0
        elif queue.current_index is None and len(queue_items) > 0:
            # items available in queue but no previous track, start at 0
            resume_item = self.get_item(queue_id, 0)
            resume_pos = 0

        if resume_item is not None:
            resume_pos = resume_pos if resume_pos > 10 else 0
            fade_in = resume_pos > 0
            await self.play_index(queue_id, resume_item.item_id, resume_pos, fade_in)
        else:
            raise QueueEmpty(f"Resume queue requested but queue {queue_id} is empty")

    @api_command("players/queue/play_index")
    async def play_index(
        self,
        queue_id: str,
        index: int | str,
        seek_position: int = 0,
        fade_in: bool = False,
        passive: bool = False,
    ) -> None:
        """Play item at index (or item_id) X in queue."""
        queue = self._queues[queue_id]
        queue_items = self._queue_items[queue_id]
        if queue.announcement_in_progress:
            LOGGER.warning(
                "Ignore queue command for %s because an announcement is in progress."
            )
            return
        if stream := self.stream:
            # make sure that the previous stream is not auto restarted (race condition)
            stream.signal_next = None
        if not isinstance(index, int):
            index = self.index_by_id(index)
        if index is None:
            raise FileNotFoundError(f"Unknown index/id: {index}")
        if not len(self.items) > index:
            return
        self._current_index = index
        # start the queue stream
        stream = await self.queue_stream_start(
            start_index=index,
            seek_position=int(seek_position),
            fade_in=fade_in,
        )
        # execute the play command on the player(s)
        if not passive:
            await self.player.play_url(stream.url)




    def get_item(self, queue_id: str, index: int) -> QueueItem | None:
        """Get queue item by index."""
        queue_items = self._queue_items[queue_id]
        if index is not None and len(queue_items) > index:
            return queue_items[index]
        return None

    def item_by_id(self, queue_id: str, queue_item_id: str) -> QueueItem | None:
        """Get item by queue_item_id from queue."""
        if not queue_item_id:
            return None
        queue_items = self._queue_items[queue_id]
        return next((x for x in queue_items if x.item_id == queue_item_id), None)

    def index_by_id(self, queue_id: str, queue_item_id: str) -> int | None:
        """Get index by queue_item_id."""
        queue_items = self._queue_items[queue_id]
        for index, item in enumerate(queue_items):
            if item.item_id == queue_item_id:
                return index
        return None

    def get_next_index(
        self, queue_id: str, cur_index: int | None, is_skip: bool = False
    ) -> int:
        """Return the next index for the queue, accounting for repeat settings."""
        queue = self._queues[queue_id]
        queue_items = self._queue_items[queue_id]
        # handle repeat single track
        if queue.repeat_mode == RepeatMode.ONE and not is_skip:
            return cur_index
        # handle repeat all
        if (
            queue.repeat_mode == RepeatMode.ALL
            and queue_items
            and cur_index == (len(queue_items) - 1)
        ):
            return 0
        # simply return the next index. other logic is guarded to detect the index
        # being higher than the number of items to detect end of queue and/or handle repeat.
        if cur_index is None:
            return 0
        next_index = cur_index + 1
        return next_index

    def _get_item_by_index(self, queue_id: str, index: int) -> QueueItem | None:
        """Return QueueItem by index."""
        # TODO: account for repeat
        queue_items = self._queue_items[queue_id]
        if index <= (len(queue_items) - 1):
            return queue_items[index]
        return None
