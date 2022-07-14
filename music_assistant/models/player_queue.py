"""Model and helpders for a PlayerQueue."""
from __future__ import annotations

import asyncio
import os
import pathlib
import random
from asyncio import TimerHandle
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

from music_assistant.models.enums import (
    ContentType,
    EventType,
    MediaType,
    ProviderType,
    QueueOption,
    RepeatMode,
)
from music_assistant.models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant.models.event import MassEvent
from music_assistant.models.media_items import (
    MediaItemType,
    StreamDetails,
    media_from_dict,
)

from .player import Player, PlayerState
from .queue_item import QueueItem
from .queue_settings import QueueSettings

if TYPE_CHECKING:
    from music_assistant.controllers.streams import QueueStream
    from music_assistant.mass import MusicAssistant

RESOURCES_DIR = (
    pathlib.Path(__file__)
    .parent.resolve()
    .parent.resolve()
    .joinpath("helpers/resources")
)

ALERT_ANNOUNCE_FILE = str(RESOURCES_DIR.joinpath("announce.flac"))
if not os.path.isfile(ALERT_ANNOUNCE_FILE):
    ALERT_ANNOUNCE_FILE = None

FALLBACK_DURATION = 172800  # if duration is None (e.g. radio stream) = 48 hours


@dataclass
class QueueSnapShot:
    """Represent a snapshot of the queue and its settings."""

    powered: bool
    state: PlayerState
    items: List[QueueItem]
    index: Optional[int]
    position: int
    settings: dict
    volume_level: int


class PlayerQueue:
    """Represents a PlayerQueue object."""

    def __init__(self, mass: MusicAssistant, player_id: str):
        """Instantiate a PlayerQueue instance."""
        self.mass = mass
        self.logger = mass.players.logger
        self.queue_id = player_id
        self.signal_next: bool = False
        self._stream_id: str = ""
        self._settings = QueueSettings(self)
        self._current_index: Optional[int] = None
        self._current_item_elapsed_time: int = 0
        self._prev_item: Optional[QueueItem] = None
        self._last_state = str
        self._items: List[QueueItem] = []
        self._save_task: TimerHandle = None
        self._last_player_update: int = 0
        self._last_stream_id: str = ""
        self._snapshot: Optional[QueueSnapShot] = None
        self._announcement_in_progress: bool = False

    async def setup(self) -> None:
        """Handle async setup of instance."""
        await self._settings.restore()
        await self._restore_items()
        self.mass.signal_event(
            MassEvent(EventType.QUEUE_ADDED, object_id=self.queue_id, data=self)
        )

    @property
    def settings(self) -> QueueSettings:
        """Return settings/preferences for this PlayerQueue."""
        return self._settings

    @property
    def player(self) -> Player:
        """Return the player attached to this queue."""
        return self.mass.players.get_player(self.queue_id)

    @property
    def available(self) -> bool:
        """Return if player(queue) is available."""
        return self.player.available

    @property
    def stream(self) -> QueueStream | None:
        """Return the currently connected/active stream for this queue."""
        return self.mass.streams.queue_streams.get(self._stream_id)

    @property
    def index_in_buffer(self) -> int | None:
        """Return the item that is curently loaded into the buffer."""
        if not self._stream_id:
            return None
        if stream := self.mass.streams.queue_streams.get(self._stream_id):
            if not stream.done.is_set():
                return stream.index_in_buffer
        return self.current_index

    @property
    def active(self) -> bool:
        """Return if the queue is currenty active."""
        if stream := self.stream:
            if not self.stream.done.is_set():
                return True
            if not self.player.current_url:
                return False
            return stream.stream_id in self.player.current_url
        return False

    @property
    def elapsed_time(self) -> float:
        """Return elapsed time of current playing media in seconds."""
        if not self.active:
            return self.player.elapsed_time
        return self._current_item_elapsed_time

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
        except (IndexError, TypeError):
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
        media: str | List[str] | MediaItemType | List[MediaItemType],
        queue_opt: QueueOption = QueueOption.PLAY,
        passive: bool = False,
    ) -> str:
        """
        Play media item(s) on the given queue.

            :param media: media(s) that should be played (MediaItem(s) or uri's).
            :param queue_opt:
                QueueOption.PLAY -> Insert new items in queue and start playing at inserted position
                QueueOption.REPLACE -> Replace queue contents with these items
                QueueOption.NEXT -> Play item(s) after current playing item
                QueueOption.ADD -> Append new items at end of the queue
            :param passive: if passive set to true the stream url will not be sent to the player.
        """
        if self._announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        # a single item or list of items may be provided
        if not isinstance(media, list):
            media = [media]
        queue_items = []
        for item in media:
            # parse provided uri into a MA MediaItem or Basic QueueItem from URL
            if isinstance(item, str):
                try:
                    media_item = await self.mass.music.get_item_by_uri(item)
                except MusicAssistantError as err:
                    # invalid MA uri or item not found error
                    raise MediaNotFoundError(f"Invalid uri: {item}") from err
            elif isinstance(item, dict):
                media_item = media_from_dict(item)
            else:
                media_item = item

            # collect tracks to play
            tracks = []
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
            elif media_item.media_type in (
                MediaType.RADIO,
                MediaType.TRACK,
            ):
                # single item
                tracks = [media_item]

            # only add available items
            for track in tracks:
                if not track.available:
                    continue
                queue_items.append(QueueItem.from_media_item(track))

        # clear queue first if it was finished
        if self._current_index and self._current_index >= (len(self._items) - 1):
            self._current_index = None
            self._items = []

        # load items into the queue, make sure we have valid values
        queue_items = [x for x in queue_items if isinstance(x, QueueItem)]
        if queue_opt == QueueOption.REPLACE:
            await self.load(queue_items, passive)
        elif (
            queue_opt in [QueueOption.PLAY, QueueOption.NEXT] and len(queue_items) > 100
        ):
            await self.load(queue_items, passive)
        elif queue_opt == QueueOption.NEXT:
            await self.insert(queue_items, 1, passive)
        elif queue_opt == QueueOption.PLAY:
            await self.insert(queue_items, 0, passive)
        elif queue_opt == QueueOption.ADD:
            await self.append(queue_items)

    async def play_announcement(self, url: str, prepend_alert: bool = False) -> str:
        """
        Play given uri as Announcement on the queue.

        url: URL that should be played as announcement, can only be plain url.
        prepend_alert: Prepend the (TTS) announcement with an alert bell sound.
        """
        if self._announcement_in_progress:
            self.logger.warning(
                "Ignore queue command: An announcement is (already) in progress"
            )
            return

        def create_announcement(_url: str):
            return QueueItem(
                uri=_url,
                name="announcement",
                duration=30,
                streamdetails=StreamDetails(
                    provider=ProviderType.URL,
                    item_id=_url,
                    content_type=ContentType.try_parse(_url),
                    media_type=MediaType.ANNOUNCEMENT,
                    loudness=0,
                    gain_correct=4,
                    direct=_url,
                ),
                media_type=MediaType.ANNOUNCEMENT,
            )

        try:
            # create snapshot
            await self.snapshot_create()
            # stop player if needed
            if self.active and self.player.state in (
                PlayerState.PLAYING,
                PlayerState.PAUSED,
            ):
                await self.stop()
                await self._wait_for_state((PlayerState.OFF, PlayerState.IDLE))
                self._announcement_in_progress = True

            # turn on player if needed
            if not self.player.powered:
                await self.player.power(True)
                await self._wait_for_state(PlayerState.IDLE)

            # adjust volume if needed
            if self._settings.announce_volume_increase:
                announce_volume = (
                    self.player.volume_level + self._settings.announce_volume_increase
                )
                announce_volume = min(announce_volume, 100)
                announce_volume = max(announce_volume, 0)
                await self.player.volume_set(announce_volume)

            # adjust queue settings for announce playback
            self._settings.from_dict(
                {
                    "repeat_mode": "off",
                    "shuffle_enabled": False,
                }
            )

            queue_items = []
            # prepend alert sound if needed
            if prepend_alert and ALERT_ANNOUNCE_FILE:
                queue_items.append(create_announcement(ALERT_ANNOUNCE_FILE))

            queue_items.append(create_announcement(url))

            # append silence track. we use that as a reliable way to make sure
            # there is enough buffer for the player to start quickly
            # and to detect when we finished playing the alert
            silence_item = create_announcement(self.mass.streams.get_silence_url())
            queue_items.append(silence_item)

            # start queue with announcement sound(s)
            self._items = queue_items
            stream = await self.queue_stream_start(start_index=0, seek_position=0)
            # execute the play command on the player(s)
            await self.player.play_url(stream.url)

            # wait for the player to finish playing
            await asyncio.sleep(5)
            await self._wait_for_state(PlayerState.PLAYING, silence_item.item_id)

        except Exception as err:  # pylint: disable=broad-except
            self.logger.exception("Error while playing announcement", exc_info=err)
        finally:
            # restore queue
            self._announcement_in_progress = False
            await self.snapshot_restore()

    async def stop(self) -> None:
        """Stop command on queue player."""
        if self._announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        self.signal_next = False
        # redirect to underlying player
        await self.player.stop()

    async def play(self) -> None:
        """Play (unpause) command on queue player."""
        if self._announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        if self.active and self.player.state == PlayerState.PAUSED:
            await self.player.play()
        else:
            await self.resume()

    async def pause(self) -> None:
        """Pause command on queue player."""
        if self._announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
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
            resume_pos = resume_pos if resume_pos > 10 else 0
            fade_in = resume_pos > 0
            await self.play_index(resume_item.item_id, resume_pos, fade_in)
        elif len(self._items) > 0:
            # items available in queue but no previous track, start at 0
            await self.play_index(0)
        else:
            self.logger.warning(
                "resume queue requested for %s but queue is empty", self.queue_id
            )

    async def snapshot_create(self) -> None:
        """Create snapshot of current Queue state."""
        self.logger.debug("Creating snapshot...")
        self._snapshot = QueueSnapShot(
            powered=self.player.powered,
            state=self.player.state,
            items=self._items,
            index=self._current_index,
            position=self._current_item_elapsed_time,
            settings=self._settings.to_dict(),
            volume_level=self.player.volume_level,
        )

    async def snapshot_restore(self) -> None:
        """Restore snapshot of Queue state."""
        assert self._snapshot, "Create snapshot before restoring it."
        try:
            # clear queue first
            await self.clear()
            # restore volume if needed
            if self._snapshot.volume_level != self.player.volume_level:
                await self.player.volume_set(self._snapshot.volume_level)
            # restore queue
            self._settings.from_dict(self._snapshot.settings)
            await self.update_items(self._snapshot.items)
            self._current_index = self._snapshot.index
            self._current_item_elapsed_time = self._snapshot.position
            if self._snapshot.state in (PlayerState.PLAYING, PlayerState.PAUSED):
                await self.resume()
            if self._snapshot.state == PlayerState.PAUSED:
                await self.pause()
            if not self._snapshot.powered:
                await self.player.power(False)
            # reset snapshot once restored
            self.logger.debug("Restored snapshot...")
        except Exception as err:  # pylint: disable=broad-except
            self.logger.exception("Error while restoring snapshot", exc_info=err)
        finally:
            self._snapshot = None

    async def play_index(
        self,
        index: Union[int, str],
        seek_position: int = 0,
        fade_in: bool = False,
        passive: bool = False,
    ) -> None:
        """Play item at index (or item_id) X in queue."""
        if self._announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
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
        await self.update_items(items)

    async def delete_item(self, queue_item_id: str) -> None:
        """Delete item (by id or index) from the queue."""
        item_index = self.index_by_id(queue_item_id)
        if self.stream and item_index <= self.index_in_buffer:
            # ignore request if track already loaded in the buffer
            # the frontend should guard so this is just in case
            self.logger.warning("delete requested for item already loaded in buffer")
            return
        self._items.pop(item_index)
        self.signal_update(True)

    async def load(self, queue_items: List[QueueItem], passive: bool = False) -> None:
        """Load (overwrite) queue with new items."""
        for index, item in enumerate(queue_items):
            item.sort_index = index
        if self.settings.shuffle_enabled and len(queue_items) > 5:
            queue_items = random.sample(queue_items, len(queue_items))
        self._items = [x for x in queue_items if x is not None]  # filter None items
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
        if offset == 0:
            cur_index = self._current_index
        else:
            cur_index = self.index_in_buffer or self._current_index
        if not self.items or cur_index is None:
            return await self.load(queue_items, passive)
        insert_at_index = cur_index + offset
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
        if offset in (0, cur_index):
            await self.play_index(insert_at_index, passive=passive)

        self.signal_update(True)

    async def append(self, queue_items: List[QueueItem]) -> None:
        """Append new items at the end of the queue."""
        for index, item in enumerate(queue_items):
            item.sort_index = len(self.items) + index
        if self.settings.shuffle_enabled:
            # if shuffle is enabled we shuffle the remaining tracks and the new ones
            cur_index = self.index_in_buffer or self._current_index
            if cur_index is None:
                played_items = []
                next_items = self.items + queue_items
                cur_item = []
            else:
                played_items = self.items[:cur_index] if cur_index is not None else []
                next_items = self.items[cur_index + 1 :] + queue_items
                cur_item = [self.get_item(cur_index)]
            # do the shuffle
            next_items = random.sample(next_items, len(next_items))
            queue_items = played_items + cur_item + next_items
        else:
            queue_items = self._items + queue_items
        await self.update_items(queue_items)

    async def clear(self) -> None:
        """Clear all items in the queue."""
        if self.player.state not in (PlayerState.IDLE, PlayerState.OFF):
            await self.stop()
        await self.update_items([])

    def on_player_update(self) -> None:
        """Call when player updates."""
        player_state_str = f"{self.player.state.value}.{self.player.current_url}"
        if self._last_state != player_state_str:
            # playback state changed
            self._last_state = player_state_str

            # always signal update if playback state changed
            self.signal_update()
            if self.player.state == PlayerState.IDLE:

                # handle end of queue
                if self._current_index is not None and self._current_index >= (
                    len(self._items) - 1
                ):
                    self._current_index += 1
                    self._current_item_elapsed_time = 0

                # handle case where stream stopped on purpose and we need to restart it
                elif self.signal_next:
                    self.signal_next = False
                    self.mass.create_task(self.resume())

        self.update_state()

    def update_state(self) -> None:
        """Update queue details, called when player updates."""
        if self.player.active_queue != self:
            return
        if not self.active:
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

    async def queue_stream_start(
        self,
        start_index: int,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> QueueStream:
        """Start the queue stream runner."""
        # start the queue stream background task
        stream = await self.mass.streams.start_queue_stream(
            queue=self,
            start_index=start_index,
            seek_position=seek_position,
            fade_in=fade_in,
            output_format=self._settings.stream_type,
        )
        self._stream_id = stream.stream_id
        self._current_item_elapsed_time = 0
        self._current_index = start_index
        return stream

    def get_next_index(self, cur_index: Optional[int]) -> int:
        """Return the next index for the queue, accounting for repeat settings."""
        # handle repeat single track
        if self.settings.repeat_mode == RepeatMode.ONE:
            return cur_index
        # handle repeat all
        if (
            self.settings.repeat_mode == RepeatMode.ALL
            and self._items
            and cur_index == (len(self._items) - 1)
        ):
            return 0
        # simply return the next index. other logic is guarded to detect the index
        # being higher than the number of items to detect end of queue and/or handle repeat.
        if cur_index is None:
            return 0
        return cur_index + 1

    def signal_update(self, items_changed: bool = False) -> None:
        """Signal state changed of this queue."""
        if items_changed:
            self.mass.create_task(self._save_items())
            self.mass.signal_event(
                MassEvent(
                    EventType.QUEUE_ITEMS_UPDATED, object_id=self.queue_id, data=self
                )
            )
        # always send the base event
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
            "index_in_buffer": self.index_in_buffer,
            "current_item": cur_item,
            "next_item": next_item,
            "items": len(self._items),
            "settings": self.settings.to_dict(),
        }

    async def update_items(self, queue_items: List[QueueItem]) -> None:
        """Update the existing queue items, mostly caused by reordering."""
        self._items = queue_items
        self.signal_update(True)

    def __get_queue_stream_index(self) -> Tuple[int, int]:
        """Calculate current queue index and current track elapsed time."""
        # player is playing a constant stream so we need to do this the hard way
        queue_index = 0
        elapsed_time_queue = self.player.elapsed_time
        total_time = 0
        track_time = 0
        if self._items and self.stream and len(self._items) > self.stream.start_index:
            # start_index: holds the position from which the stream started
            queue_index = self.stream.start_index
            queue_track = None
            while len(self._items) > queue_index:
                # keep enumerating the queue tracks to find current track
                # starting from the start index
                queue_track = self._items[queue_index]
                if not queue_track.streamdetails:
                    track_time = elapsed_time_queue - total_time
                    break
                duration = (
                    queue_track.streamdetails.seconds_streamed or queue_track.duration
                )
                if duration is not None and elapsed_time_queue > (
                    duration + total_time
                ):
                    # total elapsed time is more than (streamed) track duration
                    # move index one up
                    total_time += duration
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

    async def _wait_for_state(
        self,
        state: Union[None, PlayerState, Tuple[PlayerState]],
        queue_item_id: Optional[str] = None,
        timeout: int = 30,
    ) -> None:
        """Wait for player(queue) to reach a specific state."""
        if state is not None and not isinstance(state, tuple):
            state = (state,)

        count = 0
        while count < timeout * 10:

            if (state is None or self.player.state in state) and (
                queue_item_id is None
                or self.current_item
                and self.current_item.item_id == queue_item_id
            ):
                return

            count += 1
            await asyncio.sleep(0.1)

        raise TimeoutError(f"Timeout while waiting on state(s) {state}")
