"""Models and helpers for a player queue."""

import logging
import random
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import List

from music_assistant.constants import (
    EVENT_QUEUE_ITEMS_UPDATED,
    EVENT_QUEUE_TIME_UPDATED,
    EVENT_QUEUE_UPDATED,
)
from music_assistant.models.media_types import Track
from music_assistant.models.player import PlayerFeature, PlayerState
from music_assistant.models.streamdetails import StreamDetails
from music_assistant.utils import callback

# pylint: disable=too-many-instance-attributes
# pylint: disable=too-many-public-methods
# pylint: disable=too-few-public-methods

LOGGER = logging.getLogger("mass")


class QueueOption(Enum):
    """Enum representation of the queue (play) options."""

    Play = "play"
    Replace = "replace"
    Next = "next"
    Add = "add"


@dataclass
class QueueItem(Track):
    """Representation of a queue item, extended version of track."""

    streamdetails: StreamDetails = None
    uri: str = ""
    queue_item_id: str = ""

    def __init__(self, media_item=None):
        """Initialize class."""
        super().__init__()
        self.queue_item_id = str(uuid.uuid4())
        # if existing media_item given, load those values
        if media_item:
            for key, value in media_item.__dict__.items():
                setattr(self, key, value)


class PlayerQueue:
    """Class that holds the queue items for a player."""

    def __init__(self, mass, player):
        """Initialize class."""
        self.mass = mass
        self._player = player
        self._items = []
        self._shuffle_enabled = False
        self._repeat_enabled = False
        self._cur_index = 0
        self._cur_item_time = 0
        self._last_item = None
        self._next_queue_startindex = 0
        self._last_queue_startindex = 0
        self._last_player_state = PlayerState.Stopped
        # load previous queue settings from disk
        self.mass.add_job(self.__async_restore_saved_state())

    async def async_close(self):
        """Handle shutdown/close."""
        # pylint: disable=unused-argument
        await self.__async_save_state()

    @property
    def player(self):
        """Return handle to player."""
        return self._player

    @property
    def player_id(self):
        """Return handle to player."""
        return self._player.player_id

    @property
    def shuffle_enabled(self):
        """Return shuffle enabled property."""
        return self._shuffle_enabled

    @shuffle_enabled.setter
    def shuffle_enabled(self, enable_shuffle: bool):
        """Set shuffle."""
        if not self._shuffle_enabled and enable_shuffle:
            # shuffle requested
            self._shuffle_enabled = True
            if self.cur_index is not None:
                played_items = self.items[: self.cur_index]
                next_items = self.__shuffle_items(self.items[self.cur_index + 1 :])
                items = played_items + [self.cur_item] + next_items
                self.mass.add_job(self.async_update(items))
        elif self._shuffle_enabled and not enable_shuffle:
            # unshuffle
            self._shuffle_enabled = False
            if self.cur_index is not None:
                played_items = self.items[: self.cur_index]
                next_items = self.items[self.cur_index + 1 :]
                next_items.sort(key=lambda x: x.sort_index, reverse=False)
                items = played_items + [self.cur_item] + next_items
                self.mass.add_job(self.async_update(items))
        self.mass.add_job(self.async_update_state())

    @property
    def repeat_enabled(self):
        """Return if crossfade is enabled for this player."""
        return self._repeat_enabled

    @repeat_enabled.setter
    def repeat_enabled(self, enable_repeat: bool):
        """Set the repeat mode for this queue."""
        if self._repeat_enabled != enable_repeat:
            self._repeat_enabled = enable_repeat
            self.mass.add_job(self.async_update_state())
            self.mass.add_job(self.__async_save_state())

    @property
    def crossfade_enabled(self):
        """Return if crossfade is enabled for this player's queue."""
        return (
            self.mass.config.player_settings[self.player_id]["crossfade_duration"] > 0
        )

    @property
    def cur_index(self):
        """
        Return the current index of the queue.

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
        """Return the time (progress) for current (playing) item."""
        return self._cur_item_time

    @property
    def next_index(self):
        """Return the next index for this player's queue.

        Return None if queue is empty or no more items.
        """
        if not self.items:
            # queue is empty
            return None
        if self.cur_index is None:
            # playback started
            return 0
        # player already playing (or paused) so return the next item
        if len(self.items) > (self.cur_index + 1):
            return self.cur_index + 1
        if self._repeat_enabled:
            # repeat enabled, start queue at beginning
            return 0
        return None

    @property
    def next_item(self):
        """Return the next item in the queue.

        Returns None if queue is empty or no more items.
        """
        if self.next_index is not None:
            return self.items[self.next_index]
        return None

    @property
    def items(self):
        """Return all queue items for this player's queue."""
        return self._items

    @property
    def use_queue_stream(self):
        """
        Indicate that we need to use the queue stream.

        For example if crossfading is requested but a player doesn't natively support it
        we will send a constant stream of audio to the player with all tracks.
        """
        supports_crossfade = PlayerFeature.CROSSFADE in self.player.features
        supports_queue = PlayerFeature.QUEUE in self.player.features
        return not supports_crossfade if self.crossfade_enabled else not supports_queue

    @callback
    def get_item(self, index):
        """Get item by index from queue."""
        if index is not None and len(self.items) > index:
            return self.items[index]
        return None

    @callback
    def by_item_id(self, queue_item_id: str):
        """Get item by queue_item_id from queue."""
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
            return await self.async_play_index(self.cur_index + 1)
        await self.mass.player_manager.async_cmd_power_on(self.player_id)
        return await self.mass.player_manager.get_player_provider(
            self.player_id
        ).async_cmd_next(self.player_id)

    async def async_previous(self):
        """Play the previous track in the queue."""
        if self.cur_index is None:
            return
        await self.mass.player_manager.async_cmd_power_on(self.player_id)
        if self.use_queue_stream:
            return await self.async_play_index(self.cur_index - 1)
        return await self.mass.player_manager.async_cmd_previous(self.player_id)

    async def async_resume(self):
        """Resume previous queue."""
        if self.items:
            await self.mass.player_manager.async_cmd_power_on(self.player_id)
            prev_index = self.cur_index
            supports_queue = PlayerFeature.QUEUE in self.player.features
            if self.use_queue_stream or not supports_queue:
                await self.async_play_index(prev_index)
            else:
                # at this point we don't know if the queue is synced with the player
                # so just to be safe we send the queue_items to the player
                player_provider = self.mass.player_manager.get_player_provider(
                    self.player_id
                )
                await player_provider.async_cmd_queue_load(self.player_id, self.items)
                await self.async_play_index(prev_index)
        else:
            LOGGER.warning(
                "resume queue requested for %s but queue is empty", self.player.name
            )

    async def async_play_index(self, index):
        """Play item at index X in queue."""
        await self.mass.player_manager.async_cmd_power_on(self.player_id)
        player_prov = self.mass.player_manager.get_player_provider(self.player_id)
        supports_queue = PlayerFeature.QUEUE in self.player.features
        if not isinstance(index, int):
            index = self.__index_by_id(index)
        if not len(self.items) > index:
            return
        if self.use_queue_stream:
            self._next_queue_startindex = index
            self.player.elapsed_time = 0  # set just in case of a race condition
            queue_stream_uri = "%s/stream/%s?id=%s" % (
                self.mass.web.internal_url,
                self.player.player_id,
                self.items[
                    index
                ].queue_item_id,  # just set to invalidate any cache stuff
            )
            return await player_prov.async_cmd_play_uri(
                self.player_id, queue_stream_uri
            )
        if supports_queue:
            try:
                return await player_prov.async_cmd_queue_play_index(
                    self.player_id, index
                )
            except NotImplementedError:
                # not supported by player, use load queue instead
                LOGGER.debug(
                    "cmd_queue_insert not supported by player, fallback to cmd_queue_load "
                )
                self._items = self._items[index:]
                return await player_prov.async_cmd_queue_load(
                    self.player_id, self._items
                )
        else:
            return await player_prov.async_cmd_play_uri(
                self.player_id, self._items[index].uri
            )

    async def async_move_item(self, queue_item_id, pos_shift=1):
        """
        Move queue item x up/down the queue.

        param pos_shift: move item x positions down if positive value
                         move item x positions up if negative value
                         move item to top of queue as next item
        """
        items = self.items.copy()
        item_index = self.__index_by_id(queue_item_id)
        if pos_shift == 0 and self.player.state == PlayerState.Playing:
            new_index = self.cur_index + 1
        elif pos_shift == 0:
            new_index = self.cur_index
        else:
            new_index = item_index + pos_shift
        if (new_index < self.cur_index) or (new_index > len(self.items)):
            return
        # move the item in the list
        items.insert(new_index, items.pop(item_index))
        await self.async_update(items)
        if pos_shift == 0:
            await self.async_play_index(new_index)

    async def async_load(self, queue_items: List[QueueItem]):
        """Load (overwrite) queue with new items."""
        await self.mass.player_manager.async_cmd_power_on(self.player_id)
        supports_queue = PlayerFeature.QUEUE in self.player.features
        for index, item in enumerate(queue_items):
            item.sort_index = index
        if self._shuffle_enabled:
            queue_items = self.__shuffle_items(queue_items)
        self._items = queue_items
        if self.use_queue_stream or not supports_queue:
            await self.async_play_index(0)
        else:
            player_prov = self.mass.player_manager.get_player_provider(self.player_id)
            await player_prov.async_cmd_queue_load(self.player_id, queue_items)
        self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self.to_dict())
        self.mass.add_job(self.__async_save_state())

    async def async_insert(self, queue_items: List[QueueItem], offset=0):
        """
        Insert new items at offset x from current position.

        Keeps remaining items in queue.
        if offset 0, will start playing newly added item(s)
            :param queue_items: a list of QueueItem
            :param offset: offset from current queue position
        """
        supports_queue = PlayerFeature.QUEUE in self.player.features

        if (
            not self.items
            or self.cur_index is None
            or self.cur_index == 0
            or (self.cur_index + offset > len(self.items))
        ):
            return await self.async_load(queue_items)
        insert_at_index = self.cur_index + offset
        for index, item in enumerate(queue_items):
            item.sort_index = insert_at_index + index
        if self.shuffle_enabled:
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
        if self.use_queue_stream or not supports_queue:
            if offset == 0:
                await self.async_play_index(insert_at_index)
        else:
            # send queue to player's own implementation
            player_prov = self.mass.player_manager.get_player_provider(self.player_id)
            try:
                await player_prov.async_cmd_queue_insert(
                    self.player_id, queue_items, insert_at_index
                )
            except NotImplementedError:
                # not supported by player, use load queue instead
                LOGGER.debug(
                    "cmd_queue_insert not supported by player, fallback to cmd_queue_load "
                )
                self._items = self._items[self.cur_index :]
                return await player_prov.async_cmd_queue_load(
                    self.player_id, self._items
                )
        self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self.to_dict())
        self.mass.add_job(self.__async_save_state())

    async def async_append(self, queue_items: List[QueueItem]):
        """Append new items at the end of the queue."""
        supports_queue = PlayerFeature.QUEUE in self.player.features
        for index, item in enumerate(queue_items):
            item.sort_index = len(self.items) + index
        if self.shuffle_enabled:
            played_items = self.items[: self.cur_index]
            next_items = self.items[self.cur_index + 1 :] + queue_items
            next_items = self.__shuffle_items(next_items)
            items = played_items + [self.cur_item] + next_items
            return await self.async_update(items)
        self._items = self._items + queue_items
        if supports_queue and not self.use_queue_stream:
            # send queue to player's own implementation
            player_prov = self.mass.player_manager.get_player_provider(self.player_id)
            try:
                await player_prov.async_cmd_queue_append(self.player_id, queue_items)
            except NotImplementedError:
                # not supported by player, use load queue instead
                LOGGER.debug(
                    "cmd_queue_append not supported by player, fallback to cmd_queue_load "
                )
                self._items = self._items[self.cur_index :]
                return await player_prov.async_cmd_queue_load(
                    self.player_id, self._items
                )
        self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self.to_dict())
        self.mass.add_job(self.__async_save_state())

    async def async_update(self, queue_items: List[QueueItem]):
        """Update the existing queue items, mostly caused by reordering."""
        supports_queue = PlayerFeature.QUEUE in self.player.features
        self._items = queue_items
        if supports_queue and not self.use_queue_stream:
            # send queue to player's own implementation
            player_prov = self.mass.player_manager.get_player_provider(self.player_id)
            try:
                await player_prov.async_cmd_queue_update(self.player_id, queue_items)
            except NotImplementedError:
                # not supported by player, use load queue instead
                LOGGER.debug(
                    "cmd_queue_update not supported by player, fallback to cmd_queue_load "
                )
                self._items = self._items[self.cur_index :]
                return await player_prov.async_cmd_queue_load(
                    self.player_id, self._items
                )
        self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self.to_dict())
        self.mass.add_job(self.__async_save_state())

    async def async_clear(self):
        """Clear all items in the queue."""
        supports_queue = PlayerFeature.QUEUE in self.player.features
        await self.mass.player_manager.async_cmd_stop(self.player_id)
        self._items = []
        if supports_queue:
            # send queue cmd to player's own implementation
            player_prov = self.mass.player_manager.get_player_provider(self.player_id)
            try:
                await player_prov.async_cmd_queue_clear(self.player_id)
            except NotImplementedError:
                # not supported by player, try update instead
                try:
                    await player_prov.async_cmd_queue_update(self.player_id, [])
                except NotImplementedError:
                    # not supported by player, ignore
                    pass
        self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self.to_dict())

    async def async_update_state(self):
        """Update queue details, called when player updates."""
        new_index = self._cur_index
        track_time = self._cur_item_time
        # handle queue stream
        if (
            self.use_queue_stream
            and self.player.state == PlayerState.Playing
            and self.player.elapsed_time > 1
        ):
            new_index, track_time = self.__get_queue_stream_index()
        # normal queue based approach
        elif not self.use_queue_stream:
            track_time = self.player.elapsed_time
            for index, queue_item in enumerate(self.items):
                if queue_item.uri == self.player.current_uri:
                    new_index = index
                    break
        # process new index
        if self._cur_index != new_index:
            # queue track updated
            self._next_queue_startindex = self.next_index
            self._cur_index = new_index
        # check if a new track is loaded, wait for the streamdetails
        if (
            self.cur_item
            and self._last_item != self.cur_item
            and self.cur_item.streamdetails
        ):
            # new active item in queue
            self.mass.signal_event(EVENT_QUEUE_UPDATED, self.to_dict())
            # invalidate previous streamdetails
            if self._last_item:
                self._last_item.streamdetails = None
            self._last_item = self.cur_item
        # update vars
        if self._cur_item_time != track_time:
            self._cur_item_time = track_time
            self.mass.signal_event(
                EVENT_QUEUE_TIME_UPDATED,
                {"player_id": self.player_id, "cur_item_time": track_time},
            )

    async def async_start_queue_stream(self):
        """Call when queue_streamer starts playing the queue stream."""
        self._last_queue_startindex = self._next_queue_startindex

        self._cur_item_time = 0
        return self.get_item(self._next_queue_startindex)

    def to_dict(self):
        """Instance attributes as dict so it can be serialized to json."""
        return {
            "player_id": self.player.player_id,
            "shuffle_enabled": self.shuffle_enabled,
            "repeat_enabled": self.repeat_enabled,
            "crossfade_enabled": self.crossfade_enabled,
            "items": len(self._items),
            "cur_item_id": self.cur_item_id,
            "cur_index": self.cur_index,
            "next_index": self.next_index,
            "cur_item": self.cur_item,
            "cur_item_time": self.cur_item_time,
            "next_item": self.next_item,
            "queue_stream_enabled": self.use_queue_stream,
        }

    @callback
    def __get_queue_stream_index(self):
        """Get index of queue stream."""
        # player is playing a constant stream of the queue so we need to do this the hard way
        queue_index = 0
        elapsed_time_queue = self.player.elapsed_time
        total_time = 0
        track_time = 0
        if self.items and len(self.items) > self._last_queue_startindex:
            queue_index = (
                self._last_queue_startindex
            )  # holds the last starting position
            queue_track = None
            while len(self.items) > queue_index:
                queue_track = self.items[queue_index]
                if elapsed_time_queue > (queue_track.duration + total_time):
                    total_time += queue_track.duration
                    queue_index += 1
                else:
                    track_time = elapsed_time_queue - total_time
                    break
        return queue_index, track_time

    @staticmethod
    def __shuffle_items(queue_items):
        """Shuffle a list of tracks."""
        # for now we use default python random function
        # can be extended with some more magic last_played and stuff
        return random.sample(queue_items, len(queue_items))

    def __index_by_id(self, queue_item_id):
        """Get index by queue_item_id."""
        item_index = None
        for index, item in enumerate(self.items):
            if item.queue_item_id == queue_item_id:
                item_index = index
        return item_index

    async def __async_restore_saved_state(self):
        """Try to load the saved queue for this player from cache file."""
        cache_str = "queue_state_%s" % self.player.player_id
        cache_data = await self.mass.cache.async_get(cache_str)
        if cache_data:
            self._shuffle_enabled = cache_data["shuffle_enabled"]
            self._repeat_enabled = cache_data["repeat_enabled"]
            self._items = cache_data["items"]
            self._cur_index = cache_data["cur_item"]
            self._next_queue_startindex = cache_data["next_queue_index"]

    # pylint: enable=unused-argument

    async def __async_save_state(self):
        """Save current queue settings to file."""
        cache_str = "queue_state_%s" % self.player_id
        cache_data = {
            "shuffle_enabled": self._shuffle_enabled,
            "repeat_enabled": self._repeat_enabled,
            "items": self._items,
            "cur_item": self._cur_index,
            "next_queue_index": self._next_queue_startindex,
        }
        await self.mass.cache.async_set(cache_str, cache_data)
        LOGGER.info("queue state saved to file for player %s", self.player_id)
