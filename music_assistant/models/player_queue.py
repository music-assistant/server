"""Model and helpders for a PlayerQueue."""
from __future__ import annotations

import asyncio
import pathlib
import random
from asyncio import Task, TimerHandle
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

from music_assistant.helpers.audio import get_stream_details
from music_assistant.models.enums import EventType, MediaType, QueueOption, RepeatMode
from music_assistant.models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
    QueueEmpty,
)
from music_assistant.models.event import MassEvent
from music_assistant.models.media_items import StreamDetails

from .player import Player, PlayerGroup, PlayerState
from .queue_item import QueueItem
from .queue_settings import QueueSettings

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

RESOURCES_DIR = (
    pathlib.Path(__file__)
    .parent.resolve()
    .parent.resolve()
    .joinpath("helpers/resources")
)
ALERT_ANNOUNCE_FILE = str(RESOURCES_DIR.joinpath("announce.flac"))
FALLBACK_DURATION = 172800  # if duration is None (e.g. radio stream) = 48 hours


@dataclass
class QueueSnapShot:
    """Represent a snapshot of the queue and its settings."""

    powered: bool
    state: PlayerState
    volume_level: int
    items: List[QueueItem]
    index: Optional[int]
    position: int


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
        self._prev_item: Optional[QueueItem] = None
        # start_index: from which index did the queuestream start playing
        self._start_index: int = 0
        # start_pos: from which position (in seconds) did the first track start playing?
        self._start_pos: int = 0
        self._next_fadein: int = 0
        self._next_start_index: int = 0
        self._next_start_pos: int = 0
        self._last_state = PlayerState.IDLE
        self._items: List[QueueItem] = []
        self._save_task: TimerHandle = None
        self._update_task: Task = None
        self._signal_next: bool = False
        self._last_player_update: int = 0
        self._stream_url: str = ""
        self._snapshot: Optional[QueueSnapShot] = None

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
        try:
            next_index = self.get_next_index(self._current_index)
            return self._items[next_index]
        except IndexError:
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
            # parse provided uri into a MA MediaItem or Basic QueueItem from URL
            try:
                media_item = await self.mass.music.get_item_by_uri(uri)
            except MusicAssistantError as err:
                # invalid MA uri or item not found error
                if uri.startswith("http"):
                    # a plain url was provided
                    queue_items.append(QueueItem.from_url(uri))
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

        # clear queue first if it was finished
        if self._current_index and self._current_index >= (len(self._items) - 1):
            self._current_index = None
            self._items = []
        # clear resume point if any
        self._start_pos = 0

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

    async def play_alert(
        self, uri: str, announce: bool = False, volume_adjust: int = 10
    ) -> str:
        """
        Play given uri as Alert on the queue.

        uri: Uri that should be played as announcement, can be Music Assistant URI or plain url.
        announce: Prepend the (TTS) alert with a small announce sound.
        volume_adjust: Adjust the volume of the player by this percentage (relative).
        """
        if self._snapshot:
            self.logger.debug("Ignore play_alert: already in progress")
            return

        # create snapshot
        await self.snapshot_create()

        queue_items = []

        # prepend annnounce sound if needed
        if announce:
            queue_items.append(QueueItem.from_url(ALERT_ANNOUNCE_FILE, "alert"))

        # parse provided uri into a MA MediaItem or Basic QueueItem from URL
        try:
            media_item = await self.mass.music.get_item_by_uri(uri)
            queue_items.append(QueueItem.from_media_item(media_item))
        except MusicAssistantError as err:
            # invalid MA uri or item not found error
            if uri.startswith("http"):
                # a plain url was provided
                queue_items.append(QueueItem.from_url(uri, "alert"))
            else:
                raise MediaNotFoundError(f"Invalid uri: {uri}") from err

        # append silence track, we use this to reliably detect when the alert is ready
        silence_url = self.mass.streams.get_silence_url(600)
        queue_items.append(QueueItem.from_url(silence_url, "alert"))

        # load queue with alert sound(s)
        await self.load(queue_items)
        # set new volume
        new_volume = self.player.volume_level + (
            self.player.volume_level / 100 * volume_adjust
        )
        await self.player.volume_set(new_volume)

        # wait for the alert to finish playing
        alert_done = asyncio.Event()

        def handle_event(evt: MassEvent):
            if (
                self.current_item
                and self.current_item.uri == silence_url
                and self.elapsed_time
            ):
                alert_done.set()

        unsub = self.mass.subscribe(
            handle_event, EventType.QUEUE_TIME_UPDATED, self.queue_id
        )
        try:
            await asyncio.wait_for(alert_done.wait(), 120)
        finally:

            unsub()
            # restore queue
            await self.snapshot_restore()

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

    async def skip_ahead(self, seconds: int = 10) -> None:
        """Skip X seconds ahead in track."""
        await self.seek(self.elapsed_time + seconds)

    async def skip_back(self, seconds: int = 10) -> None:
        """Skip X seconds back in track."""
        await self.seek(self.elapsed_time - seconds)

    async def seek(self, position: int) -> None:
        """Seek to a specific position in the track (given in seconds)."""
        assert self.current_item, "No item loaded"
        assert position < self.current_item.duration, "Position exceeds track duration"
        await self.play_index(self._current_index, position)

    async def resume(self) -> None:
        """Resume previous queue."""
        resume_item = self.current_item
        resume_pos = self._current_item_elapsed_time
        if (
            resume_item
            and resume_item.duration
            and resume_pos > (resume_item.duration * 0.9)
        ):
            # track is already played for > 90% - skip to next
            resume_item = self.next_item
            resume_pos = 0

        if resume_item is not None:
            await self.play_index(resume_item.item_id, resume_pos, 5)
        else:
            self.logger.warning(
                "resume queue requested for %s but queue is empty", self.queue_id
            )

    async def snapshot_create(self) -> None:
        """Create snapshot of current Queue state."""
        self._snapshot = QueueSnapShot(
            powered=self.player.powered,
            state=self.player.state,
            volume_level=self.player.volume_level,
            items=self._items,
            index=self._current_index,
            position=self._current_item_elapsed_time,
        )

    async def snapshot_restore(self) -> None:
        """Restore snapshot of Queue state."""
        assert self._snapshot, "Create snapshot before restoring it."
        # clear queue first
        await self.clear()
        # restore volume
        await self.player.volume_set(self._snapshot.volume_level)
        # restore queue
        await self.update(self._snapshot.items)
        self._current_index = self._snapshot.index
        if self._snapshot.state in (PlayerState.PLAYING, PlayerState.PAUSED):
            await self.resume()
        if self._snapshot.state == PlayerState.PAUSED:
            await self.pause()
        if not self._snapshot.powered:
            await self.player.power(False)
        # reset snapshot once restored
        self._snapshot = None

    async def play_index(
        self,
        index: Union[int, str],
        seek_position: int = 0,
        fade_in: int = 0,
        passive: bool = False,
    ) -> None:
        """Play item at index (or item_id) X in queue."""
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
        self._next_start_pos = int(seek_position)
        self._next_fadein = fade_in
        # send stream url to player connected to this queue
        self._stream_url = self.mass.streams.get_stream_url(
            self.queue_id, content_type=self._settings.stream_type
        )

        if self.player.use_multi_stream:
            # multi stream enabled, all child players should receive the same audio stream
            # redirect command to all (powered) players
            coros = []
            expected_clients = set()
            for child_id in self.player.group_childs:
                if child_player := self.mass.players.get_player(child_id):
                    if child_player.powered:
                        # TODO: this assumes that all client players support flac
                        player_url = self.mass.streams.get_stream_url(
                            self.queue_id, child_id, self._settings.stream_type
                        )
                        expected_clients.add(child_id)
                        coros.append(child_player.play_url(player_url))
            await self.mass.streams.start_multi_client_queue_stream(
                # TODO: this assumes that all client players support flac
                self.queue_id,
                expected_clients,
                self._settings.stream_type,
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
            new_index = (self._current_index or 0) + 1
        elif pos_shift == 0:
            new_index = self._current_index or 0
        else:
            new_index = item_index + pos_shift
        if (new_index < (self._current_index or 0)) or (new_index > len(self.items)):
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
        cur_index = self._current_index or 0
        for index, item in enumerate(queue_items):
            item.sort_index = len(self.items) + index
        if self.settings.shuffle_enabled:
            played_items = self.items[:cur_index]
            next_items = self.items[cur_index + 1 :] + queue_items
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
        if self.player.state not in (PlayerState.IDLE, PlayerState.OFF):
            await self.stop()
        await self.update([])

    def on_player_update(self) -> None:
        """Call when player updates."""
        if self._last_state != self.player.state:
            # playback state changed
            self._last_state = self.player.state

            # always signal update if playback state changed
            self.signal_update()
            if self.player.state == PlayerState.IDLE:

                # handle end of queue
                if self._current_index is not None and self._current_index >= (
                    len(self._items) - 1
                ):
                    self._current_index += 1
                    self._current_item_elapsed_time = 0
                    # repeat enabled (of whole queue), play queue from beginning
                    if self.settings.repeat_mode == RepeatMode.ALL:
                        self.mass.create_task(self.play_index(0))

                # handle case where stream stopped on purpose and we need to restart it
                elif self._signal_next:
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
        if self.player.state == PlayerState.PLAYING and self.player.elapsed_time > 0:
            new_index, track_time = self.__get_queue_stream_index()
        # process new index
        if self._current_index != new_index:
            # queue track updated
            self._current_index = new_index
        # check if a new track is loaded, wait for the streamdetails
        if (
            self.current_item
            and self._prev_item != self.current_item
            and self.current_item.streamdetails
        ):
            # new active item in queue
            new_item_loaded = True
            self._prev_item = self.current_item
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

    async def queue_stream_start(self) -> Tuple[int, int, int]:
        """Call when queue_streamer starts playing the queue stream."""
        start_from_index = self._next_start_index
        self._current_item_elapsed_time = 0
        self._current_index = start_from_index
        self._start_index = start_from_index
        self._next_start_index = self.get_next_index(start_from_index)
        self._index_in_buffer = start_from_index
        seek_position = self._next_start_pos
        self._next_start_pos = 0
        fade_in = self._next_fadein
        self._next_fadein = 0
        return (start_from_index, seek_position, fade_in)

    async def queue_stream_next(self, cur_index: int) -> int | None:
        """Call when queue_streamer loads next track in buffer."""
        next_idx = self._next_start_index
        self._index_in_buffer = next_idx
        self._next_start_index = self.get_next_index(self._next_start_index)
        return next_idx

    def get_next_index(self, cur_index: int) -> int:
        """Return the next index for the queue, accounting for repeat settings."""
        if not self._items or cur_index is None:
            raise IndexError("No (more) items in queue")
        if self.settings.repeat_mode == RepeatMode.ONE:
            return cur_index
        # simply return the next index. other logic is guarded to detect the index
        # being higher than the number of items to detect end of queue and/or handle repeat.
        return cur_index + 1

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
                # keep enumerating the queue tracks to find current track
                # starting from the start index
                queue_track = self._items[queue_index]
                if not queue_track.streamdetails:
                    track_time = elapsed_time_queue - total_time
                    break
                track_seconds = queue_track.streamdetails.seconds_played
                if elapsed_time_queue > (track_seconds + total_time):
                    # total elapsed time is more than (streamed) track duration
                    # move index one up
                    total_time += track_seconds
                    queue_index += 1
                else:
                    # no more seconds left to divide, this is our track
                    # account for any seeking by adding the skipped seconds
                    track_sec_skipped = queue_track.streamdetails.seconds_skipped
                    track_time = elapsed_time_queue + track_sec_skipped - total_time
                    break
        return queue_index, track_time

    async def _restore_items(self) -> None:
        """Try to load the saved state from cache."""
        if queue_cache := await self.mass.cache.get(f"queue_items.{self.queue_id}"):
            try:
                self._items = [QueueItem.from_dict(x) for x in queue_cache["items"]]
                self._current_index = queue_cache["current_index"]
                self._current_item_elapsed_time = queue_cache.get(
                    "current_item_elapsed_time", 0
                )
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
                "current_item_elapsed_time": self._current_item_elapsed_time,
            },
        )
