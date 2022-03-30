"""Models and helpers for a player."""
from __future__ import annotations
from abc import ABC
from asyncio import TimerHandle
from dataclasses import dataclass
from enum import Enum, IntEnum
import random

from typing import TYPE_CHECKING, Any, Dict, List
from uuid import uuid4

from mashumaro import DataClassDictMixin

from music_assistant.constants import EventType
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import create_task
from music_assistant.music.models import MediaType, StreamDetails

if TYPE_CHECKING:
    from music_assistant.music.models import Track, Radio


class PlayerState(Enum):
    """Enum for the (playback)state of a player."""

    IDLE = "idle"
    PAUSED = "paused"
    PLAYING = "playing"
    OFF = "off"


@dataclass(frozen=True)
class DeviceInfo(DataClassDictMixin):
    """Model for a player's deviceinfo."""

    model: str = "unknown"
    address: str = "unknown"
    manufacturer: str = "unknown"


class PlayerFeature(IntEnum):
    """Enum for player features."""

    QUEUE = 0
    GAPLESS = 1
    CROSSFADE = 2


class Player(ABC):
    """Model for a music player."""

    player_id: str
    is_group: bool = False
    _attr_name: str = None
    _attr_powered: bool = False
    _elapsed_time: int = 0
    _attr_current_url: str = None
    _attr_state: PlayerState = PlayerState.IDLE
    _attr_available: bool = True
    _attr_volume_level: int = 100
    _attr_device_info: DeviceInfo = DeviceInfo()
    # mass object will be set by playermanager at register
    mass: MusicAssistant = None  # type: ignore[assignment]

    @property
    def name(self) -> bool:
        """Return player name."""
        return self._attr_name or self.player_id

    @property
    def powered(self) -> bool:
        """Return current power state of player."""
        return self._attr_powered

    @property
    def elapsed_time(self) -> int:
        """Return elapsed time of current playing media in seconds."""
        return self._elapsed_time

    @property
    def current_url(self) -> str:
        """Return URL that is currently loaded in the player."""

    @property
    def state(self) -> PlayerState:
        """Return current PlayerState of player."""
        return self._attr_state

    @property
    def available(self) -> bool:
        """Return current availablity of player."""
        return self._attr_available

    @property
    def volume_level(self) -> int:
        """Return current volume level of player (scale 0..100)."""
        return self._attr_volume_level

    @property
    def device_info(self) -> DeviceInfo:
        """Return basic device/provider info for this player."""
        return self._attr_device_info

    async def play_url(self, url: str) -> None:
        """Play the specified url on the player."""
        raise NotImplementedError

    async def stop(self) -> None:
        """Send STOP command to player."""
        raise NotImplementedError

    async def play(self) -> None:
        """Send PLAY/UNPAUSE command to player."""
        raise NotImplementedError

    async def pause(self) -> None:
        """Send PAUSE command to player."""
        raise NotImplementedError

    async def power(self, powered: bool) -> None:
        """Send POWER command to player."""
        raise NotImplementedError

    async def volume_set(self, volume_level: int) -> None:
        """Send volume level (0..100) command to player."""
        raise NotImplementedError

    async def volume_up(self, step_size: int = 5):
        """Send volume UP command to player."""
        new_level = min(self.volume_level + step_size, 100)
        return await self.volume_set(new_level)

    async def volume_down(self, step_size: int = 5):
        """Send volume DOWN command to player."""
        new_level = max(self.volume_level - step_size, 0)
        return await self.volume_set(new_level)

    async def play_pause(self) -> None:
        """Toggle play/pause on player."""
        if self.state == PlayerState.PAUSED:
            await self.play()
        else:
            await self.pause()

    async def power_toggle(self) -> None:
        """Toggle power on player."""
        await self.power(not self.powered)

    # DO NOT OVERRIDE BELOW

    def update_state(self) -> None:
        """Update current player state in the player manager."""
        # basic throttle: do not send state changed events if player did not change
        prev_state = getattr(self, "_prev_state", None)
        cur_state = self.to_dict()
        if prev_state == cur_state:
            return
        setattr(self, "_prev_state", cur_state)
        self.mass.signal_event(EventType.PLAYER_CHANGED, self)
        if self.is_group:
            # update group player childs when parent updates
            for child_player_id in self.group_childs:
                if player := self.mass.players.get_player(child_player_id):
                    create_task(player.update_state)
        else:
            # update group player when child updates
            for group_player_id in self.get_group_parents():
                if player := self.mass.players.get_player(group_player_id):
                    create_task(player.update_state)

    def get_group_parents(self) -> List[str]:
        """Get any/all group player id's this player belongs to."""
        return [
            x.player_id
            for x in self.mass.players
            if x.is_group and self.player_id in x.group_childs
        ]

    def to_dict(self) -> Dict[str, Any]:
        """Export object to dict."""
        return {
            "player_id": self.player_id,
            "name": self.name,
            "powered": self.powered,
            "elapsed_time": self.elapsed_time,
            "state": self.state.value,
            "available": self.available,
            "volume_level": int(self.volume_level),
            "device_info": self.device_info.to_dict(),
        }


class PlayerGroup(Player):
    """Model for a player group."""

    is_group: bool = True
    _attr_group_childs: List[str] = []
    _attr_support_join_control: bool = True

    @property
    def support_join_control(self) -> bool:
        """Return bool if joining/unjoining of players to this group is supported."""
        return self._attr_support_join_control

    @property
    def group_childs(self) -> List[str]:
        """Return list of child player id's of this PlayerGroup."""
        return self._attr_group_childs

    @property
    def volume_level(self) -> int:
        """Return current volume level of player (scale 0..100)."""
        if not self.available:
            return 0
        # calculate group volume from powered players for convenience
        group_volume = 0
        active_players = 0
        for child_player in self._get_players(True):
            group_volume += child_player.volume_level
            active_players += 1
        if active_players:
            group_volume = group_volume / active_players
        return int(group_volume)

    async def power(self, powered: bool) -> None:
        """Send POWER command to player."""
        try:
            super().power(powered)
        except NotImplementedError:
            self._attr_powered = powered
        if not powered:
            # turn off all childs
            for child_player in self._get_players(True):
                await child_player.power(False)

    async def volume_set(self, volume_level: int) -> None:
        """Send volume level (0..100) command to player."""
        # handle group volume
        cur_volume = self.volume_level
        new_volume = volume_level
        volume_dif = new_volume - cur_volume
        if cur_volume == 0:
            volume_dif_percent = 1 + (new_volume / 100)
        else:
            volume_dif_percent = volume_dif / cur_volume
        for child_player in self._get_players(True):
            cur_child_volume = child_player.volume_level
            new_child_volume = cur_child_volume + (
                cur_child_volume * volume_dif_percent
            )
            await child_player.volume_set(new_child_volume)

    async def join(self, player_id: str) -> None:
        """Command to add/join a player to this group."""
        raise NotImplementedError

    async def unjoin(self, player_id: str) -> None:
        """Command to remove/unjoin a player to this group."""
        raise NotImplementedError

    def _get_players(self, only_powered: bool = False) -> List[Player]:
        """Get players attached to this group."""
        return [
            x
            for x in self.mass.players
            if x.player_id in self.group_childs and x.powered or not only_powered
        ]


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
    duration: int | None = None
    item_id: str = ""
    sort_index: int = 0
    streamdetails: StreamDetails | None = None

    def __post_init__(self):
        """Set default values."""
        if not self.item_id:
            self.item_id = str(uuid4())
        if not self.name:
            self.name = self.uri

    @classmethod
    def from_media_item(cls, media_item: "Track" | "Radio"):
        """Construct QueueItem from track/radio item."""
        return cls(uri=media_item.uri, duration=media_item.duration)


class PlayerQueue:
    """Represents a PlayerQueue object."""

    def __init__(self, mass: MusicAssistant, player_id: str):
        """Instantiate a PlayerQueue instance."""
        self.mass = mass
        self.logger = mass.players.logger
        self.queue_id = player_id
        self.player_id = player_id

        self._shuffle_enabled: bool = False
        self._repeat_enabled: bool = False
        self._crossfade_duration: int = 0
        self._volume_normalisation_enabled: bool = True
        self._volume_normalisation_target: int = 23

        self._elapsed_time: int = 0
        self._current_index: int | None = None
        self._current_item_time: int = 0
        self._last_item: int | None = None
        self._start_index: int = 0
        self._next_index: int = 0
        self._state: PlayerState = PlayerState.IDLE
        self._last_state = PlayerState.IDLE
        self._items: List[QueueItem] = []
        self._save_task: TimerHandle = None
        create_task(self._restore_saved_state)

    @property
    def player(self) -> Player:
        """Return the player attached to this queue."""
        return self.mass.players.get_player(self.player_id, include_unavailable=True)

    @property
    def available(self) -> bool:
        """Return bool if this queue is available."""
        return self.player.available

    @property
    def active(self) -> bool:
        """Return bool if the queue is currenty active on the player."""
        # TODO: figure out a way to handle group childs player the parent queue
        return self.player.current_url == self.get_stream_url()

    @property
    def elapsed_time(self) -> int:
        """Return elapsed time of current playing media in seconds."""
        if not self.active:
            return self.player.elapsed_time
        return self._elapsed_time

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
    def items(self) -> List[QueueItem]:
        """Return all items in this queue."""
        return self._items

    @property
    def current_index(self) -> int | None:
        """
        Return the current index of the queue.

        Returns None if queue is empty.
        """
        return self._current_index

    @property
    def current_item(self) -> QueueItem | None:
        """
        Return the current item in the queue.

        Returns None if queue is empty.
        """
        if self._current_index is None:
            return None
        return self._items[self._current_index]

    @property
    def next_index(self) -> int | None:
        """
        Return the next index for this PlayerQueue.

        Return None if queue is empty or no more items.
        """
        if not self._items:
            # queue is empty
            return None
        if self._current_index is None:
            # playback just started
            return 0
        # player already playing (or paused) so return the next item
        if len(self._items) > (self._current_index + 1):
            return self._current_index + 1
        if self.repeat_enabled:
            # repeat enabled, start queue at beginning
            return 0
        return None

    @property
    def next_item(self) -> QueueItem | None:
        """
        Return the next item in the queue.

        Returns None if queue is empty or no more items.
        """
        if self.next_index is not None:
            return self._items[self.next_index]
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

    def index_by_id(self, queue_item_id: str) -> int | None:
        """Get index by queue_item_id."""
        for index, item in enumerate(self.items):
            if item.item_id == queue_item_id:
                return index
        return None

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
            return await self.load(queue_id, queue_items)
        if queue_opt in [QueueOption.PLAY, QueueOption.NEXT] and len(queue_items) > 100:
            return await self.load(queue_id, queue_items)
        if queue_opt == QueueOption.NEXT:
            return await self.insert(queue_id, queue_items, 1)
        if queue_opt == QueueOption.PLAY:
            return await self.insert(queue_id, queue_items, 0)
        if queue_opt == QueueOption.ADD:
            return await self.append(queue_id, queue_items)

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
            self._save_state()

    async def stop(self) -> None:
        """Stop command on queue player."""
        # redirect to underlying player
        await self.player.stop()

    async def play(self) -> None:
        """Play (unpause) command on queue player."""
        if self._state == PlayerState.PAUSED:
            await self.player.play()
        else:
            await self.resume()

    async def pause(self) -> None:
        """Pause command on queue player."""
        # redirect to underlying player
        await self.player.pause()

    async def next(self) -> None:
        """Play the next track in the queue."""
        if self._current_index is None:
            return
        await self.play_index(self._current_index + 1)

    async def previous(self) -> None:
        """Play the previous track in the queue."""
        if self._current_index is None:
            return
        await self.play_index(self._current_index - 1)

    async def resume(self) -> None:
        """Resume previous queue."""
        # TODO: Support skipping to last known position
        if self._items:
            prev_index = self._current_index
            await self.play_index(prev_index)
        else:
            self.logger.warning(
                "resume queue requested for %s but queue is empty", self.name
            )

    async def play_index(self, index: int | str) -> None:
        """Play item at index (or item_id) X in queue."""
        if not isinstance(index, int):
            index = self.index_by_id(index)
        if index is None:
            raise FileNotFoundError("Unknown index/id: %s" % index)
        if not len(self.items) > index:
            return
        self._current_index = index
        self._next_index = index

        # send stream url to player connected to this queue
        queue_stream_url = self.get_stream_url()
        await self.player.play_url(queue_stream_url)

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

    # @callback
    # def update_state(self) -> None:
    #     """Update queue details, called when player updates."""
    #     new_index = self._current_index
    #     track_time = self._cur_item_time
    #     new_item_loaded = False
    #     # handle queue stream
    #     if self._state == PlayerState.PLAYING and self.elapsed_time > 1:
    #         new_index, track_time = self.__get_queue_stream_index()

    #     # process new index
    #     if self._current_index != new_index:
    #         # queue track updated
    #         self._current_index = new_index
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
        self._current_item_time = 0
        self._current_index = self._next_index
        self._next_index += 1
        self._start_index = self._current_index
        return self._current_index

    async def queue_stream_next(self, cur_index: int) -> None:
        """Call when queue_streamer loads next track in buffer."""
        next_index = 0
        if len(self.items) > (next_index):
            next_index = cur_index + 1
        elif self._repeat_enabled:
            # repeat enabled, start queue at beginning
            next_index = 0
        self._next_index = next_index + 1
        return next_index

    # @callback
    # def __get_queue_stream_index(self) -> Tuple[int, int]:
    #     """Get index of queue stream."""
    #     # player is playing a constant stream of the queue so we need to do this the hard way
    #     queue_index = 0
    #     elapsed_time_queue = self.player.elapsed_time
    #     total_time = 0
    #     track_time = 0
    #     if self.items and len(self.items) > self._start_index:
    #         queue_index = (
    #             self._start_index
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

    def get_stream_url(self) -> str:
        """Return the full stream url for the PlayerQueue Stream."""
        url = f"{self.mass.web.stream_url}/queue/{self.queue_id}"
        return url

    @staticmethod
    def __shuffle_items(queue_items: List[QueueItem]) -> List[QueueItem]:
        """Shuffle a list of tracks."""
        # for now we use default python random function
        # can be extended with some more magic based on last_played and stuff
        return random.sample(queue_items, len(queue_items))

    async def _restore_saved_state(self) -> None:
        """Try to load the saved state from database."""
        if db_row := self.mass.database.get_row({"queue_id": self.queue_id}):
            self._shuffle_enabled = db_row["shuffle_enabled"]
            self._repeat_enabled = db_row["repeat_enabled"]
            self._crossfade_duration = db_row["crossfade_duration"]
        if queue_cache := self.mass.cache.get(f"queue_items.{self.queue_id}"):
            self._items = queue_cache["items"]
            self._current_index = queue_cache["current_index"]

    async def _save_state(self) -> None:
        """Save state in database."""
        # save queue settings in db
        await self.mass.database.insert_or_replace(
            {
                "queue_id": self.queue_id,
                "shuffle_enabled": self._shuffle_enabled,
                "repeat_enabled": self.repeat_enabled,
                "crossfade_duration": self._crossfade_duration,
                "volume_normalisation_enabled": self._volume_normalisation_enabled,
                "volume_normalisation_target": self._volume_normalisation_target,
            }
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
