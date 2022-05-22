"""Model and helpders for a PlayerQueue."""
from __future__ import annotations

import asyncio
import random
from asyncio import Task, TimerHandle
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union
from uuid import uuid4

from mashumaro import DataClassDictMixin

from music_assistant.helpers.audio import get_stream_details
from music_assistant.models.enums import (
    ContentType,
    CrossFadeMode,
    EventType,
    MediaType,
    QueueOption,
    RepeatMode,
)
from music_assistant.models.errors import MediaNotFoundError, QueueEmpty
from music_assistant.models.event import MassEvent
from music_assistant.models.media_items import Radio, StreamDetails, Track

from .player import Player, PlayerGroup, PlayerState

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


@dataclass
class QueueItem(DataClassDictMixin):
    """Representation of a queue item."""

    uri: str
    name: str = ""
    duration: Optional[int] = None
    item_id: str = ""
    sort_index: int = 0
    streamdetails: Optional[StreamDetails] = None
    media_type: MediaType = MediaType.UNKNOWN
    image: Optional[str] = None
    available: bool = True
    media_item: Union[Track, Radio, None] = None

    def __post_init__(self):
        """Set default values."""
        if not self.item_id:
            self.item_id = str(uuid4())
        if not self.name:
            self.name = self.uri

    @classmethod
    def __pre_deserialize__(cls, d: Dict[Any, Any]) -> Dict[Any, Any]:
        """Run actions before deserialization."""
        d.pop("streamdetails", None)
        return d

    def __post_serialize__(self, d: Dict[Any, Any]) -> Dict[Any, Any]:
        """Run actions before serialization."""
        if self.media_type == MediaType.RADIO:
            d.pop("duration")
        return d

    @classmethod
    def from_media_item(cls, media_item: Track | Radio):
        """Construct QueueItem from track/radio item."""
        if isinstance(media_item, Track):
            artists = "/".join((x.name for x in media_item.artists))
            name = f"{artists} - {media_item.name}"
        else:
            name = media_item.name
        return cls(
            uri=media_item.uri,
            name=name,
            duration=media_item.duration,
            media_type=media_item.media_type,
            media_item=media_item,
            image=media_item.image,
            available=media_item.available,
        )


class QueueSettings:
    """Representation of (user adjustable) PlayerQueue settings/preferences."""

    def __init__(self, queue: PlayerQueue) -> None:
        """Initialize."""
        self._queue = queue
        self.mass = queue.mass
        self._repeat_mode: RepeatMode = RepeatMode.OFF
        self._shuffle_enabled: bool = False
        self._crossfade_mode: CrossFadeMode = CrossFadeMode.DISABLED
        self._crossfade_duration: int = 6
        self._volume_normalization_enabled: bool = True
        self._volume_normalization_target: int = -23

    @property
    def repeat_mode(self) -> RepeatMode:
        """Return repeat enabled setting."""
        return self._repeat_mode

    @repeat_mode.setter
    def repeat_mode(self, enabled: bool) -> None:
        """Set repeat enabled setting."""
        if self._repeat_mode != enabled:
            self._repeat_mode = enabled
            self._on_update("repeat_mode")

    @property
    def shuffle_enabled(self) -> bool:
        """Return shuffle enabled setting."""
        return self._shuffle_enabled

    @shuffle_enabled.setter
    def shuffle_enabled(self, enabled: bool) -> None:
        """Set shuffle enabled setting."""
        if not self._shuffle_enabled and enabled:
            # shuffle requested
            self._shuffle_enabled = True
            if self._queue.current_index is not None:
                played_items = self._queue.items[: self._queue.current_index]
                next_items = self._queue.items[self._queue.current_index + 1 :]
                # for now we use default python random function
                # can be extended with some more magic based on last_played and stuff
                next_items = random.sample(next_items, len(next_items))
                items = played_items + [self._queue.current_item] + next_items
                asyncio.create_task(self._queue.update(items))
                self._on_update("shuffle_enabled")
        elif self._shuffle_enabled and not enabled:
            # unshuffle
            self._shuffle_enabled = False
            if self._queue.current_index is not None:
                played_items = self._queue.items[: self._queue.current_index]
                next_items = self._queue.items[self._queue.current_index + 1 :]
                next_items.sort(key=lambda x: x.sort_index, reverse=False)
                items = played_items + [self._queue.current_item] + next_items
                asyncio.create_task(self._queue.update(items))
                self._on_update("shuffle_enabled")

    @property
    def crossfade_mode(self) -> CrossFadeMode:
        """Return crossfade mode setting."""
        return self._crossfade_mode

    @crossfade_mode.setter
    def crossfade_mode(self, mode: CrossFadeMode) -> None:
        """Set crossfade enabled setting."""
        if self._crossfade_mode != mode:
            # TODO: restart the queue stream if its playing
            self._crossfade_mode = mode
            self._on_update("crossfade_mode")

    @property
    def crossfade_duration(self) -> int:
        """Return crossfade_duration setting."""
        return self._crossfade_duration

    @crossfade_duration.setter
    def crossfade_duration(self, duration: int) -> None:
        """Set crossfade_duration setting (1..10 seconds)."""
        duration = max(1, duration)
        duration = min(10, duration)
        if self._crossfade_duration != duration:
            self._crossfade_duration = duration
            self._on_update("crossfade_duration")

    @property
    def volume_normalization_enabled(self) -> bool:
        """Return volume_normalization_enabled setting."""
        return self._volume_normalization_enabled

    @volume_normalization_enabled.setter
    def volume_normalization_enabled(self, enabled: bool) -> None:
        """Set volume_normalization_enabled setting."""
        if self._volume_normalization_enabled != enabled:
            self._volume_normalization_enabled = enabled
            self._on_update("volume_normalization_enabled")

    @property
    def volume_normalization_target(self) -> float:
        """Return volume_normalization_target setting."""
        return self._volume_normalization_target

    @volume_normalization_target.setter
    def volume_normalization_target(self, target: float) -> None:
        """Set volume_normalization_target setting (-40..10 LUFS)."""
        target = max(-40, target)
        target = min(10, target)
        if self._volume_normalization_target != target:
            self._volume_normalization_target = target
            self._on_update("volume_normalization_target")

    @property
    def stream_type(self) -> ContentType:
        """Return supported/preferred stream type for playerqueue. Read only."""
        # determine default stream type from player capabilities
        return next(
            x
            for x in (
                ContentType.FLAC,
                ContentType.WAVE,
                ContentType.PCM_S16LE,
                ContentType.MP3,
                ContentType.MPEG,
            )
            if x in self._queue.player.supported_content_types
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return dict from settings."""
        return {
            "repeat_mode": self.repeat_mode.value,
            "shuffle_enabled": self.shuffle_enabled,
            "crossfade_mode": self.crossfade_mode.value,
            "crossfade_duration": self.crossfade_duration,
            "volume_normalization_enabled": self.volume_normalization_enabled,
            "volume_normalization_target": self.volume_normalization_target,
        }

    async def restore(self) -> None:
        """Restore state from db."""
        async with self.mass.database.get_db() as _db:
            for key, val_type in (
                ("repeat_mode", RepeatMode),
                ("crossfade_mode", CrossFadeMode),
                ("shuffle_enabled", bool),
                ("crossfade_duration", int),
                ("volume_normalization_enabled", bool),
                ("volume_normalization_target", float),
            ):
                db_key = f"{self._queue.queue_id}_{key}"
                if db_value := await self.mass.database.get_setting(db_key, db=_db):
                    value = val_type(db_value["value"])
                    setattr(self, f"_{key}", value)

    def _on_update(self, changed_key: Optional[str] = None) -> None:
        """Handle state changed."""
        self._queue.signal_update()
        self.mass.create_task(self.save(changed_key))
        # TODO: restart play if setting changed that impacts playing queue

    async def save(self, changed_key: Optional[str] = None) -> None:
        """Save state in db."""
        async with self.mass.database.get_db() as _db:
            for key, value in self.to_dict().items():
                if key == changed_key or changed_key is None:
                    db_key = f"{self._queue.queue_id}_{key}"
                    await self.mass.database.set_setting(db_key, value, db=_db)


class PlayerQueue:
    """Represents a PlayerQueue object."""

    def __init__(self, mass: MusicAssistant, player_id: str):
        """Instantiate a PlayerQueue instance."""
        self.mass = mass
        self.logger = mass.players.logger
        self.queue_id = player_id
        self._settings = QueueSettings(self)
        self._current_index: Optional[int] = None
        # index_in_buffer: which track is currently (pre)loaded in the streamer
        self._index_in_buffer: Optional[int] = None
        self._current_item_elapsed_time: int = 0
        self._last_item: Optional[QueueItem] = None
        # start_index: from which index did the queuestream start playing
        self._start_index: int = 0
        self._next_start_index: int = 0
        self._last_state = PlayerState.IDLE
        self._items: List[QueueItem] = []
        self._save_task: TimerHandle = None
        self._update_task: Task = None
        self._signal_next: bool = False
        self._last_player_update: int = 0

        self._stream_url: str = ""

    async def setup(self) -> None:
        """Handle async setup of instance."""
        await self._settings.restore()
        await self._restore_items()
        self._stream_url: str = self.mass.streams.get_stream_url(
            self.queue_id, content_type=self._settings.stream_type
        )
        self.mass.signal_event(
            MassEvent(EventType.QUEUE_ADDED, object_id=self.queue_id, data=self)
        )

    @property
    def settings(self) -> QueueSettings:
        """Return settings/preferences for this PlayerQueue."""
        return self._settings

    @property
    def player(self) -> Player | PlayerGroup:
        """Return the player attached to this queue."""
        return self.mass.players.get_player(self.queue_id, include_unavailable=True)

    @property
    def available(self) -> bool:
        """Return if player(queue) is available."""
        return self.player.available

    @property
    def active(self) -> bool:
        """Return bool if the queue is currenty active on the player."""
        if self.player.use_multi_stream:
            return self.queue_id in self.player.current_url
        return self._stream_url == self.player.current_url

    @property
    def elapsed_time(self) -> float:
        """Return elapsed time of current playing media in seconds."""
        if not self.active:
            return self.player.elapsed_time
        return self._current_item_elapsed_time

    @property
    def max_sample_rate(self) -> int:
        """Return the maximum samplerate supported by this queue(player)."""
        return max(self.player.supported_sample_rates)

    @property
    def items(self) -> List[QueueItem]:
        """Return all items in this queue."""
        return self._items

    @property
    def current_index(self) -> int | None:
        """Return current index."""
        return self._current_index

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
        passive: bool = False,
    ) -> str:
        """
        Play media item(s) on the given queue.

            :param queue_id: queue id of the PlayerQueue to handle the command.
            :param uri: uri(s) that should be played (single item or list of uri's).
            :param queue_opt:
                QueueOption.PLAY -> Insert new items in queue and start playing at inserted position
                QueueOption.REPLACE -> Replace queue contents with these items
                QueueOption.NEXT -> Play item(s) after current playing item
                QueueOption.ADD -> Append new items at end of the queue
            :param passive: do not actually start playback.
        Returns: the stream URL for this queue.
        """
        # a single item or list of items may be provided
        if not isinstance(uris, list):
            uris = [uris]
        queue_items = []
        for uri in uris:
            # parse provided uri into a MA MediaItem or Basis QueueItem from URL
            try:
                media_item = await self.mass.music.get_item_by_uri(uri)
            except MediaNotFoundError as err:
                if uri.startswith("http"):
                    # a plain url was provided
                    queue_items.append(QueueItem(uri))
                    continue
                raise MediaNotFoundError(f"Invalid uri: {uri}") from err

            # collect tracks to play
            if media_item.media_type == MediaType.ARTIST:
                tracks = await self.mass.music.artists.toptracks(
                    media_item.item_id, provider=media_item.provider
                )
            elif media_item.media_type == MediaType.ALBUM:
                tracks = await self.mass.music.albums.tracks(
                    media_item.item_id, provider=media_item.provider
                )
            elif media_item.media_type == MediaType.PLAYLIST:
                tracks = await self.mass.music.playlists.tracks(
                    media_item.item_id, provider=media_item.provider
                )
            elif media_item.media_type == MediaType.RADIO:
                # single radio
                tracks = [
                    await self.mass.music.radio.get(
                        media_item.item_id, provider=media_item.provider
                    )
                ]
            else:
                # single track
                tracks = [
                    await self.mass.music.tracks.get(
                        media_item.item_id, provider=media_item.provider
                    )
                ]
            for track in tracks:
                if not track.available:
                    continue
                queue_items.append(QueueItem.from_media_item(track))

        # load items into the queue
        if queue_opt == QueueOption.REPLACE:
            await self.load(queue_items, passive=passive)
        elif (
            queue_opt in [QueueOption.PLAY, QueueOption.NEXT] and len(queue_items) > 100
        ):
            await self.load(queue_items, passive=passive)
        elif queue_opt == QueueOption.NEXT:
            await self.insert(queue_items, 1, passive=passive)
        elif queue_opt == QueueOption.PLAY:
            await self.insert(queue_items, 0, passive=passive)
        elif queue_opt == QueueOption.ADD:
            await self.append(queue_items)
        return self._stream_url

    async def stop(self) -> None:
        """Stop command on queue player."""
        # redirect to underlying player
        await self.player.stop()

    async def play(self) -> None:
        """Play (unpause) command on queue player."""
        if self.active and self.player.state == PlayerState.PAUSED:
            await self.player.play()
        else:
            await self.resume()

    async def pause(self) -> None:
        """Pause command on queue player."""
        # redirect to underlying player
        await self.player.pause()

    async def play_pause(self) -> None:
        """Toggle play/pause on queue/player."""
        if self.player.state == PlayerState.PLAYING:
            await self.pause()
            return
        await self.play()

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

    async def play_index(self, index: Union[int, str], passive: bool = False) -> None:
        """Play item at index (or item_id) X in queue."""
        # power on player when requesting play
        if not self.player.powered:
            await self.player.power(True)
        if self.player.use_multi_stream:
            await self.mass.streams.stop_multi_client_queue_stream(self.queue_id)
        if not isinstance(index, int):
            index = self.index_by_id(index)
        if index is None:
            raise FileNotFoundError(f"Unknown index/id: {index}")
        if not len(self.items) > index:
            return
        self._current_index = index
        self._next_start_index = index
        # send stream url to player connected to this queue
        self._stream_url = self.mass.streams.get_stream_url(
            self.queue_id, content_type=self._settings.stream_type
        )

        if self.player.use_multi_stream:
            # multi stream enabled, all child players should receive the same audio stream
            # redirect command to all (powered) players
            # TODO: this assumes that all client players support flac
            content_type = ContentType.FLAC
            coros = []
            expected_clients = set()
            for child_id in self.player.group_childs:
                if child_player := self.mass.players.get_player(child_id):
                    if child_player.powered:
                        # TODO: this assumes that all client players support flac
                        player_url = self.mass.streams.get_stream_url(
                            self.queue_id, child_id, content_type
                        )
                        expected_clients.add(child_id)
                        coros.append(child_player.play_url(player_url))
            await self.mass.streams.start_multi_client_queue_stream(
                # TODO: this assumes that all client players support flac
                self.queue_id,
                expected_clients,
                content_type,
            )
            await asyncio.gather(*coros)
        elif not passive:
            # regular (single player) request
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

    async def delete_item(self, queue_item_id: str) -> None:
        """Delete item (by id or index) from the queue."""
        item_index = self.index_by_id(queue_item_id)
        if item_index <= self._index_in_buffer:
            # ignore request if track already loaded in the buffer
            # the frontend should guard so this is just in case
            return
        self._items.pop(item_index)
        self.signal_update(True)

    async def load(self, queue_items: List[QueueItem], passive: bool = False) -> None:
        """Load (overwrite) queue with new items."""
        for index, item in enumerate(queue_items):
            item.sort_index = index
        if self.settings.shuffle_enabled and len(queue_items) > 5:
            queue_items = random.sample(queue_items, len(queue_items))
        self._items = queue_items
        await self.play_index(0, passive=passive)
        self.signal_update(True)

    async def insert(
        self, queue_items: List[QueueItem], offset: int = 0, passive: bool = False
    ) -> None:
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
        if self.settings.shuffle_enabled and len(queue_items) > 5:
            queue_items = random.sample(queue_items, len(queue_items))
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

        if offset in (0, self._index_in_buffer):
            await self.play_index(insert_at_index, passive=passive)

        self.signal_update(True)

    async def append(self, queue_items: List[QueueItem]) -> None:
        """Append new items at the end of the queue."""
        for index, item in enumerate(queue_items):
            item.sort_index = len(self.items) + index
        if self.settings.shuffle_enabled:
            played_items = self.items[: self._current_index]
            next_items = self.items[self._current_index + 1 :] + queue_items
            next_items = random.sample(next_items, len(next_items))
            items = played_items + [self.current_item] + next_items
            await self.update(items)
            return
        self._items = self._items + queue_items
        self.signal_update(True)

    async def update(self, queue_items: List[QueueItem]) -> None:
        """Update the existing queue items, mostly caused by reordering."""
        self._items = queue_items
        self.signal_update(True)

    async def clear(self) -> None:
        """Clear all items in the queue."""
        await self.stop()
        await self.update([])

    def on_player_update(self) -> None:
        """Call when player updates."""
        if self._last_state != self.player.state:
            self._last_state = self.player.state
            # always signal update if playback state changed
            self.signal_update()
            # handle case where stream stopped on purpose and we need to restart it
            if self.player.state != PlayerState.PLAYING and self._signal_next:
                self._signal_next = False
                self.mass.create_task(self.resume())

            # start poll/updater task if playback starts on player
            async def updater() -> None:
                """Update player queue every second while playing."""
                while True:
                    await asyncio.sleep(1)
                    self.update_state()

            if self.player.state == PlayerState.PLAYING and self.active:
                if not self._update_task or self._update_task.done():
                    self._update_task = self.mass.create_task(updater)
            elif self._update_task:
                self._update_task.cancel()
                self._update_task = None

        self.update_state()

    def update_state(self) -> None:
        """Update queue details, called when player updates."""
        if self.player.active_queue.queue_id != self.queue_id:
            return
        new_index = self._current_index
        track_time = self._current_item_elapsed_time
        new_item_loaded = False
        # if self.player.state == PlayerState.PLAYING:
        if self.player.state == PlayerState.PLAYING and self.player.elapsed_time > 0:
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
        prev_item_time = int(self._current_item_elapsed_time)
        self._current_item_elapsed_time = int(track_time)

        if new_item_loaded:
            self.signal_update()
        if abs(prev_item_time - self._current_item_elapsed_time) >= 1:
            self.mass.signal_event(
                MassEvent(
                    EventType.QUEUE_TIME_UPDATED,
                    object_id=self.queue_id,
                    data=int(self.elapsed_time),
                )
            )

    async def queue_stream_prepare(self) -> StreamDetails:
        """Call when queue_streamer is about to start playing."""
        start_from_index = self._next_start_index
        try:
            next_item = self._items[start_from_index]
        except (IndexError, TypeError) as err:
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
        self._current_item_elapsed_time = 0
        self._current_index = start_from_index
        self._start_index = start_from_index
        self._next_start_index = self.get_next_index(start_from_index)
        self._index_in_buffer = start_from_index
        return start_from_index

    async def queue_stream_next(self, cur_index: int) -> int | None:
        """Call when queue_streamer loads next track in buffer."""
        next_idx = self._next_start_index
        self._index_in_buffer = next_idx
        self._next_start_index = self.get_next_index(self._next_start_index)
        return next_idx

    def get_next_index(self, index: int) -> int | None:
        """Return the next index or None if no more items."""
        if not self._items or index is None:
            # queue is empty
            return None
        if self.settings.repeat_mode == RepeatMode.ONE:
            return index
        if len(self._items) > (index + 1):
            return index + 1
        if self.settings.repeat_mode == RepeatMode.ALL:
            # repeat enabled, start queue at beginning
            return 0
        return None

    async def queue_stream_signal_next(self):
        """Indicate that queue stream needs to start next index once playback finished."""
        self._signal_next = True

    def signal_update(self, items_changed: bool = False) -> None:
        """Signal state changed of this queue."""
        if items_changed:
            self.mass.create_task(self._save_items())
            self.mass.signal_event(
                MassEvent(
                    EventType.QUEUE_ITEMS_UPDATED, object_id=self.queue_id, data=self
                )
            )
        else:
            self.mass.signal_event(
                MassEvent(EventType.QUEUE_UPDATED, object_id=self.queue_id, data=self)
            )

    def to_dict(self) -> Dict[str, Any]:
        """Export object to dict."""
        cur_item = self.current_item.to_dict() if self.current_item else None
        next_item = self.next_item.to_dict() if self.next_item else None
        return {
            "queue_id": self.queue_id,
            "player": self.player.player_id,
            "name": self.player.name,
            "active": self.active,
            "elapsed_time": int(self.elapsed_time),
            "state": self.player.state.value,
            "available": self.player.available,
            "current_index": self.current_index,
            "current_item": cur_item,
            "next_item": next_item,
            "settings": self.settings.to_dict(),
        }

    def __get_queue_stream_index(self) -> Tuple[int, int]:
        """Calculate current queue index and current track elapsed time."""
        # player is playing a constant stream so we need to do this the hard way
        queue_index = 0
        elapsed_time_queue = self.player.elapsed_time
        total_time = 0
        track_time = 0
        if self._items and len(self._items) > self._start_index:
            # start_index: holds the last starting position
            queue_index = self._start_index
            queue_track = None
            while len(self._items) > queue_index:
                queue_track = self._items[queue_index]
                if queue_track.duration is None:
                    # in case of a radio stream
                    queue_track.duration = 86400
                if elapsed_time_queue > (queue_track.duration + total_time):
                    total_time += queue_track.duration
                    queue_index += 1
                else:
                    track_time = elapsed_time_queue - total_time
                    break
        return queue_index, track_time

    async def _restore_items(self) -> None:
        """Try to load the saved state from cache."""
        if queue_cache := await self.mass.cache.get(f"queue_items.{self.queue_id}"):
            try:
                self._items = [QueueItem.from_dict(x) for x in queue_cache["items"]]
                self._current_index = queue_cache["current_index"]
            except (KeyError, AttributeError, TypeError) as err:
                self.logger.warning(
                    "Unable to restore queue state for queue %s",
                    self.queue_id,
                    exc_info=err,
                )
        await self.settings.restore()

    async def _save_items(self) -> None:
        """Save current queue items/state in cache."""
        await self.mass.cache.set(
            f"queue_items.{self.queue_id}",
            {
                "items": [x.to_dict() for x in self._items],
                "current_index": self._current_index,
            },
        )
