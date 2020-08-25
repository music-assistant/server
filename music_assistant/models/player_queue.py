#!/usr/bin/env python3
# -*- coding:utf-8 -*-

"""
    Models and helpers for a player queue.
"""

import asyncio
from enum import Enum
import random
from typing import List
import uuid

from music_assistant.constants import (
    EVENT_PLAYBACK_STARTED,
    EVENT_PLAYBACK_STOPPED,
    EVENT_QUEUE_ITEMS_UPDATED,
    EVENT_QUEUE_UPDATED,
)
from music_assistant.models.media_types import Track
from music_assistant.models.player import PlayerState
from music_assistant.utils import LOGGER, serialize_values, callback

# pylint: disable=too-many-instance-attributes
# pylint: disable=too-many-public-methods
# pylint: disable=too-few-public-methods


class QueueOption(str, Enum):
    """Enum representation of the queue (play) options"""

    Play = "play"
    Replace = "replace"
    Next = "next"
    Add = "add"


class QueueItem(Track):
    """Representation of a queue item, extended version of track."""

    def __init__(self, media_item=None):
        super().__init__()
        self.streamdetails = {}
        self.uri = ""
        self.queue_item_id = str(uuid.uuid4())
        # if existing media_item given, load those values
        if media_item:
            for key, value in media_item.__dict__.items():
                setattr(self, key, value)


class PlayerQueue:
    """
        Model for a player's queue.
        Can be overriden by custom implementation, but will not be needed
        in most cases.
    """

    def __init__(self, mass, player):
        self.mass = mass
        self._player = player
        self._items = []
        self._shuffle_enabled = False
        self._repeat_enabled = False
        self._cur_index = 0
        self._cur_item_time = 0
        self._last_item_time = 0
        self._last_queue_startindex = 0
        self._next_queue_startindex = 0
        self._last_player_state = PlayerState.Stopped
        self._last_track = None
        asyncio.run_coroutine_threadsafe(
            self.mass.add_event_listener(self.on_shutdown, "shutdown"),
            self.mass.loop,
        )
        # load previous queue settings from disk
        asyncio.run_coroutine_threadsafe(
            self.__restore_saved_state(), self.mass.loop
        )

    @property
    def shuffle_enabled(self):
        """Shuffle enabled property"""
        return self._shuffle_enabled

    @shuffle_enabled.setter
    def shuffle_enabled(self, enable_shuffle: bool):
        """enable/disable shuffle"""
        if not self._shuffle_enabled and enable_shuffle:
            # shuffle requested
            self._shuffle_enabled = True
            if self.cur_index is not None:
                played_items = self.items[: self.cur_index]
                next_items = self.__shuffle_items(self.items[self.cur_index + 1 :])
                items = played_items + [self.cur_item] + next_items
                self.mass.loop.create_task(self.update(items))
        elif self._shuffle_enabled and not enable_shuffle:
            # unshuffle
            self._shuffle_enabled = False
            if self.cur_index is not None:
                played_items = self.items[: self.cur_index]
                next_items = self.items[self.cur_index + 1 :]
                next_items.sort(key=lambda x: x.sort_index, reverse=False)
                items = played_items + [self.cur_item] + next_items
                self.mass.loop.create_task(self.update(items))
        self.mass.loop.create_task(self.update_state())

    @property
    def repeat_enabled(self):
        """Returns if crossfade is enabled for this player."""
        return self._repeat_enabled

    @repeat_enabled.setter
    def repeat_enabled(self, enable_repeat: bool):
        """Set the repeat mode for this queue."""
        if self._repeat_enabled != enable_repeat:
            self._repeat_enabled = enable_repeat
            self.mass.loop.create_task(self.update_state())
            self.mass.loop.create_task(self.__save_state())

    @property
    def crossfade_enabled(self):
        """Returns if crossfade is enabled for this player's queue."""
        return self._player.settings.get("crossfade_duration", 0) > 0

    @property
    def gapless_enabled(self):
        """Returns if gapless support is enabled for this player."""
        return self._player.settings.get("gapless_enabled", True)

    @property
    def cur_index(self):
        """
            Returns the current index of the queue.
            Returns None if queue is empty.
        """
        if not self._items:
            return None
        return self._cur_index

    @property
    def cur_item_id(self):
        """
            Return the queue item id of the current item in the queue.
            Returns None if queue is empty.
        """
        if self.cur_index is None or not len(self.items) > self.cur_index:
            return None
        return self.items[self.cur_index].queue_item_id

    @property
    def cur_item(self):
        """
            Return the current item in the queue.
            Returns None if queue is empty.
        """
        if self.cur_index is None or not len(self.items) > self.cur_index:
            return None
        return self.items[self.cur_index]

    @property
    def cur_item_time(self):
        """Returns the time (progress) for current (playing) item."""
        return self._cur_item_time

    @property
    def next_index(self):
        """
            Returns the next index for this player's queue.
            Returns None if queue is empty or no more items.
        """
        if not self.items:
            # queue is empty
            return None
        if self.cur_index is None:
            # playback started
            return 0
        else:
            # player already playing (or paused) so return the next item
            if len(self.items) > (self.cur_index + 1):
                return self.cur_index + 1
            if self._repeat_enabled:
                # repeat enabled, start queue at beginning
                return 0
        return None

    @property
    def next_item(self):
        """
            Returns the next item in the queue.
            Returns None if queue is empty or no more items.
        """
        if self.next_index is not None:
            return self.items[self.next_index]
        return None

    @property
    def items(self):
        """
            Returns all queue items for this player's queue.
        """
        return self._items

    @property
    def use_queue_stream(self):
        """
            bool to indicate that we need to use the queue stream
            for example if crossfading is requested but a player doesn't natively support it
            it will send a constant stream of audio to the player and all tracks
        """
        return (self.crossfade_enabled and not self._player.supports_crossfade) or (
            self.gapless_enabled and not self._player.supports_gapless
        )

    @callback
    def get_item(self, index):
        """get item by index from queue"""
        if index is not None and len(self.items) > index:
            return self.items[index]
        return None

    @callback
    def by_item_id(self, queue_item_id: str):
        """get item by queue_item_id from queue"""
        if not queue_item_id:
            return None
        for item in self.items:
            if item.queue_item_id == queue_item_id:
                return item
        return None

    async def async_next(self):
        """Play the next track in the queue."""
        if self.cur_index is None:
            return
        if self.use_queue_stream:
            return await self.play_index(self.cur_index + 1)
        else:
            return await self._player.cmd_next()

    async def async_previous(self):
        """Play the previous track in the queue."""
        if self.cur_index is None:
            return
        if self.use_queue_stream:
            return await self.play_index(self.cur_index - 1)
        else:
            return await self._player.cmd_previous()

    async def async_resume(self):
        """Resume previous queue."""
        if self.items:
            prev_index = self.cur_index
            if self.use_queue_stream or not self._player.supports_queue:
                await self.play_index(prev_index)
            else:
                # at this point we don't know if the queue is synced with the player
                # so just to be safe we send the queue_items to the player
                await self._player.cmd_queue_load(self.items)
                await self.play_index(prev_index)
        else:
            LOGGER.warning(
                "resume queue requested for %s but queue is empty", self._player.name
            )

    async def async_play_index(self, index):
        """Play item at index X in queue."""
        if not isinstance(index, int):
            index = self.__index_by_id(index)
        if not len(self.items) > index:
            return
        if self.use_queue_stream:
            self._next_queue_startindex = index
            queue_stream_uri = "http://%s:%s/stream/%s" % (
                self.mass.web.local_ip,
                self.mass.web.http_port,
                self._player.player_id,
            )
            return await self._player.cmd_play_uri(queue_stream_uri)
        elif self._player.supports_queue:
            return await self._player.cmd_queue_play_index(index)
        else:
            return await self._player.cmd_play_uri(self._items[index].uri)

    async def async_move_item(self, queue_item_id, pos_shift=1):
        """
            move queue item x up/down the queue
            param pos_shift: move item x positions down if positive value
                             move item x positions up if negative value
                             move item to top of queue as next item
       """
        items = self.items.copy()
        item_index = self.__index_by_id(queue_item_id)
        if pos_shift == 0 and self._player.state == PlayerState.Playing:
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
        if pos_shift == 0:
            await self.play_index(new_index)

    async def async_load(self, queue_items: List[QueueItem]):
        """load (overwrite) queue with new items"""
        for index, item in enumerate(queue_items):
            item.sort_index = index
        if self._shuffle_enabled:
            queue_items = self.__shuffle_items(queue_items)
        self._items = queue_items
        if self.use_queue_stream or not self._player.supports_queue:
            await self.play_index(0)
        else:
            await self._player.cmd_queue_load(queue_items)
        await self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self.to_dict())
        self.mass.loop.create_task(self.__save_state())

    async def async_insert(self, queue_items: List[QueueItem], offset=0):
        """
            insert new items at offset x from current position
            keeps remaining items in queue
            if offset 0, will start playing newly added item(s)
                :param queue_items: a list of QueueItem
                :param offset: offset from current queue position
       """

        if (
            not self.items
            or self.cur_index is None
            or self.cur_index + offset > len(self.items)
        ):
            return await self.load(queue_items)
        insert_at_index = self.cur_index + offset
        for index, item in enumerate(queue_items):
            item.sort_index = insert_at_index + index
        if self.shuffle_enabled:
            queue_items = self.__shuffle_items(queue_items)
        self._items = (
            self._items[:insert_at_index] + queue_items + self._items[insert_at_index:]
        )
        if self.use_queue_stream or not self._player.supports_queue:
            if offset == 0:
                await self.play_index(insert_at_index)
        else:
            try:
                await self._player.cmd_queue_insert(queue_items, insert_at_index)
            except NotImplementedError:
                # not supported by player, use load queue instead
                LOGGER.debug(
                    "cmd_queue_insert not supported by player, fallback to cmd_queue_load "
                )
                self._items = self._items[self.cur_index :]
                await self._player.cmd_queue_load(self._items)
        self.mass.loop.create_task(
            self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self.to_dict())
        )
        self.mass.loop.create_task(self.__save_state())

    async def async_append(self, queue_items: List[QueueItem]):
        """
            append new items at the end of the queue
       """
        for index, item in enumerate(queue_items):
            item.sort_index = len(self.items) + index
        if self.shuffle_enabled:
            played_items = self.items[: self.cur_index]
            next_items = self.items[self.cur_index :] + queue_items
            next_items = self.__shuffle_items(next_items)
            items = played_items + next_items
            return await self.update(items)
        self._items = self._items + queue_items
        if self._player.supports_queue and not self.use_queue_stream:
            try:
                await self._player.cmd_queue_append(queue_items)
            except NotImplementedError:
                # not supported by player, use load queue instead
                LOGGER.debug(
                    "cmd_queue_append not supported by player, fallback to cmd_queue_load "
                )
                self._items = self._items[self.cur_index :]
                await self._player.cmd_queue_load(self._items)
        self.mass.loop.create_task(
            self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self.to_dict())
        )
        self.mass.loop.create_task(self.__save_state())

    async def async_update(self, queue_items: List[QueueItem]):
        """
            update the existing queue items, mostly caused by reordering
       """
        self._items = queue_items
        if self._player.supports_queue and not self.use_queue_stream:
            try:
                await self._player.cmd_queue_update(queue_items)
            except NotImplementedError:
                # not supported by player, use load queue instead
                LOGGER.debug(
                    "cmd_queue_update not supported by player, fallback to cmd_queue_load "
                )
                self._items = self._items[self.cur_index :]
                await self._player.cmd_queue_load(self._items)
        self.mass.loop.create_task(
            self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self.to_dict())
        )
        self.mass.loop.create_task(self.__save_state())

    async def async_clear(self):
        """
            clear all items in the queue
       """
        await self._player.stop()
        self._items = []
        if self._player.supports_queue:
            try:
                await self._player.cmd_queue_update(self._items)
            except NotImplementedError:
                # not supported by player, ignore
                pass
        self.mass.loop.create_task(
            self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self.to_dict())
        )

    async def update_state(self):
        """update queue details, called when player updates"""
        cur_index = self._cur_index
        track_time = self._cur_item_time
        # handle queue stream
        if self.use_queue_stream and self._player.state == PlayerState.Playing:
            cur_index, track_time = self.__get_queue_stream_index()
        # normal queue based approach
        elif not self.use_queue_stream:
            track_time = self._player.cur_time
            for index, queue_item in enumerate(self.items):
                if queue_item.uri == self._player.cur_uri:
                    cur_index = index
                    break
        # process new index
        await self._async_process_queue_update(cur_index, track_time)
        await self.mass.signal_event(EVENT_QUEUE_UPDATED, self.to_dict())

    async def start_queue_stream(self):
        """called by the queue streamer when it starts playing the queue stream"""
        self._last_queue_startindex = self._next_queue_startindex
        return await self.get_item(self._next_queue_startindex)

    def to_dict(self):
        """instance attributes as dict so it can be serialized to json"""
        return {
            "player_id": self._player.player_id,
            "shuffle_enabled": self.shuffle_enabled,
            "repeat_enabled": self.repeat_enabled,
            "crossfade_enabled": self.crossfade_enabled,
            "gapless_enabled": self.gapless_enabled,
            "items": len(self.items),
            "cur_item_id": self.cur_item_id,
            "cur_index": self.cur_index,
            "next_index": self.next_index,
            "cur_item": serialize_values(self.cur_item),
            "cur_item_time": self.cur_item_time,
            "next_item": serialize_values(self.next_item),
            "queue_stream_enabled": self.use_queue_stream,
        }

    @callback
    def __get_queue_stream_index(self):
        # player is playing a constant stream of the queue so we need to do this the hard way
        queue_index = 0
        cur_time_queue = self._player.cur_time
        total_time = 0
        track_time = 0
        if self.items and len(self.items) > self._last_queue_startindex:
            queue_index = (
                self._last_queue_startindex
            )  # holds the last starting position
            queue_track = None
            while len(self.items) > queue_index:
                queue_track = self.items[queue_index]
                if cur_time_queue > (queue_track.duration + total_time):
                    total_time += queue_track.duration
                    queue_index += 1
                else:
                    track_time = cur_time_queue - total_time
                    break
            self._next_queue_startindex = queue_index + 1
        return queue_index, track_time

    async def async_process_queue_update(self, new_index, track_time):
        """compare the queue index to determine if playback changed"""
        new_track = await self.get_item(new_index)
        if (not self._last_track and new_track) or self._last_track != new_track:
            # queue track updated
            # account for track changing state so trigger track change after 1 second
            if self._last_track and self._last_track.streamdetails:
                self._last_track.streamdetails["seconds_played"] = self._last_item_time
                await self.mass.signal_event(
                    EVENT_PLAYBACK_STOPPED, self._last_track.streamdetails
                )
            if new_track and new_track.streamdetails:
                await self.mass.signal_event(
                    EVENT_PLAYBACK_STARTED, new_track.streamdetails
                )
                self._last_track = new_track
        if self._last_player_state != self._player.state:
            self._last_player_state = self._player.state
            if self._player.cur_time == 0 and self._player.state in [
                PlayerState.Stopped,
                PlayerState.Off,
            ]:
                # player stopped playing
                if self._last_track:
                    await self.mass.signal_event(
                        EVENT_PLAYBACK_STOPPED, self._last_track.streamdetails
                    )
        # update vars
        if track_time > 2:
            # account for track changing state so keep this a few seconds behind
            self._last_item_time = track_time
        self._cur_item_time = track_time
        self._cur_index = new_index

    @staticmethod
    def __shuffle_items(queue_items):
        """shuffle a list of tracks"""
        # for now we use default python random function
        # can be extended with some more magic last_played and stuff
        return random.sample(queue_items, len(queue_items))

    def __index_by_id(self, queue_item_id):
        """get index by queue_item_id"""
        item_index = None
        for index, item in enumerate(self.items):
            if item.queue_item_id == queue_item_id:
                item_index = index
        return item_index

    async def __restore_saved_state(self):
        """try to load the saved queue for this player from cache file"""
        cache_str = "queue_%s" % self._player.player_id
        cache_data = await self.mass.cache.get(cache_str)
        if cache_data:
            self._shuffle_enabled = cache_data["shuffle_enabled"]
            self._repeat_enabled = cache_data["repeat_enabled"]
            self._items = cache_data["items"]
            self._cur_index = cache_data["cur_item"]
            self._next_queue_startindex = cache_data["next_queue_index"]

    # pylint: disable=unused-argument
    async def on_shutdown(self, msg, msg_details):
        """Handle shutdown event, save queue state."""
        await self.__save_state()

    # pylint: enable=unused-argument

    async def __save_state(self):
        """save current queue settings to file"""
        cache_str = "queue_%s" % self._player.player_id
        cache_data = {
            "shuffle_enabled": self._shuffle_enabled,
            "repeat_enabled": self._repeat_enabled,
            "items": self._items,
            "cur_item": self._cur_index,
            "next_queue_index": self._next_queue_startindex,
        }
        await self.mass.cache.set(cache_str, cache_data)
        LOGGER.info("queue state saved to file for player %s", self._player.player_id)
