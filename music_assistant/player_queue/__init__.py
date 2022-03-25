"""Models and helpers for a player queue."""
from __future__ import annotations
import asyncio

import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from music_assistant.constants import (
    EVENT_QUEUE_ITEMS_UPDATED,
    EVENT_QUEUE_UPDATED,
)
from music_assistant.helpers.datetime import now
from music_assistant.helpers.typing import (
    MusicAssistant,
    Player,
)
from ...helpers.util import callback, create_task
from ...helpers.web import api_route
from ...models.media_types import ItemMapping, MediaItem, MediaType, Radio, Track
from ..player.player import PlayerFeature, PlayerState
from ...models.streamdetails import StreamDetails
from .models import PlayerQueue, QueueItem, QueueOption

LOGGER = logging.getLogger("queues")


class PlayerQueueController:
    """Controller holding and managing PlayerQueue's."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        self.mass: MusicAssistant = mass
        self._items: Dict[str, PlayerQueue] = {}
        self._save_tasks: Dict[str, asyncio.TimerHandle] = []
        # load previous queue settings from disk
        create_task(self._restore_saved_state())

    async def setup(self):
        """Async initialize of module."""
        # prepare database
        await self.mass.database.execute(
            """CREATE TABLE IF NOT EXISTS player_queues(
                queue_id TEXT NOT NUL PRIMARY KEY,
                shuffle_enabled BOOLEAN,
                repeat_enabled BOOLEAN,
                crossfade_duration INTEGER,
                name TEXT,
                players json,
                items json,
                current_index INTEGER
                ;"""
        )

    @property
    def items(self) -> List[PlayerQueue]:
        """Return all PlayerQueue's."""
        return list(self._items.values())

    def get(self, queue_id: str) -> PlayerQueue:
        """Get PlayerQueue by ID. Create new queue if not exists."""
        if queue_id not in self._items:
            # create queue on the fly if it doesn't exist
            self._items[queue_id] = PlayerQueue(queue_id)
        return self._items[queue_id]

    @api_route("queues/{queue_id}/play_media", method="PUT")
    async def play_media(
        self,
        queue_id: str,
        uris: str | List[str],
        queue_opt: QueueOption = QueueOption.PLAY,
    ):
        """
        Play media item(s) on the given queue.

            :param queue_id: queue id of the PlayerQueue to handle the command.
            :param uri: uri(s) that should be played (single item or list of uri's).
            :param queue_opt:
                QueueOption.PLAY -> Insert new items in queue and start playing at inserted position
                QueueOption.REPLACE -> Replace queue contents with these items
                QueueOption.NEXT -> Play item(s) after current playing item
                QueueOption.ADD -> Append new items at end of the queue
        """
        # a single item or list of items may be provided
        if not isinstance(uris, list):
            uris = [uri]
        queue_items = []
        for uri in uris:
            if uri.startswith("http"):
                # a plain url was provided
                queue_items.append(QueueItem(uri))
                continue
            media_item = await self.mass.music.get_item_by_uri(uri)
            if not media_item:
                raise FileNotFoundError(f"Invalid uri: {uri}")
            # collect tracks to play
            if media_item.media_type == MediaType.ARTIST:
                tracks = await self.mass.music.get_artist_toptracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            elif media_item.media_type == MediaType.ALBUM:
                tracks = await self.mass.music.get_album_tracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            elif media_item.media_type == MediaType.PLAYLIST:
                tracks = await self.mass.music.get_playlist_tracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            elif media_item.media_type == MediaType.RADIO:
                # single radio
                tracks = [
                    await self.mass.music.get_radio(
                        media_item.item_id, provider_id=media_item.provider
                    )
                ]
            else:
                # single track
                tracks = [
                    await self.mass.music.get_track(
                        media_item.item_id, provider_id=media_item.provider
                    )
                ]
            for track in tracks:
                if not track.available:
                    continue
                queue_items.append(QueueItem.from_media_item(track))

        # load items into the queue
        if queue_opt == QueueOption.REPLACE:
            return await self.load(queue_id, queue_items)
        if queue_opt in [QueueOption.PLAY, QueueOption.NEXT] and len(queue_items) > 100:
            return await self.load(queue_id, queue_items)
        if queue_opt == QueueOption.NEXT:
            return await self.insert(queue_id, queue_items, 1)
        if queue_opt == QueueOption.PLAY:
            return await self.insert(queue_id, queue_items, 0)
        if queue_opt == QueueOption.ADD:
            return await self.append(queue_id, queue_items)

    def get_stream_url(self, queue_id: str, player_id: str = "") -> str:
        """Return the full stream url for a PlayerQueue Stream."""
        url = f"{self.mass.web.stream_url}/queue/{queue_id}?player_id={player_id}"
        # we set the checksum just to invalidate cache stuff
        url += f"&checksum={int(time.time())}"
        return url

    async def set_shuffle_enabled(self, queue_id: str, enable_shuffle: bool) -> None:
        """Set shuffle."""
        player_queue = self.get(queue_id)
        if not player_queue.shuffle_enabled and enable_shuffle:
            # shuffle requested
            player_queue.shuffle_enabled = True
            if self.cur_index is not None:
                played_items = self.items[: self.cur_index]
                next_items = self.__shuffle_items(self.items[self.cur_index + 1 :])
                items = played_items + [self.cur_item] + next_items
                await self.update(items)
        elif player_queue.shuffle_enabled and not enable_shuffle:
            # unshuffle
            player_queue.shuffle_enabled = False
            if self.cur_index is not None:
                played_items = self.items[: self.cur_index]
                next_items = self.items[self.cur_index + 1 :]
                next_items.sort(key=lambda x: x.sort_index, reverse=False)
                items = played_items + [self.cur_item] + next_items
                await self.update(items)

    async def set_repeat_enabled(self, queue_id: str, enable_repeat: bool) -> None:
        """Set the repeat mode for this queue."""
        player_queue = self.get(queue_id)
        if player_queue.repeat_enabled != enable_repeat:
            player_queue.repeat_enabled = enable_repeat
            self.signal_update(player_queue)
            self.schedule_save_state(queue_id)

    async def stop(self, queue_id: str) -> None:
        """Stop command on queue player."""
        # TODO

    async def play(self, queue_id: str) -> None:
        """Play (unpause) command on queue player."""
        # TODO

    async def pause(self, queue_id: str) -> None:
        """Pause command on queue player."""
        # TODO

    async def next(self, queue_id: str) -> None:
        """Play the next track in the queue."""
        player_queue = self.get(queue_id)
        if player_queue.current_index is None:
            return
        await self.play_index(queue_id, player_queue.current_index + 1)

    async def previous(self, queue_id: str) -> None:
        """Play the previous track in the queue."""
        player_queue = self.get(queue_id)
        if player_queue.current_index is None:
            return
        await self.play_index(queue_id, player_queue.current_index - 1)

    async def resume(self, queue_id: str) -> None:
        """Resume previous queue."""
        player_queue = self.get(queue_id)
        # TODO: Support skipping to last known position
        if player_queue.items:
            prev_index = player_queue.current_index
            await self.play_index(queue_id, prev_index)
        else:
            LOGGER.warning(
                "resume queue requested for %s but queue is empty", player_queue.name
            )

    async def play_index(self, queue_id: str, index: int | str) -> None:
        """Play item at index (or item_id) X in queue."""
        player_queue = self.get(queue_id)
        if not isinstance(index, int):
            index = player_queue.index_by_id(index)
        if index is None:
            raise FileNotFoundError("Unknown index/id: %s" % index)
        if not len(self.items) > index:
            return
        player_queue.current_index = index
        player_queue.queue_stream_next_index = index

        # send stream url to all players connected to this queue
        for player_id in player_queue.players:
            player = self.mass.players.get_player(player_id)
            if not player:
                continue

            queue_stream_url = self.get_stream_url(player_id)
            await self.mass.players.play_uri(player_id, queue_stream_url)

    async def move_item(
        self, queue_id: str, queue_item_id: str, pos_shift: int = 1
    ) -> None:
        """
        Move queue item x up/down the queue.

        param pos_shift: move item x positions down if positive value
                         move item x positions up if negative value
                         move item to top of queue as next item if 0
        """
        player_queue = self.get(queue_id)
        items = self.items.copy()
        item_index = self.__index_by_id(queue_item_id)
        if pos_shift == 0 and self.player.state == PlayerState.PLAYING:
            new_index = self.cur_index + 1
        elif pos_shift == 0:
            new_index = self.cur_index
        else:
            new_index = item_index + pos_shift
        if (new_index < self.cur_index) or (new_index > len(self.items)):
            return
        # move the item in the list
        items.insert(new_index, items.pop(item_index))
        await self.update(items)

    async def load(self, queue_id: str, queue_items: List[QueueItem]) -> None:
        """Load (overwrite) queue with new items."""
        player_queue = self.get(queue_id)
        for index, item in enumerate(queue_items):
            item.sort_index = index
        if self._shuffle_enabled and len(queue_items) > 5:
            queue_items = self.__shuffle_items(queue_items)
        self._items = queue_items
        await self.play_index(0)
        self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self)
        create_task(self._save_state())

    async def insert(
        self, queue_id: str, queue_items: List[QueueItem], offset: int = 0
    ) -> None:
        """
        Insert new items at offset x from current position.

        Keeps remaining items in queue.
        if offset 0, will start playing newly added item(s)
            :param queue_items: a list of QueueItem
            :param offset: offset from current queue position
        """
        player_queue = self.get(queue_id)
        if not self.items or self.cur_index is None:
            return await self.load(queue_items)
        insert_at_index = self.cur_index + offset
        for index, item in enumerate(queue_items):
            item.sort_index = insert_at_index + index
        if self.shuffle_enabled and len(queue_items) > 5:
            queue_items = self.__shuffle_items(queue_items)
        if offset == 0:
            # replace current item with new
            self._items = (
                self._items[:insert_at_index]
                + queue_items
                + self._items[insert_at_index + 1 :]
            )
        else:
            self._items = (
                self._items[:insert_at_index]
                + queue_items
                + self._items[insert_at_index:]
            )

        if offset == 0:
            await self.play_index(insert_at_index)

        self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self)
        create_task(self._save_state())

    async def append(self, queue_id: str, queue_items: List[QueueItem]) -> None:
        """Append new items at the end of the queue."""
        player_queue = self.get(queue_id)
        for index, item in enumerate(queue_items):
            item.sort_index = len(self.items) + index
        if self.shuffle_enabled:
            played_items = self.items[: self.cur_index]
            next_items = self.items[self.cur_index + 1 :] + queue_items
            next_items = self.__shuffle_items(next_items)
            items = played_items + [self.cur_item] + next_items
            return await self.update(items)
        self._items = self._items + queue_items
        self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self)
        create_task(self._save_state())

    async def update(self, queue_id: str, queue_items: List[QueueItem]) -> None:
        """Update the existing queue items, mostly caused by reordering."""
        player_queue = self.get(queue_id)
        self._items = queue_items
        self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self)
        create_task(self._save_state())

    async def clear(self, queue_id: str) -> None:
        """Clear all items in the queue."""
        player_queue = self.get(queue_id)
        await self.stop()
        await self.update([])

    # @callback
    # def update_state(self) -> None:
    #     """Update queue details, called when player updates."""
    #     new_index = self._cur_index
    #     track_time = self._cur_item_time
    #     new_item_loaded = False
    #     # handle queue stream
    #     if self._state == PlayerState.PLAYING and self.elapsed_time > 1:
    #         new_index, track_time = self.__get_queue_stream_index()

    #     # process new index
    #     if self._cur_index != new_index:
    #         # queue track updated
    #         self._cur_index = new_index
    #     # check if a new track is loaded, wait for the streamdetails
    #     if (
    #         self.cur_item
    #         and self._last_item != self.cur_item
    #         and self.cur_item.streamdetails
    #     ):
    #         # new active item in queue
    #         new_item_loaded = True
    #         # invalidate previous streamdetails
    #         if self._last_item:
    #             self._last_item.streamdetails = None
    #         self._last_item = self.cur_item
    #     # update vars and signal update on eventbus if needed
    #     prev_item_time = int(self._cur_item_time)
    #     self._cur_item_time = int(track_time)
    #     if self._last_state != self.state:
    #         # fire event with updated state
    #         self.signal_update()
    #         self._last_state = self.state
    #     elif abs(prev_item_time - self._cur_item_time) > 3:
    #         # only send media_position if it changed more then 3 seconds (e.g. skipping)
    #         self.signal_update()
    #     elif new_item_loaded:
    #         self.signal_update()

    async def queue_stream_start(self) -> None:
        """Call when queue_streamer starts playing the queue stream."""
        self._cur_item_time = 0
        self._cur_index = self._queue_stream_next_index
        self._queue_stream_next_index += 1
        self._queue_stream_start_index = self._cur_index
        return self._cur_index

    async def queue_stream_next(self, cur_index: int) -> None:
        """Call when queue_streamer loads next track in buffer."""
        next_index = 0
        if len(self.items) > (next_index):
            next_index = cur_index + 1
        elif self._repeat_enabled:
            # repeat enabled, start queue at beginning
            next_index = 0
        self._queue_stream_next_index = next_index + 1
        return next_index

    def signal_update(self, queue: PlayerQueue):
        """Signal update of a Queue on the eventbus."""
        self.mass.signal_event(
            EVENT_QUEUE_UPDATED,
            queue,
        )

    # @callback
    # def __get_queue_stream_index(self) -> Tuple[int, int]:
    #     """Get index of queue stream."""
    #     # player is playing a constant stream of the queue so we need to do this the hard way
    #     queue_index = 0
    #     elapsed_time_queue = self.player.elapsed_time
    #     total_time = 0
    #     track_time = 0
    #     if self.items and len(self.items) > self._queue_stream_start_index:
    #         queue_index = (
    #             self._queue_stream_start_index
    #         )  # holds the last starting position
    #         queue_track = None
    #         while len(self.items) > queue_index:
    #             queue_track = self.items[queue_index]
    #             if elapsed_time_queue > (queue_track.duration + total_time):
    #                 total_time += queue_track.duration
    #                 queue_index += 1
    #             else:
    #                 track_time = elapsed_time_queue - total_time
    #                 break
    #     return queue_index, track_time

    @staticmethod
    def __shuffle_items(queue_items: List[QueueItem]) -> List[QueueItem]:
        """Shuffle a list of tracks."""
        # for now we use default python random function
        # can be extended with some more magic based on last_played and stuff
        return random.sample(queue_items, len(queue_items))

    async def _restore_saved_state(self) -> Dict[str, Any]:
        """Try to load the saved state from database."""
        for db_row in await self.mass.database.get_rows("player_queues"):
            player_queue = PlayerQueue.from_db_row(db_row)
            self._items[player_queue.queue_id] = player_queue

    async def _save_state(self, queue_id: str) -> None:
        """Save state of given queue in database."""
        await self.mass.database.insert_or_replace(
            "player_queues", self._items[queue_id].to_dict()
        )

    def schedule_save_state(self, queue_id: str) -> None:
        """Schedule save of the queue."""

        def do_save():
            asyncio.create_task(self._save_state(queue_id))

        existing_task = self._save_tasks.pop(queue_id, None)
        if existing_task and not existing_task.cancelled():
            existing_task.cancel()
        self._save_tasks[queue_id] = self.mass.loop.call_later(60, do_save)
