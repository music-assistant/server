"""Models and helpers for a player queue."""

import logging
import random
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

from music_assistant.constants import (
    CONF_CROSSFADE_DURATION,
    EVENT_QUEUE_ITEMS_UPDATED,
    EVENT_QUEUE_TIME_UPDATED,
    EVENT_QUEUE_UPDATED,
)
from music_assistant.helpers.typing import (
    MusicAssistant,
    OptionalInt,
    OptionalStr,
    Player,
)
from music_assistant.helpers.util import callback
from music_assistant.models.media_types import Radio, Track
from music_assistant.models.player import PlaybackState, PlayerFeature
from music_assistant.models.streamdetails import StreamDetails

# pylint: disable=too-many-instance-attributes
# pylint: disable=too-many-public-methods
# pylint: disable=too-few-public-methods

LOGGER = logging.getLogger("player_queue")


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

    def __post_init__(self):
        """Generate unique id for the QueueItem."""
        self.queue_item_id = str(uuid.uuid4())

    @classmethod
    def from_track(cls, track: Union[Track, Radio]):
        """Construct QueueItem from track/radio item."""
        return cls.from_dict(track.to_dict())


class PlayerQueue:
    """Class that holds the queue items for a player."""

    def __init__(self, mass: MusicAssistant, player_id: str) -> None:
        """Initialize class."""
        self.mass = mass
        self._queue_id = player_id
        self._items = []
        self._shuffle_enabled = False
        self._repeat_enabled = False
        self._cur_index = 0
        self._cur_item_time = 0
        self._last_item = None
        self._queue_stream_start_index = 0
        self._queue_stream_next_index = 0
        self._last_player = PlaybackState.Stopped
        # load previous queue settings from disk
        self.mass.add_job(self._restore_saved_state())

    async def close(self) -> None:
        """Handle shutdown/close."""
        # pylint: disable=unused-argument
        await self._save_state()

    @property
    def player(self) -> Player:
        """Return handle to (master) player of this queue."""
        return self.mass.players.get_player(self._queue_id)

    @property
    def queue_id(self) -> str:
        """Return the Queue's id."""
        return self._queue_id

    def get_stream_url(self) -> str:
        """Return the full stream url for the player's Queue Stream."""
        uri = f"{self.mass.web.stream_url}/queue/{self.queue_id}"
        # we set the checksum just to invalidate cache stuf
        uri += f"?checksum={time.time()}"
        return uri

    @property
    def shuffle_enabled(self) -> bool:
        """Return shuffle enabled property."""
        return self._shuffle_enabled

    async def set_shuffle_enabled(self, enable_shuffle: bool) -> None:
        """Set shuffle."""
        if not self._shuffle_enabled and enable_shuffle:
            # shuffle requested
            self._shuffle_enabled = True
            if self.cur_index is not None:
                played_items = self.items[: self.cur_index]
                next_items = self.__shuffle_items(self.items[self.cur_index + 1 :])
                items = played_items + [self.cur_item] + next_items
                self.mass.add_job(self.update(items))
        elif self._shuffle_enabled and not enable_shuffle:
            # unshuffle
            self._shuffle_enabled = False
            if self.cur_index is not None:
                played_items = self.items[: self.cur_index]
                next_items = self.items[self.cur_index + 1 :]
                next_items.sort(key=lambda x: x.sort_index, reverse=False)
                items = played_items + [self.cur_item] + next_items
                self.mass.add_job(self.update(items))
        self.update_state()
        self.mass.signal_event(EVENT_QUEUE_UPDATED, self)

    @property
    def repeat_enabled(self) -> bool:
        """Return if crossfade is enabled for this player."""
        return self._repeat_enabled

    async def set_repeat_enabled(self, enable_repeat: bool) -> None:
        """Set the repeat mode for this queue."""
        if self._repeat_enabled != enable_repeat:
            self._repeat_enabled = enable_repeat
            self.update_state()
            self.mass.add_job(self._save_state())
            self.mass.signal_event(EVENT_QUEUE_UPDATED, self)

    @property
    def cur_index(self) -> OptionalInt:
        """
        Return the current index of the queue.

        Returns None if queue is empty.
        """
        if not self._items:
            return None
        return self._cur_index

    @property
    def cur_item_id(self) -> OptionalStr:
        """
        Return the queue item id of the current item in the queue.

        Returns None if queue is empty.
        """
        cur_item = self.cur_item
        if not cur_item:
            return None
        return cur_item.queue_item_id

    @property
    def cur_item(self) -> Optional[QueueItem]:
        """
        Return the current item in the queue.

        Returns None if queue is empty.
        """
        if (
            self.cur_index is None
            or not self.items
            or not len(self.items) > self.cur_index
        ):
            return None
        return self.items[self.cur_index]

    @property
    def cur_item_time(self) -> int:
        """Return the time (progress) for current (playing) item."""
        return self._cur_item_time

    @property
    def next_index(self) -> OptionalInt:
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
    def next_item(self) -> Optional[QueueItem]:
        """Return the next item in the queue.

        Returns None if queue is empty or no more items.
        """
        if self.next_index is not None:
            return self.items[self.next_index]
        return None

    @property
    def items(self) -> List[QueueItem]:
        """Return all queue items for this player's queue."""
        return self._items

    @property
    def use_queue_stream(self) -> bool:
        """
        Indicate that we need to use the queue stream.

        For example if crossfading is requested but a player doesn't natively support it
        we will send a constant stream of audio to the player with all tracks.
        """
        return (
            not self.supports_crossfade
            if self.crossfade_enabled
            else not self.supports_queue
        )

    @property
    def crossfade_duration(self) -> int:
        """Return crossfade duration (if enabled)."""
        player_settings = self.mass.config.get_player_config(self.queue_id)
        if player_settings:
            return player_settings.get(CONF_CROSSFADE_DURATION, 0)
        return 0

    @property
    def crossfade_enabled(self) -> bool:
        """Return bool if crossfade is enabled."""
        return self.crossfade_duration > 0

    @property
    def supports_queue(self) -> bool:
        """Return if this player supports native queue."""
        return PlayerFeature.QUEUE in self.player.features

    @property
    def supports_crossfade(self) -> bool:
        """Return if this player supports native crossfade."""
        return PlayerFeature.CROSSFADE in self.player.features

    @callback
    def get_item(self, index: int) -> Optional[QueueItem]:
        """Get item by index from queue."""
        if index is not None and len(self.items) > index:
            return self.items[index]
        return None

    @callback
    def by_item_id(self, queue_item_id: str) -> Optional[QueueItem]:
        """Get item by queue_item_id from queue."""
        if not queue_item_id:
            return None
        for item in self.items:
            if item.queue_item_id == queue_item_id:
                return item
        return None

    async def next(self) -> None:
        """Play the next track in the queue."""
        if self.cur_index is None:
            return
        if self.use_queue_stream:
            return await self.play_index(self.cur_index + 1)
        return await self.player.cmd_next()

    async def previous(self) -> None:
        """Play the previous track in the queue."""
        if self.cur_index is None:
            return
        if self.use_queue_stream:
            return await self.play_index(self.cur_index - 1)
        return await self.player.cmd_previous()

    async def resume(self) -> None:
        """Resume previous queue."""
        if self.items:
            prev_index = self.cur_index
            if self.use_queue_stream or not self.supports_queue:
                await self.play_index(prev_index)
            else:
                # at this point we don't know if the queue is synced with the player
                # so just to be safe we send the queue_items to the player
                self._items = self._items[prev_index:]
                return await self.player.cmd_queue_load(self._items)
        else:
            LOGGER.warning(
                "resume queue requested for %s but queue is empty", self.queue_id
            )

    async def play_index(self, index: Union[int, str]) -> None:
        """Play item at index (or item_id) X in queue."""
        if not isinstance(index, int):
            index = self.__index_by_id(index)
        if not len(self.items) > index:
            return
        self._cur_index = index
        self._queue_stream_next_index = index
        if self.use_queue_stream:
            queue_stream_uri = self.get_stream_url()
            return await self.player.cmd_play_uri(queue_stream_uri)
        if self.supports_queue:
            try:
                return await self.player.cmd_queue_play_index(index)
            except NotImplementedError:
                # not supported by player, use load queue instead
                LOGGER.debug(
                    "cmd_queue_insert not supported by player, fallback to cmd_queue_load "
                )
                self._items = self._items[index:]
                return await self.player.cmd_queue_load(self._items)
        else:
            return await self.player.cmd_play_uri(self._items[index].uri)

    async def move_item(self, queue_item_id: str, pos_shift: int = 1) -> None:
        """
        Move queue item x up/down the queue.

        param pos_shift: move item x positions down if positive value
                         move item x positions up if negative value
                         move item to top of queue as next item if 0
        """
        items = self.items.copy()
        item_index = self.__index_by_id(queue_item_id)
        if pos_shift == 0 and self.player.state == PlaybackState.Playing:
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

    async def load(self, queue_items: List[QueueItem]) -> None:
        """Load (overwrite) queue with new items."""
        for index, item in enumerate(queue_items):
            item.sort_index = index
        if self._shuffle_enabled:
            queue_items = self.__shuffle_items(queue_items)
        self._items = queue_items
        if self.use_queue_stream or not self.supports_queue:
            await self.play_index(0)
        else:
            await self.player.cmd_queue_load(queue_items)
        self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self)
        self.mass.add_job(self._save_state())

    async def insert(self, queue_items: List[QueueItem], offset: int = 0) -> None:
        """
        Insert new items at offset x from current position.

        Keeps remaining items in queue.
        if offset 0, will start playing newly added item(s)
            :param queue_items: a list of QueueItem
            :param offset: offset from current queue position
        """

        if not self.items or self.cur_index is None:
            return await self.load(queue_items)
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
        if self.use_queue_stream:
            if offset == 0:
                await self.play_index(insert_at_index)
        else:
            # send queue to player's own implementation
            try:
                await self.player.cmd_queue_insert(queue_items, insert_at_index)
            except NotImplementedError:
                # not supported by player, use load queue instead
                LOGGER.debug(
                    "cmd_queue_insert not supported by player, fallback to cmd_queue_load "
                )
                self._items = self._items[self.cur_index + offset :]
                return await self.player.cmd_queue_load(self._items)
        self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self)
        self.mass.add_job(self._save_state())

    async def append(self, queue_items: List[QueueItem]) -> None:
        """Append new items at the end of the queue."""
        for index, item in enumerate(queue_items):
            item.sort_index = len(self.items) + index
        if self.shuffle_enabled:
            played_items = self.items[: self.cur_index]
            next_items = self.items[self.cur_index + 1 :] + queue_items
            next_items = self.__shuffle_items(next_items)
            items = played_items + [self.cur_item] + next_items
            return await self.update(items)
        self._items = self._items + queue_items
        if self.supports_queue and not self.use_queue_stream:
            # send queue to player's own implementation
            try:
                await self.player.cmd_queue_append(queue_items)
            except NotImplementedError:
                # not supported by player, use load queue instead
                LOGGER.debug(
                    "cmd_queue_append not supported by player, fallback to cmd_queue_load "
                )
                self._items = self._items[self.cur_index :]
                return await self.player.cmd_queue_load(self._items)
        self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self)
        self.mass.add_job(self._save_state())

    async def update(self, queue_items: List[QueueItem]) -> None:
        """Update the existing queue items, mostly caused by reordering."""
        self._items = queue_items
        if self.supports_queue and not self.use_queue_stream:
            # send queue to player's own implementation
            try:
                await self.player.cmd_queue_update(queue_items)
            except NotImplementedError:
                # not supported by player, use load queue instead
                LOGGER.debug(
                    "cmd_queue_update not supported by player, fallback to cmd_queue_load "
                )
                self._items = self._items[self.cur_index :]
                await self.player.cmd_queue_load(self._items)
        self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self)
        self.mass.add_job(self._save_state())

    async def clear(self) -> None:
        """Clear all items in the queue."""
        await self.mass.players.cmd_stop(self.queue_id)
        self._items = []
        if self.supports_queue:
            # send queue cmd to player's own implementation
            try:
                await self.player.cmd_queue_clear()
            except NotImplementedError:
                # not supported by player, try update instead
                try:
                    await self.player.cmd_queue_update([])
                except NotImplementedError:
                    # not supported by player, ignore
                    pass
        self.mass.signal_event(EVENT_QUEUE_ITEMS_UPDATED, self)

    @callback
    def update_state(self) -> None:
        """Update queue details, called when player updates."""
        new_index = self._cur_index
        track_time = self._cur_item_time
        # handle queue stream
        if (
            self.use_queue_stream
            and self.player.state == PlaybackState.Playing
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
            self._cur_index = new_index
        # check if a new track is loaded, wait for the streamdetails
        if (
            self.cur_item
            and self._last_item != self.cur_item
            and self.cur_item.streamdetails
        ):
            # new active item in queue
            self.mass.signal_event(EVENT_QUEUE_UPDATED, self)
            # invalidate previous streamdetails
            if self._last_item:
                self._last_item.streamdetails = None
            self._last_item = self.cur_item
        # update vars
        if self._cur_item_time != track_time:
            self._cur_item_time = track_time
            self.mass.signal_event(
                EVENT_QUEUE_TIME_UPDATED,
                {"queue_id": self.queue_id, "cur_item_time": track_time},
            )

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

    def to_dict(self) -> Dict[str, Any]:
        """Instance attributes as dict so it can be serialized to json."""
        return {
            "queue_id": self.player.player_id,
            "queue_name": self.player.player_state.name,
            "shuffle_enabled": self.shuffle_enabled,
            "repeat_enabled": self.repeat_enabled,
            "crossfade_enabled": self.crossfade_enabled,
            "items": len(self._items),
            "cur_item_id": self.cur_item_id,
            "cur_index": self.cur_index,
            "next_index": self.next_index,
            "cur_item": self.cur_item.to_dict() if self.cur_item else None,
            "cur_item_time": self.cur_item_time,
            "next_item": self.next_item.to_dict() if self.next_item else None,
            "queue_stream_enabled": self.use_queue_stream,
        }

    @callback
    def __get_queue_stream_index(self) -> Tuple[int, int]:
        """Get index of queue stream."""
        # player is playing a constant stream of the queue so we need to do this the hard way
        queue_index = 0
        elapsed_time_queue = self.player.elapsed_time
        total_time = 0
        track_time = 0
        if self.items and len(self.items) > self._queue_stream_start_index:
            queue_index = (
                self._queue_stream_start_index
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
    def __shuffle_items(queue_items) -> List[QueueItem]:
        """Shuffle a list of tracks."""
        # for now we use default python random function
        # can be extended with some more magic last_played and stuff
        return random.sample(queue_items, len(queue_items))

    def __index_by_id(self, queue_item_id) -> OptionalInt:
        """Get index by queue_item_id."""
        item_index = None
        for index, item in enumerate(self.items):
            if item.queue_item_id == queue_item_id:
                item_index = index
        return item_index

    async def _restore_saved_state(self) -> None:
        """Try to load the saved queue for this player from cache file."""
        cache_str = "queue_state_%s" % self.queue_id
        cache_data = await self.mass.cache.get(cache_str)
        if cache_data:
            self._shuffle_enabled = cache_data["shuffle_enabled"]
            self._repeat_enabled = cache_data["repeat_enabled"]
            self._items = cache_data["items"]
            self._cur_index = cache_data.get("cur_index", 0)
            self._queue_stream_next_index = self._cur_index

    # pylint: enable=unused-argument

    async def _save_state(self) -> None:
        """Save current queue settings to file."""
        cache_str = "queue_state_%s" % self.queue_id
        cache_data = {
            "shuffle_enabled": self._shuffle_enabled,
            "repeat_enabled": self._repeat_enabled,
            "items": self._items,
            "cur_index": self._cur_index,
        }
        await self.mass.cache.set(cache_str, cache_data)
        LOGGER.info("queue state saved to file for player %s", self.queue_id)
