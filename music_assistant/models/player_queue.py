"""Model and helpders for a PlayerQueue."""
from __future__ import annotations

import asyncio
import random
import time
from asyncio import Task, TimerHandle
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, List, Optional, Tuple, Union
from uuid import uuid4

from mashumaro import DataClassDictMixin
from music_assistant.constants import EventType
from music_assistant.helpers.audio import get_stream_details
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import create_task
from music_assistant.models.errors import MediaNotFoundError, QueueEmpty
from music_assistant.models.media_items import MediaType, StreamDetails

from .player import Player, PlayerState

if TYPE_CHECKING:
    from music_assistant.models.media_items import Radio, Track


class QueueOption(Enum):
    """Enum representation of the queue (play) options."""

    PLAY = "play"
    REPLACE = "replace"
    NEXT = "next"
    ADD = "add"


@dataclass
class QueueItem(DataClassDictMixin):
    """Representation of a queue item."""

    uri: str
    name: str = ""
    duration: Optional[int] = None
    item_id: str = ""
    sort_index: int = 0
    streamdetails: Optional[StreamDetails] = None
    is_media_item: bool = False

    def __post_init__(self):
        """Set default values."""
        if not self.item_id:
            self.item_id = str(uuid4())
        if not self.name:
            self.name = self.uri

    @classmethod
    def from_media_item(cls, media_item: "Track" | "Radio"):
        """Construct QueueItem from track/radio item."""
        return cls(
            uri=media_item.uri,
            name=media_item.name,
            duration=media_item.duration,
            is_media_item=True,
        )


class PlayerQueue:
    """Represents a PlayerQueue object."""

    def __init__(self, mass: MusicAssistant, player_id: str):
        """Instantiate a PlayerQueue instance."""
        self.mass = mass
        self.logger = mass.players.logger
        self.queue_id = player_id

        self._shuffle_enabled: bool = False
        self._repeat_enabled: bool = False
        self._crossfade_duration: int = 0
        self._volume_normalization_enabled: bool = True
        self._volume_normalization_target: int = -23

        self._current_index: Optional[int] = None
        self._current_item_time: int = 0
        self._last_item: Optional[QueueItem] = None
        self._start_index: int = 0  # from which index did the queue start playing
        self._next_start_index: int = 0  # which index should the stream start
        self._last_state = PlayerState.IDLE
        self._items: List[QueueItem] = []
        self._save_task: TimerHandle = None
        self._update_task: Task = None
        self._signal_next: bool = False
        self._last_player_update: int = 0
        self._stream_url: Optional[str] = None

    async def setup(self) -> None:
        """Handle async setup of instance."""
        await self._restore_saved_state()
        self.mass.signal_event(EventType.QUEUE_ADDED, self)

    @property
    def player(self) -> Player:
        """Return the player attached to this queue."""
        return self.mass.players.get_player(self.queue_id, include_unavailable=True)

    @property
    def active(self) -> bool:
        """Return bool if the queue is currenty active on the player."""
        if self.player.current_url is None:
            return False
        return self._stream_url in self.player.current_url

    @property
    def elapsed_time(self) -> int:
        """Return elapsed time of current playing media in seconds."""
        if not self.active:
            return self.player.elapsed_time
        return self._current_item_time

    @property
    def repeat_enabled(self) -> bool:
        """Return if repeat is enabled."""
        return self._repeat_enabled

    @property
    def shuffle_enabled(self) -> bool:
        """Return if shuffle is enabled."""
        return self._shuffle_enabled

    @property
    def crossfade_duration(self) -> int:
        """Return crossfade duration (0 if disabled)."""
        return self._crossfade_duration

    @property
    def max_sample_rate(self) -> int:
        """Return the maximum supported sample rate this playerqueue supports."""
        if self.player.max_sample_rate is None:
            return 96000
        return self.player.max_sample_rate

    @property
    def items(self) -> List[QueueItem]:
        """Return all items in this queue."""
        return self._items

    @property
    def current_item(self) -> QueueItem | None:
        """
        Return the current item in the queue.

        Returns None if queue is empty.
        """
        if self._current_index is None:
            return None
        if self._current_index >= len(self._items):
            return None
        return self._items[self._current_index]

    @property
    def next_item(self) -> QueueItem | None:
        """
        Return the next item in the queue.

        Returns None if queue is empty or no more items.
        """
        if next_index := self.get_next_index(self._current_index):
            return self._items[next_index]
        return None

    @property
    def volume_normalization_enabled(self) -> bool:
        """Return bool if volume normalization is enabled for this queue."""
        return self._volume_normalization_enabled

    @property
    def volume_normalization_target(self) -> int:
        """Return volume target (in LUFS) for volume normalization for this queue."""
        return self._volume_normalization_target

    def get_item(self, index: int) -> QueueItem | None:
        """Get queue item by index."""
        if index is not None and len(self._items) > index:
            return self._items[index]
        return None

    def item_by_id(self, queue_item_id: str) -> QueueItem | None:
        """Get item by queue_item_id from queue."""
        if not queue_item_id:
            return None
        return next((x for x in self.items if x.item_id == queue_item_id), None)

    def index_by_id(self, queue_item_id: str) -> Optional[int]:
        """Get index by queue_item_id."""
        for index, item in enumerate(self.items):
            if item.item_id == queue_item_id:
                return index
        return None

    async def play_media(
        self,
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
            uris = [uris]
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
                tracks = await self.mass.music.artists.toptracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            elif media_item.media_type == MediaType.ALBUM:
                tracks = await self.mass.music.albums.tracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            elif media_item.media_type == MediaType.PLAYLIST:
                tracks = await self.mass.music.playlists.tracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            elif media_item.media_type == MediaType.RADIO:
                # single radio
                tracks = [
                    await self.mass.music.radio.get(
                        media_item.item_id, provider_id=media_item.provider
                    )
                ]
            else:
                # single track
                tracks = [
                    await self.mass.music.tracks.get(
                        media_item.item_id, provider_id=media_item.provider
                    )
                ]
            for track in tracks:
                if not track.available:
                    continue
                queue_items.append(QueueItem.from_media_item(track))

        # load items into the queue
        if queue_opt == QueueOption.REPLACE:
            return await self.load(queue_items)
        if queue_opt in [QueueOption.PLAY, QueueOption.NEXT] and len(queue_items) > 100:
            return await self.load(queue_items)
        if queue_opt == QueueOption.NEXT:
            return await self.insert(queue_items, 1)
        if queue_opt == QueueOption.PLAY:
            return await self.insert(queue_items, 0)
        if queue_opt == QueueOption.ADD:
            return await self.append(queue_items)

    async def set_shuffle_enabled(self, enable_shuffle: bool) -> None:
        """Set shuffle."""
        if not self._shuffle_enabled and enable_shuffle:
            # shuffle requested
            self._shuffle_enabled = True
            if self._current_index is not None:
                played_items = self.items[: self._current_index]
                next_items = self.__shuffle_items(self.items[self._current_index + 1 :])
                items = played_items + [self.current_item] + next_items
                self.mass.signal_event(EventType.QUEUE_UPDATED, self)
                await self.update(items)
        elif self._shuffle_enabled and not enable_shuffle:
            # unshuffle
            self._shuffle_enabled = False
            if self._current_index is not None:
                played_items = self.items[: self._current_index]
                next_items = self.items[self._current_index + 1 :]
                next_items.sort(key=lambda x: x.sort_index, reverse=False)
                items = played_items + [self.current_item] + next_items
                self.mass.signal_event(EventType.QUEUE_UPDATED, self)
                await self.update(items)

    async def set_repeat_enabled(self, enable_repeat: bool) -> None:
        """Set the repeat mode for this queue."""
        if self._repeat_enabled != enable_repeat:
            self._repeat_enabled = enable_repeat
            self.mass.signal_event(EventType.QUEUE_UPDATED, self)
            await self._save_state()

    async def set_crossfade_duration(self, duration: int) -> None:
        """Set the crossfade duration for this queue, 0 to disable."""
        duration = max(duration, 10)
        if self._crossfade_duration != duration:
            self._crossfade_duration = duration
            self.mass.signal_event(EventType.QUEUE_UPDATED, self)
            await self._save_state()

    async def set_volume_normalization_enabled(self, enable: bool) -> None:
        """Set volume normalization."""
        if self._repeat_enabled != enable:
            self._repeat_enabled = enable
            self.mass.signal_event(EventType.QUEUE_UPDATED, self)
            await self._save_state()

    async def set_volume_normalization_target(self, target: int) -> None:
        """Set the target for the volume normalization in LUFS (default is -23)."""
        target = min(target, 0)
        target = max(target, -40)
        if self._volume_normalization_target != target:
            self._volume_normalization_target = target
            self.mass.signal_event(EventType.QUEUE_UPDATED, self)
            await self._save_state()

    async def stop(self) -> None:
        """Stop command on queue player."""
        # redirect to underlying player
        await self.player.stop()

    async def play(self) -> None:
        """Play (unpause) command on queue player."""
        if self.player.state == PlayerState.PAUSED:
            await self.player.play()
        else:
            await self.resume()

    async def pause(self) -> None:
        """Pause command on queue player."""
        # redirect to underlying player
        await self.player.pause()

    async def next(self) -> None:
        """Play the next track in the queue."""
        next_index = self.get_next_index(self._current_index)
        if next_index is None:
            return None
        await self.play_index(next_index)

    async def previous(self) -> None:
        """Play the previous track in the queue."""
        if self._current_index is None:
            return
        await self.play_index(max(self._current_index - 1, 0))

    async def resume(self) -> None:
        """Resume previous queue."""
        # TODO: Support skipping to last known position
        if self._items:
            prev_index = self._current_index
            await self.play_index(prev_index)
        else:
            self.logger.warning(
                "resume queue requested for %s but queue is empty", self.queue_id
            )

    async def play_index(self, index: Union[int, str]) -> None:
        """Play item at index (or item_id) X in queue."""
        if not isinstance(index, int):
            index = self.index_by_id(index)
        if index is None:
            raise FileNotFoundError(f"Unknown index/id: {index}")
        if not len(self.items) > index:
            return
        self._current_index = index
        self._next_start_index = index

        # send stream url to player connected to this queue
        self._stream_url = self.mass.players.streams.get_stream_url(self.queue_id)
        await self.player.play_url(self._stream_url)

    async def move_item(self, queue_item_id: str, pos_shift: int = 1) -> None:
        """
        Move queue item x up/down the queue.

        param pos_shift: move item x positions down if positive value
                         move item x positions up if negative value
                         move item to top of queue as next item if 0
        """
        items = self._items.copy()
        item_index = self.index_by_id(queue_item_id)
        if pos_shift == 0 and self.player.state == PlayerState.PLAYING:
            new_index = self._current_index + 1
        elif pos_shift == 0:
            new_index = self._current_index
        else:
            new_index = item_index + pos_shift
        if (new_index < self._current_index) or (new_index > len(self.items)):
            return
        # move the item in the list
        items.insert(new_index, items.pop(item_index))
        await self.update(items)

    async def load(self, queue_items: List[QueueItem]) -> None:
        """Load (overwrite) queue with new items."""
        for index, item in enumerate(queue_items):
            item.sort_index = index
        if self._shuffle_enabled and len(queue_items) > 5:
            queue_items = self.__shuffle_items(queue_items)
        self._items = queue_items
        self.mass.signal_event(EventType.QUEUE_ITEMS_UPDATED, self)
        await self.play_index(0)
        await self._save_state()

    async def insert(self, queue_items: List[QueueItem], offset: int = 0) -> None:
        """
        Insert new items at offset x from current position.

        Keeps remaining items in queue.
        if offset 0, will start playing newly added item(s)
            :param queue_items: a list of QueueItem
            :param offset: offset from current queue position
        """
        if not self.items or self._current_index is None:
            return await self.load(queue_items)
        insert_at_index = self._current_index + offset
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

        self.mass.signal_event(EventType.QUEUE_ITEMS_UPDATED, self)
        await self._save_state()

    async def append(self, queue_items: List[QueueItem]) -> None:
        """Append new items at the end of the queue."""
        for index, item in enumerate(queue_items):
            item.sort_index = len(self.items) + index
        if self.shuffle_enabled:
            played_items = self.items[: self._current_index]
            next_items = self.items[self._current_index + 1 :] + queue_items
            next_items = self.__shuffle_items(next_items)
            items = played_items + [self.current_item] + next_items
            await self.update(items)
            return
        self._items = self._items + queue_items
        self.mass.signal_event(EventType.QUEUE_ITEMS_UPDATED, self)
        await self._save_state()

    async def update(self, queue_items: List[QueueItem]) -> None:
        """Update the existing queue items, mostly caused by reordering."""
        self._items = queue_items
        self.mass.signal_event(EventType.QUEUE_ITEMS_UPDATED, self)
        await self._save_state()

    async def clear(self) -> None:
        """Clear all items in the queue."""
        await self.stop()
        await self.update([])

    def on_player_update(self) -> None:
        """Call when player updates."""
        self._last_player_update = time.time()
        if self._last_state != self.player.state:
            self._last_state = self.player.state
            # handle case where stream stopped on purpose and we need to restart it
            if self.player.state != PlayerState.PLAYING and self._signal_next:
                self._signal_next = False
                create_task(self.play())
            # start updater task if needed
            if self.player.state == PlayerState.PLAYING:
                if not self._update_task:
                    self._update_task = create_task(self.__update_task())
            else:
                if self._update_task:
                    self._update_task.cancel()
                self._update_task = None

        if not self.update_state():
            # fire event anyway when player updated.
            self.mass.signal_event(EventType.QUEUE_UPDATED, self)

    def update_state(self) -> bool:
        """Update queue details, called when player updates."""
        new_index = self._current_index
        track_time = self._current_item_time
        new_item_loaded = False
        # if self.player.state == PlayerState.PLAYING and self.elapsed_time > 1:
        if self.player.state == PlayerState.PLAYING:
            new_index, track_time = self.__get_queue_stream_index()
        # process new index
        if self._current_index != new_index:
            # queue track updated
            self._current_index = new_index
        # check if a new track is loaded, wait for the streamdetails
        if (
            self.current_item
            and self._last_item != self.current_item
            and self.current_item.streamdetails
        ):
            # new active item in queue
            new_item_loaded = True
            # invalidate previous streamdetails
            if self._last_item:
                self._last_item.streamdetails = None
            self._last_item = self.current_item
        # update vars and signal update on eventbus if needed
        prev_item_time = int(self._current_item_time)
        self._current_item_time = int(track_time)
        if new_item_loaded or abs(prev_item_time - self._current_item_time) >= 1:
            self.mass.signal_event(EventType.QUEUE_UPDATED, self)
            return True
        return False

    async def queue_stream_prepare(self) -> StreamDetails:
        """Call when queue_streamer is about to start playing."""
        start_from_index = self._next_start_index
        try:
            next_item = self._items[start_from_index]
        except IndexError as err:
            raise QueueEmpty() from err
        try:
            return await get_stream_details(self.mass, next_item, self.queue_id)
        except MediaNotFoundError as err:
            # something bad happened, try to recover by requesting the next track in the queue
            await self.play_index(self._current_index + 2)
            raise err

    async def queue_stream_start(self) -> int:
        """Call when queue_streamer starts playing the queue stream."""
        start_from_index = self._next_start_index
        self._current_item_time = 0
        self._current_index = start_from_index
        self._start_index = start_from_index
        self._next_start_index = self.get_next_index(start_from_index)
        return start_from_index

    async def queue_stream_next(self, cur_index: int) -> int | None:
        """Call when queue_streamer loads next track in buffer."""
        next_idx = self._next_start_index
        self._next_start_index = self.get_next_index(self._next_start_index)
        return next_idx

    def get_next_index(self, index: int) -> int | None:
        """Return the next index or None if no more items."""
        if not self._items:
            # queue is empty
            return None
        if index is None:
            # guard just in case
            return 0
        if len(self._items) > (index + 1):
            return index + 1
        if self.repeat_enabled:
            # repeat enabled, start queue at beginning
            return 0
        return None

    async def queue_stream_signal_next(self):
        """Indicate that queue stream needs to start next index once playback finished."""
        self._signal_next = True

    async def __update_task(self) -> None:
        """Update player queue every interval."""
        while True:
            self.update_state()
            await asyncio.sleep(1)

    def __get_total_elapsed_time(self) -> int:
        """Calculate the total elapsed time of the queue(player)."""
        if self.player.state == PlayerState.PLAYING:
            time_diff = time.time() - self._last_player_update
            return int(self.player.elapsed_time + time_diff)
        if self.player.state == PlayerState.PAUSED:
            return self.player.elapsed_time
        return 0

    def __get_queue_stream_index(self) -> Tuple[int, int]:
        """Calculate current queue index and current track elapsed time."""
        # player is playing a constant stream so we need to do this the hard way
        queue_index = 0
        elapsed_time_queue = self.__get_total_elapsed_time()
        total_time = 0
        track_time = 0
        if self._items and len(self._items) > self._start_index:
            # start_index: holds the last starting position
            queue_index = self._start_index
            queue_track = None
            while len(self._items) > queue_index:
                queue_track = self._items[queue_index]
                if elapsed_time_queue > (queue_track.duration + total_time):
                    total_time += queue_track.duration
                    queue_index += 1
                else:
                    track_time = elapsed_time_queue - total_time
                    break
        return queue_index, track_time

    @staticmethod
    def __shuffle_items(queue_items: List[QueueItem]) -> List[QueueItem]:
        """Shuffle a list of tracks."""
        # for now we use default python random function
        # can be extended with some more magic based on last_played and stuff
        return random.sample(queue_items, len(queue_items))

    async def _restore_saved_state(self) -> None:
        """Try to load the saved state from database."""
        if db_row := await self.mass.database.get_row(
            "queue_settings", {"queue_id": self.queue_id}
        ):
            self._shuffle_enabled = db_row["shuffle_enabled"]
            self._repeat_enabled = db_row["repeat_enabled"]
            self._crossfade_duration = db_row["crossfade_duration"]
        if queue_cache := await self.mass.cache.get(f"queue_items.{self.queue_id}"):
            self._items = queue_cache["items"]
            self._current_index = queue_cache["current_index"]

    async def _save_state(self) -> None:
        """Save state in database."""
        # save queue settings in db
        await self.mass.database.insert_or_replace(
            "queue_settings",
            {
                "queue_id": self.queue_id,
                "shuffle_enabled": self._shuffle_enabled,
                "repeat_enabled": self.repeat_enabled,
                "crossfade_duration": self._crossfade_duration,
                "volume_normalization_enabled": self._volume_normalization_enabled,
                "volume_normalization_target": self._volume_normalization_target,
            },
        )

        # store current items in cache
        async def cache_items():
            await self.mass.cache.set(
                f"queue_items.{self.queue_id}",
                {"items": self._items, "current_index": self._current_index},
            )

        if self._save_task and not self._save_task.cancelled():
            return
        self._save_task = self.mass.loop.call_later(60, create_task, cache_items)
