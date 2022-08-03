"""Model for a PlayerQueue."""
from __future__ import annotations

import asyncio
import random
from asyncio import TimerHandle
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

from music_assistant.constants import ANNOUNCE_ALERT_FILE, FALLBACK_DURATION
from music_assistant.helpers.tags import parse_tags
from music_assistant.helpers.util import try_parse_int
from music_assistant.models.enums import (
    EventType,
    MediaType,
    ProviderType,
    QueueOption,
    RepeatMode,
)
from music_assistant.models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant.models.event import MassEvent
from music_assistant.models.media_items import MediaItemType, media_from_dict

from .player import Player, PlayerState
from .queue_item import QueueItem
from .queue_settings import QueueSettings

if TYPE_CHECKING:
    from music_assistant.controllers.streams import QueueStream
    from music_assistant.mass import MusicAssistant


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
    player_url: str


class PlayerQueue:
    """Represents a PlayerQueue object."""

    def __init__(self, mass: MusicAssistant, player_id: str):
        """Instantiate a PlayerQueue instance."""
        self.mass = mass
        self.logger = mass.players.logger
        self.queue_id = player_id
        self._stream_id: str = ""
        self._settings = QueueSettings(self)
        self._current_index: Optional[int] = None
        self._current_item_elapsed_time: int = 0
        self._prev_item: Optional[QueueItem] = None
        self._last_player_state: Tuple[PlayerState, str] = (PlayerState.OFF, "")
        self._items: List[QueueItem] = []
        self._save_task: TimerHandle = None
        self._last_player_update: int = 0
        self._last_stream_id: str = ""
        self._snapshot: Optional[QueueSnapShot] = None
        self._radio_source: List[MediaItemType] = []
        self.announcement_in_progress: bool = False

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
        option: QueueOption = QueueOption.PLAY,
        radio_mode: bool = False,
        passive: bool = False,
    ) -> str:
        """
        Play media item(s) on the given queue.

        media: Media(s) that should be played (MediaItem(s) or uri's).
        queue_opt: Which enqueue mode to use.
        radio_mode: Enable radio mode for the given item(s).
        passive: If passive set to true the stream url will not be sent to the player.
        """
        if self.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return

        # a single item or list of items may be provided
        if not isinstance(media, list):
            media = [media]

        # clear queue first if it was finished
        if self._current_index and self._current_index >= (len(self._items) - 1):
            self._current_index = None
            self._items = []

        # clear radio source items if needed
        if option not in (QueueOption.ADD, QueueOption.PLAY, QueueOption.NEXT):
            self._radio_source = []

        tracks: List[MediaItemType] = []
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
            ctrl = self.mass.music.get_controller(media_item.media_type)
            if radio_mode:
                self._radio_source.append(media_item)
                # if radio mode enabled, grab the first batch of tracks here
                tracks += await ctrl.dynamic_tracks(
                    item_id=media_item.item_id, provider=media_item.provider
                )
            elif media_item.media_type in (
                MediaType.ARTIST,
                MediaType.ALBUM,
                MediaType.PLAYLIST,
            ):
                tracks += await ctrl.tracks(
                    media_item.item_id, provider=media_item.provider
                )
            else:
                # single track or radio item
                tracks += [media_item]

        # only add valid/available items
        queue_items = [
            QueueItem.from_media_item(x) for x in tracks if x and x.available
        ]

        # load the items into the queue
        cur_index = self.index_in_buffer or self._current_index or 0
        shuffle = self.settings.shuffle_enabled and len(queue_items) >= 5

        # handle replace: clear all items and replace with the new items
        if option == QueueOption.REPLACE:
            await self.clear()
            await self.load(queue_items, shuffle=shuffle)
            if not passive:
                await self.play_index(0)
        # handle next: add item(s) in the index next to the playing/loaded/buffered index
        elif option == QueueOption.NEXT:
            await self.load(queue_items, insert_at_index=cur_index + 1, shuffle=shuffle)
        elif option == QueueOption.REPLACE_NEXT:
            await self.load(
                queue_items,
                insert_at_index=cur_index + 1,
                keep_remaining=False,
                shuffle=shuffle,
            )
        # handle play: replace current loaded/playing index with new item(s)
        elif option == QueueOption.PLAY:
            await self.load(queue_items, insert_at_index=cur_index, shuffle=shuffle)
            if not passive:
                await self.play_index(cur_index)
        # handle add: add/append item(s) to the remaining queue items
        elif option == QueueOption.ADD:
            shuffle = self.settings.shuffle_enabled
            if shuffle:
                # shuffle the new items with remaining queue items
                insert_at_index = cur_index + 1
            else:
                # just append at the end
                insert_at_index = len(self._items)
            await self.load(
                queue_items, insert_at_index=insert_at_index, shuffle=shuffle
            )

    async def _fill_radio_tracks(self) -> None:
        """Fill the Queue with (additional) Radio tracks."""
        assert self._radio_source, "No Radio item(s) loaded/active!"
        tracks: List[MediaItemType] = []
        # grab dynamic tracks for (all) source items
        # shuffle the source items, just in case
        for radio_item in random.sample(self._radio_source, len(self._radio_source)):
            ctrl = self.mass.music.get_controller(radio_item.media_type)
            tracks += await ctrl.dynamic_tracks(
                item_id=radio_item.item_id, provider=radio_item.provider
            )
            # make sure we do not grab too much items
            if len(tracks) >= 50:
                break
        # fill queue - filter out unavailable items
        queue_items = [QueueItem.from_media_item(x) for x in tracks if x.available]
        await self.load(
            queue_items,
            insert_at_index=len(self._items) - 1,
        )

    async def play_announcement(self, url: str, prepend_alert: bool = False) -> str:
        """
        Play given uri as Announcement on the queue.

        url: URL that should be played as announcement, can only be plain url.
        prepend_alert: Prepend the (TTS) announcement with an alert bell sound.
        """
        if self.announcement_in_progress:
            self.logger.warning(
                "Ignore queue command: An announcement is (already) in progress"
            )
            return

        try:
            # create snapshot
            await self.snapshot_create()
            wait_time = 2
            # stop player if needed
            if self.active and self.player.state == PlayerState.PLAYING:
                await self.stop()
                self.announcement_in_progress = True
                await asyncio.sleep(0.1)

            # adjust volume if needed
            if self._settings.announce_volume_increase:
                announce_volume = (
                    self.player.volume_level + self._settings.announce_volume_increase
                )
                announce_volume = min(announce_volume, 100)
                announce_volume = max(announce_volume, 0)
                # turn on player if needed (might be needed before adjusting the volume)
                if not self.player.powered:
                    await self.player.power(True)
                    wait_time += 2
                await self.player.volume_set(announce_volume)

            # prepend alert sound if needed
            if prepend_alert:
                announce_urls = (ANNOUNCE_ALERT_FILE, url)
                wait_time += 2
            else:
                announce_urls = (url,)

            # send announcement stream to player
            announce_stream_url = self.mass.streams.get_announcement_url(
                self.queue_id, announce_urls, self._settings.stream_type
            )
            await self.player.play_url(announce_stream_url)

            # wait for the player to finish playing
            info = await parse_tags(url)
            wait_time += info.duration or 10
            await asyncio.sleep(wait_time)

        except Exception as err:  # pylint: disable=broad-except
            self.logger.exception("Error while playing announcement", exc_info=err)
        finally:
            # restore queue
            self.announcement_in_progress = False
            await self.snapshot_restore()

    async def stop(self) -> None:
        """Stop command on queue player."""
        if self.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        if stream := self.stream:
            stream.signal_next = None
        # redirect to underlying player
        await self.player.stop()

    async def play(self) -> None:
        """Play (unpause) command on queue player."""
        if self.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        if self.player.state == PlayerState.PAUSED:
            await self.player.play()
        else:
            await self.resume()

    async def pause(self) -> None:
        """Pause command on queue player."""
        if self.announcement_in_progress:
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
        next_index = self.get_next_index(self._current_index, True)
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
        assert self.current_item.media_item.media_type == MediaType.TRACK
        assert self.current_item.duration
        assert position < self.current_item.duration
        await self.play_index(self._current_index, position)

    async def resume(self) -> None:
        """Resume previous queue."""
        last_player_url = self._last_player_state[1]
        if last_player_url and self.mass.streams.base_url not in last_player_url:
            self.logger.info("Trying to resume non-MA content %s...", last_player_url)
            await self.player.play_url(last_player_url)
            return
        resume_item = self.current_item
        next_item = self.next_item
        resume_pos = self._current_item_elapsed_time
        if (
            resume_item
            and next_item
            and resume_item.duration
            and resume_pos > (resume_item.duration * 0.9)
        ):
            # track is already played for > 90% - skip to next
            resume_item = next_item
            resume_pos = 0
        elif self._current_index is None and len(self._items) > 0:
            # items available in queue but no previous track, start at 0
            resume_item = self.get_item(0)
            resume_pos = 0

        if resume_item is not None:
            resume_pos = resume_pos if resume_pos > 10 else 0
            fade_in = resume_pos > 0
            await self.play_index(resume_item.item_id, resume_pos, fade_in)
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
            player_url=self.player.current_url,
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
            self._last_player_state = (
                self._snapshot.state,
                self._snapshot.player_url,
            )
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
        if self.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
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
        # TODO: guard for position that is already played/buffered!
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

    async def load(
        self,
        queue_items: List[QueueItem],
        insert_at_index: int = 0,
        keep_remaining: bool = True,
        shuffle: bool = False,
    ) -> None:
        """
        Load new items at index.

        queue_items: a list of QueueItem
        insert_at_index: insert the item(s) at this index
        keep_remaining: keep the remaining items after the insert
        shuffle: (re)shuffle the items after insert index
        """

        # keep previous/played items, append the new ones
        prev_items = self._items[:insert_at_index]
        next_items = queue_items

        # if keep_remaining, append the old previous items
        if keep_remaining:
            next_items += self._items[insert_at_index:]

        # we set the original insert order as attribute so we can un-shuffle
        for index, item in enumerate(next_items):
            item.sort_index += insert_at_index + index
        # (re)shuffle the final batch if needed
        if shuffle:
            next_items = random.sample(next_items, len(next_items))
        await self.update_items(prev_items + next_items)

    async def clear(self) -> None:
        """Clear all items in the queue."""
        self._radio_source = []
        if self.player.state not in (PlayerState.IDLE, PlayerState.OFF):
            await self.stop()
        await self.update_items([])

    def on_player_update(self) -> None:
        """Call when player updates."""
        prev_state = self._last_player_state
        new_state = (self.player.state, self.player.current_url)

        # handle PlayerState changed
        if new_state[0] != prev_state[0]:

            # store previous state
            if self.announcement_in_progress:
                # while announcement in progress dont update the last url
                # to allow us to resume from 3rd party sources
                # https://github.com/music-assistant/hass-music-assistant/issues/697
                self._last_player_state = (new_state[0], prev_state[1])
            else:
                self._last_player_state = new_state

            # the queue stream was aborted on purpose and needs to restart
            if (
                prev_state[0] == PlayerState.PLAYING
                and new_state[0] == PlayerState.IDLE
                and self.stream
                and self.stream.signal_next is not None
            ):
                # the queue stream was aborted on purpose (e.g. because of sample rate mismatch)
                # we need to restart the stream with the next index
                self._current_item_elapsed_time = 0
                self.mass.create_task(self.play_index(self.stream.signal_next))
                return

            # queue exhausted or player turned off/stopped
            if self.stream and (
                new_state[0] in (PlayerState.IDLE, PlayerState.OFF)
                or not self.player.available
            ):
                self.stream.signal_next = None
                # handle last track of the queue, set the index to index that is out of range
                if (self._current_index or 0) >= (len(self._items) - 1):
                    self._current_index += 1

        # always signal update if the PlayerState changed
        if new_state[0] != prev_state[0]:
            self.signal_update()

        # update queue details only if we're the active queue for the attached player
        if self.player.active_queue != self or not self.active:
            return

        track_time = self._current_item_elapsed_time
        new_item_loaded = False
        if self.player.state == PlayerState.PLAYING and self.player.elapsed_time > 0:
            new_index, track_time = self.__get_queue_stream_index()

            # process new index
            if self._current_index != new_index:
                # queue index updated
                self._current_index = new_index
                # watch dynamic radio items refill if needed
                fill_index = len(self._items) - 5
                if self._radio_source and (new_index >= fill_index):
                    self.mass.create_task(self._fill_radio_tracks())

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
            self.mass.create_task(self._fetch_full_details(self._current_index))
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

    def get_next_index(self, cur_index: Optional[int], is_skip: bool = False) -> int:
        """Return the next index for the queue, accounting for repeat settings."""
        # handle repeat single track
        if self.settings.repeat_mode == RepeatMode.ONE and not is_skip:
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
        next_index = cur_index + 1
        return next_index

    def signal_update(self, items_changed: bool = False) -> None:
        """Signal state changed of this queue."""
        if items_changed:
            self.mass.signal_event(
                MassEvent(
                    EventType.QUEUE_ITEMS_UPDATED, object_id=self.queue_id, data=self
                )
            )
            # save items
            self.mass.create_task(
                self.mass.cache.set(
                    f"queue.items.{self.queue_id}",
                    [x.to_dict() for x in self._items],
                )
            )

        # always send the base event
        self.mass.signal_event(
            MassEvent(EventType.QUEUE_UPDATED, object_id=self.queue_id, data=self)
        )
        # save state
        self.mass.create_task(
            self.mass.database.set_setting(
                f"queue.{self.queue_id}.current_index", self._current_index
            )
        )
        self.mass.create_task(
            self.mass.database.set_setting(
                f"queue.{self.queue_id}.current_item_elapsed_time",
                self._current_item_elapsed_time,
            )
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
            "radio_source": [x.to_dict() for x in self._radio_source[:5]],
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
                if queue_track.streamdetails.seconds_streamed is not None:
                    track_duration = queue_track.streamdetails.seconds_streamed
                else:
                    track_duration = queue_track.duration or FALLBACK_DURATION
                if elapsed_time_queue > (track_duration + total_time):
                    # total elapsed time is more than (streamed) track duration
                    # move index one up
                    total_time += track_duration
                    queue_index += 1
                else:
                    # no more seconds left to divide, this is our track
                    # account for any seeking by adding the skipped seconds
                    track_sec_skipped = queue_track.streamdetails.seconds_skipped or 0
                    track_time = elapsed_time_queue + track_sec_skipped - total_time
                    break
        return queue_index, track_time

    async def _restore_items(self) -> None:
        """Try to load the saved state from cache."""
        if queue_cache := await self.mass.cache.get(f"queue.items.{self.queue_id}"):
            try:
                self._items = [QueueItem.from_dict(x) for x in queue_cache]
            except (KeyError, AttributeError, TypeError) as err:
                self.logger.warning(
                    "Unable to restore queue state for queue %s",
                    self.queue_id,
                    exc_info=err,
                )
            else:
                # restore state too
                db_key = f"queue.{self.queue_id}.current_index"
                if db_value := await self.mass.database.get_setting(db_key):
                    self._current_index = try_parse_int(db_value)
                db_key = f"queue.{self.queue_id}.current_item_elapsed_time"
                if db_value := await self.mass.database.get_setting(db_key):
                    self._current_item_elapsed_time = try_parse_int(db_value)

        await self.settings.restore()

    async def _fetch_full_details(self, index: int) -> None:
        """Background task that fetches the full details of an item in the queue."""
        if not self._items or len(self._items) < (index + 1):
            return

        item_before = self._items[index]

        # check if the details are already fetched
        if item_before.media_item.provider == ProviderType.DATABASE:
            return

        # fetch full details here to prevent all clients do this on their own
        full_details = await self.mass.music.get_item_by_uri(
            self.current_item.media_item.uri, lazy=False
        )
        # convert to queueitem in between to minimize data
        temp_queue_item = QueueItem.from_media_item(full_details)

        # safe guard: check that item still matches
        # prevents race condition where items changes just while we were waiting for data
        if self._items[index].item_id != item_before.item_id:
            return
        self._items[index].media_item = temp_queue_item.media_item
        self.signal_update()
