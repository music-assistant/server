"""Logic to play music from MusicProviders to supported players."""

from __future__ import annotations

import asyncio
import random
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any, TypedDict

from music_assistant.common.helpers.util import get_changed_keys
from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    EventType,
    MediaType,
    PlayerFeature,
    PlayerState,
    QueueOption,
    RepeatMode,
)
from music_assistant.common.models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
    PlayerUnavailableError,
    QueueEmpty,
)
from music_assistant.common.models.media_items import AlbumTrack, MediaItemType, media_from_dict
from music_assistant.common.models.player import PlayerMedia
from music_assistant.common.models.player_queue import PlayerQueue
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import CONF_FLOW_MODE, FALLBACK_DURATION, MASS_LOGO_ONLINE
from music_assistant.server.helpers.api import api_command
from music_assistant.server.helpers.audio import get_stream_details
from music_assistant.server.models.core_controller import CoreController

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant.common.models.media_items import Album, Artist, Track
    from music_assistant.common.models.player import Player


CONF_DEFAULT_ENQUEUE_SELECT_ARTIST = "default_enqueue_select_artist"
CONF_DEFAULT_ENQUEUE_SELECT_ALBUM = "default_enqueue_select_album"

ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE = "all_tracks"
ENQUEUE_SELECT_ALBUM_DEFAULT_VALUE = "all_tracks"

CONF_DEFAULT_ENQUEUE_OPTION_ARTIST = "default_enqueue_option_artist"
CONF_DEFAULT_ENQUEUE_OPTION_ALBUM = "default_enqueue_option_album"
CONF_DEFAULT_ENQUEUE_OPTION_TRACK = "default_enqueue_option_track"
CONF_DEFAULT_ENQUEUE_OPTION_RADIO = "default_enqueue_option_radio"
CONF_DEFAULT_ENQUEUE_OPTION_PLAYLIST = "default_enqueue_option_playlist"


class CompareState(TypedDict):
    """Simple object where we store the (previous) state of a queue.

    Used for compare actions.
    """

    queue_id: str
    state: PlayerState
    current_index: int | None
    elapsed_time: int
    stream_title: str | None


class PlayerQueuesController(CoreController):
    """Controller holding all logic to enqueue music for players."""

    domain: str = "player_queues"

    def __init__(self, *args, **kwargs) -> None:
        """Initialize core controller."""
        super().__init__(*args, **kwargs)
        self._queues: dict[str, PlayerQueue] = {}
        self._queue_items: dict[str, list[QueueItem]] = {}
        self._prev_states: dict[str, CompareState] = {}
        self.manifest.name = "Player Queues controller"
        self.manifest.description = (
            "Music Assistant's core controller which manages the queues for all players."
        )
        self.manifest.icon = "playlist-music"

    async def close(self) -> None:
        """Cleanup on exit."""
        # stop all playback
        for queue in self.all():
            if queue.state not in (PlayerState.PLAYING, PlayerState.PAUSED):
                continue
            await self.stop(queue.queue_id)

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        enqueue_options = tuple(ConfigValueOption(x.name, x.value) for x in QueueOption)
        return (
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
                type=ConfigEntryType.STRING,
                default_value=ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE,
                label="Items to select when you play a (in-library) artist.",
                options=(
                    ConfigValueOption(
                        title="Only in-library tracks",
                        value="library_tracks",
                    ),
                    ConfigValueOption(
                        title="All tracks from all albums in the library",
                        value="library_album_tracks",
                    ),
                    ConfigValueOption(
                        title="All (top) tracks from (all) streaming provider(s)",
                        value="all_tracks",
                    ),
                    ConfigValueOption(
                        title="All tracks from all albums from (all) streaming provider(s)",
                        value="all_album_tracks",
                    ),
                ),
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_SELECT_ALBUM,
                type=ConfigEntryType.STRING,
                default_value=ENQUEUE_SELECT_ALBUM_DEFAULT_VALUE,
                label="Items to select when you play a (in-library) album.",
                options=(
                    ConfigValueOption(
                        title="Only in-library tracks",
                        value="library_tracks",
                    ),
                    ConfigValueOption(
                        title="All tracks for album on (streaming) provider",
                        value="all_tracks",
                    ),
                ),
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_ARTIST,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.REPLACE.value,
                label="Default enqueue option for Artist item(s).",
                options=enqueue_options,
                description="Define the default enqueue action for this mediatype.",
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_ALBUM,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.REPLACE.value,
                label="Default enqueue option for Album item(s).",
                options=enqueue_options,
                description="Define the default enqueue action for this mediatype.",
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_TRACK,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.PLAY.value,
                label="Default enqueue option for Track item(s).",
                options=enqueue_options,
                description="Define the default enqueue action for this mediatype.",
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_RADIO,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.REPLACE.value,
                label="Default enqueue option for Track item(s).",
                options=enqueue_options,
                description="Define the default enqueue action for this mediatype.",
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_PLAYLIST,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.REPLACE.value,
                label="Default enqueue option for Playlist item(s).",
                options=enqueue_options,
                description="Define the default enqueue action for this mediatype.",
            ),
        )

    def __iter__(self) -> Iterator[PlayerQueue]:
        """Iterate over (available) players."""
        return iter(self._queues.values())

    @api_command("player_queues/all")
    def all(self) -> tuple[PlayerQueue, ...]:
        """Return all registered PlayerQueues."""
        return tuple(self._queues.values())

    @api_command("player_queues/get")
    def get(self, queue_id: str) -> PlayerQueue | None:
        """Return PlayerQueue by queue_id or None if not found."""
        return self._queues.get(queue_id)

    @api_command("player_queues/items")
    def items(self, queue_id: str, limit: int = 500, offset: int = 0) -> list[QueueItem]:
        """Return all QueueItems for given PlayerQueue."""
        if queue_id not in self._queue_items:
            return []

        return self._queue_items[queue_id][offset : offset + limit]

    @api_command("player_queues/get_active_queue")
    def get_active_queue(self, player_id: str) -> PlayerQueue:
        """Return the current active/synced queue for a player."""
        if player := self.mass.players.get(player_id):
            # account for player that is synced (sync child)
            if player.synced_to and player.synced_to != player.player_id:
                return self.get_active_queue(player.synced_to)
            # handle active group player
            if player.active_group and player.active_group != player.player_id:
                return self.get_active_queue(player.active_group)
            # active_source may be filled with other queue id
            if player.active_source != player_id and (
                queue := self.get_active_queue(player.active_source)
            ):
                return queue
        return self.get(player_id)

    # Queue commands

    @api_command("player_queues/shuffle")
    def set_shuffle(self, queue_id: str, shuffle_enabled: bool) -> None:
        """Configure shuffle setting on the the queue."""
        # always fetch the underlying player so we can raise early if its not available
        player = self.mass.players.get(queue_id, True)
        if player.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        queue = self._queues[queue_id]
        if queue.shuffle_enabled == shuffle_enabled:
            return  # no change

        queue.shuffle_enabled = shuffle_enabled
        queue_items = self._queue_items[queue_id]
        cur_index = queue.index_in_buffer or queue.current_index
        if cur_index is not None:
            next_index = cur_index + 1
            next_items = queue_items[next_index:]
        else:
            next_items = []
            next_index = 0

        if not shuffle_enabled:
            # shuffle disabled, try to restore original sort order of the remaining items
            next_items.sort(key=lambda x: x.sort_index, reverse=False)
        self.load(
            queue_id=queue_id,
            queue_items=next_items,
            insert_at_index=next_index,
            keep_remaining=False,
            shuffle=shuffle_enabled,
        )

    @api_command("player_queues/repeat")
    def set_repeat(self, queue_id: str, repeat_mode: RepeatMode) -> None:
        """Configure repeat setting on the the queue."""
        # always fetch the underlying player so we can raise early if its not available
        player = self.mass.players.get(queue_id, True)
        if player.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        queue = self._queues[queue_id]
        if queue.repeat_mode == repeat_mode:
            return  # no change
        queue.repeat_mode = repeat_mode
        self.signal_update(queue_id)

    @api_command("player_queues/play_media")
    async def play_media(
        self,
        queue_id: str,
        media: MediaItemType | list[MediaItemType] | str | list[str],
        option: QueueOption | None = None,
        radio_mode: bool = False,
        start_item: str | None = None,
    ) -> None:
        """Play media item(s) on the given queue.

        - media: Media that should be played (MediaItem(s) or uri's).
        - queue_opt: Which enqueue mode to use.
        - radio_mode: Enable radio mode for the given item(s).
        - start_item: Optional item to start the playlist or album from.
        """
        # ruff: noqa: PLR0915,PLR0912
        queue = self._queues[queue_id]
        # always fetch the underlying player so we can raise early if its not available
        player = self.mass.players.get(queue_id, True)
        if player.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return

        # a single item or list of items may be provided
        if not isinstance(media, list):
            media = [media]

        # clear queue first if it was finished
        if queue.current_index and queue.current_index >= (len(self._queue_items[queue_id]) - 1):
            queue.current_index = None
            self._queue_items[queue_id] = []
        # clear queue if needed
        if option == QueueOption.REPLACE:
            self.clear(queue_id)

        tracks: list[MediaItemType] = []
        radio_source: list[MediaItemType] = []
        for item in media:
            try:
                # parse provided uri into a MA MediaItem or Basic QueueItem from URL
                if isinstance(item, str):
                    media_item = await self.mass.music.get_item_by_uri(item)
                elif isinstance(item, dict):
                    media_item = media_from_dict(item)
                else:
                    media_item = item

                # handle default enqueue option if needed
                if option is None:
                    option = QueueOption(
                        await self.mass.config.get_core_config_value(
                            self.domain,
                            f"default_enqueue_option_{media_item.media_type.value}",
                        )
                    )
                    if option == QueueOption.REPLACE:
                        self.clear(queue_id)

                # collect tracks to play
                if radio_mode:
                    radio_source.append(media_item)
                elif media_item.media_type == MediaType.PLAYLIST:
                    tracks += await self.mass.music.playlists.get_all_playlist_tracks(media_item)
                    await self.mass.music.mark_item_played(
                        media_item.media_type, media_item.item_id, media_item.provider
                    )
                elif media_item.media_type == MediaType.ARTIST:
                    tracks += await self.get_artist_tracks(media_item)
                    await self.mass.music.mark_item_played(
                        media_item.media_type, media_item.item_id, media_item.provider
                    )
                elif media_item.media_type == MediaType.ALBUM:
                    tracks += await self.get_album_tracks(media_item)
                    await self.mass.music.mark_item_played(
                        media_item.media_type, media_item.item_id, media_item.provider
                    )
                else:
                    # single track or radio item
                    tracks += [media_item]

                # handle optional start item (play playlist/album from here feature)
                if start_item is not None:
                    prev_items = []
                    next_items = []
                    for track in tracks:
                        if next_items or track.item_id == start_item:
                            next_items.append(track)
                        else:
                            prev_items.append(track)
                    tracks = next_items + prev_items

            except MusicAssistantError as err:
                # invalid MA uri or item not found error
                self.logger.warning("Skipping %s: %s", item, str(err))

        # overwrite or append radio source items
        if option not in (QueueOption.ADD, QueueOption.PLAY, QueueOption.NEXT):
            queue.radio_source = radio_source
        else:
            queue.radio_source += radio_source
        # Use collected media items to calculate the radio if radio mode is on
        if radio_mode:
            tracks = await self._get_radio_tracks(queue_id)

        # only add valid/available items
        queue_items = [QueueItem.from_media_item(queue_id, x) for x in tracks if x and x.available]

        if not queue_items:
            raise MediaNotFoundError("No playable items found")

        # load the items into the queue
        if queue.state in (PlayerState.PLAYING, PlayerState.PAUSED):
            cur_index = queue.index_in_buffer or 0
        else:
            cur_index = queue.current_index or 0
        insert_at_index = cur_index + 1 if self._queue_items.get(queue_id) else 0
        shuffle = queue.shuffle_enabled and len(queue_items) > 1

        # handle replace: clear all items and replace with the new items
        if option == QueueOption.REPLACE:
            self.load(
                queue_id,
                queue_items=queue_items,
                keep_remaining=False,
                keep_played=False,
                shuffle=shuffle,
            )
            await self.play_index(queue_id, 0)
            return
        # handle next: add item(s) in the index next to the playing/loaded/buffered index
        if option == QueueOption.NEXT:
            self.load(
                queue_id,
                queue_items=queue_items,
                insert_at_index=insert_at_index,
                shuffle=shuffle,
            )
            return
        if option == QueueOption.REPLACE_NEXT:
            self.load(
                queue_id,
                queue_items=queue_items,
                insert_at_index=insert_at_index,
                keep_remaining=False,
                shuffle=shuffle,
            )
            return
        # handle play: replace current loaded/playing index with new item(s)
        if option == QueueOption.PLAY:
            self.load(
                queue_id,
                queue_items=queue_items,
                insert_at_index=insert_at_index,
                shuffle=shuffle,
            )
            next_index = min(insert_at_index, len(self._queue_items[queue_id]) - 1)
            await self.play_index(queue_id, next_index)
            return
        # handle add: add/append item(s) to the remaining queue items
        if option == QueueOption.ADD:
            self.load(
                queue_id=queue_id,
                queue_items=queue_items,
                insert_at_index=insert_at_index
                if queue.shuffle_enabled
                else len(self._queue_items[queue_id]),
                shuffle=queue.shuffle_enabled,
            )
            # handle edgecase, queue is empty and items are only added (not played)
            # mark first item as new index
            if queue.current_index is None:
                queue.current_index = 0
                queue.current_item = self.get_item(queue_id, 0)
                queue.items = len(queue_items)
                self.signal_update(queue_id)

    @api_command("player_queues/move_item")
    def move_item(self, queue_id: str, queue_item_id: str, pos_shift: int = 1) -> None:
        """
        Move queue item x up/down the queue.

        - queue_id: id of the queue to process this request.
        - queue_item_id: the item_id of the queueitem that needs to be moved.
        - pos_shift: move item x positions down if positive value
        - pos_shift: move item x positions up if negative value
        - pos_shift:  move item to top of queue as next item if 0.
        """
        # always fetch the underlying player so we can raise early if its not available
        player = self.mass.players.get(queue_id, True)
        if player.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        queue = self._queues[queue_id]
        item_index = self.index_by_id(queue_id, queue_item_id)
        if item_index <= queue.index_in_buffer:
            msg = f"{item_index} is already played/buffered"
            raise IndexError(msg)

        queue_items = self._queue_items[queue_id]
        queue_items = queue_items.copy()

        if pos_shift == 0 and queue.state == PlayerState.PLAYING:
            new_index = (queue.current_index or 0) + 1
        elif pos_shift == 0:
            new_index = queue.current_index or 0
        else:
            new_index = item_index + pos_shift
        if (new_index < (queue.current_index or 0)) or (new_index > len(queue_items)):
            return
        # move the item in the list
        queue_items.insert(new_index, queue_items.pop(item_index))
        self.update_items(queue_id, queue_items)

    @api_command("player_queues/delete_item")
    def delete_item(self, queue_id: str, item_id_or_index: int | str) -> None:
        """Delete item (by id or index) from the queue."""
        # always fetch the underlying player so we can raise early if its not available
        player = self.mass.players.get(queue_id, True)
        if player.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        if isinstance(item_id_or_index, str):
            item_index = self.index_by_id(queue_id, item_id_or_index)
        else:
            item_index = item_id_or_index
        queue = self._queues[queue_id]
        if item_index <= queue.index_in_buffer:
            # ignore request if track already loaded in the buffer
            # the frontend should guard so this is just in case
            self.logger.warning("delete requested for item already loaded in buffer")
            return
        queue_items = self._queue_items[queue_id]
        queue_items.pop(item_index)
        self.update_items(queue_id, queue_items)

    @api_command("player_queues/clear")
    def clear(self, queue_id: str) -> None:
        """Clear all items in the queue."""
        # always fetch the underlying player so we can raise early if its not available
        player = self.mass.players.get(queue_id, True)
        if player.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        queue = self._queues[queue_id]
        queue.radio_source = []
        queue.stream_finished = None
        queue.end_of_track_reached = None
        if queue.state != PlayerState.IDLE:
            self.mass.create_task(self.stop(queue_id))
        queue.current_index = None
        queue.current_item = None
        queue.elapsed_time = 0
        queue.index_in_buffer = None
        self.update_items(queue_id, [])

    @api_command("player_queues/stop")
    async def stop(self, queue_id: str) -> None:
        """
        Handle STOP command for given queue.

        - queue_id: queue_id of the playerqueue to handle the command.
        """
        if queue := self.get(queue_id):
            queue.stream_finished = None
            queue.end_of_track_reached = None
        # forward the actual stop command to the player provider
        if player_provider := self.mass.players.get_player_provider(queue_id):
            await player_provider.cmd_stop(queue_id)

    @api_command("player_queues/play")
    async def play(self, queue_id: str) -> None:
        """
        Handle PLAY command for given queue.

        - queue_id: queue_id of the playerqueue to handle the command.
        """
        # always fetch the underlying player so we can raise early if its not available
        player = self.mass.players.get(queue_id, True)
        if player.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        if self._queues[queue_id].state == PlayerState.PAUSED:
            # forward the actual stop command to the player provider
            if player_provider := self.mass.players.get_player_provider(queue_id):
                await player_provider.cmd_play(queue_id)
        else:
            await self.resume(queue_id)

    @api_command("player_queues/pause")
    async def pause(self, queue_id: str) -> None:
        """Handle PAUSE command for given queue.

        - queue_id: queue_id of the playerqueue to handle the command.
        """
        # always fetch the underlying player so we can raise early if its not available
        player = self.mass.players.get(queue_id, True)
        if player.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        if PlayerFeature.PAUSE not in player.supported_features:
            # if player does not support pause, we need to send stop
            await self.stop(queue_id)
            return
        # forward the actual stop command to the player provider
        if player_provider := self.mass.players.get_player_provider(queue_id):
            await player_provider.cmd_pause(queue_id)

    @api_command("player_queues/play_pause")
    async def play_pause(self, queue_id: str) -> None:
        """Toggle play/pause on given playerqueue.

        - queue_id: queue_id of the queue to handle the command.
        """
        if self._queues[queue_id].state == PlayerState.PLAYING:
            await self.pause(queue_id)
            return
        await self.play(queue_id)

    @api_command("player_queues/next")
    async def next(self, queue_id: str) -> None:
        """Handle NEXT TRACK command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        """
        # always fetch the underlying player so we can raise early if its not available
        player = self.mass.players.get(queue_id, True)
        if player.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        current_index = self._queues[queue_id].current_index
        if (next_index := self._get_next_index(queue_id, current_index, True)) is not None:
            await self.play_index(queue_id, next_index)

    @api_command("player_queues/previous")
    async def previous(self, queue_id: str) -> None:
        """Handle PREVIOUS TRACK command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        """
        # always fetch the underlying player so we can raise early if its not available
        player = self.mass.players.get(queue_id, True)
        if player.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        current_index = self._queues[queue_id].current_index
        if current_index is None:
            return
        await self.play_index(queue_id, max(current_index - 1, 0))

    @api_command("player_queues/skip")
    async def skip(self, queue_id: str, seconds: int = 10) -> None:
        """Handle SKIP command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        - seconds: number of seconds to skip in track. Use negative value to skip back.
        """
        # always fetch the underlying player so we can raise early if its not available
        player = self.mass.players.get(queue_id, True)
        if player.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        await self.seek(queue_id, self._queues[queue_id].elapsed_time + seconds)

    @api_command("player_queues/seek")
    async def seek(self, queue_id: str, position: int = 10) -> None:
        """Handle SEEK command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        - position: position in seconds to seek to in the current playing item.
        """
        # always fetch the underlying player so we can raise early if its not available
        player = self.mass.players.get(queue_id, True)
        if player.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        queue = self._queues[queue_id]
        assert queue.current_item, "No item loaded"
        assert queue.current_item.media_item.media_type == MediaType.TRACK
        assert queue.current_item.duration
        assert position < queue.current_item.duration
        await self.play_index(queue_id, queue.current_index, position)

    @api_command("player_queues/resume")
    async def resume(self, queue_id: str, fade_in: bool | None = None) -> None:
        """Handle RESUME command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        """
        queue = self._queues[queue_id]
        queue_items = self._queue_items[queue_id]
        resume_item = queue.current_item
        next_item = queue.next_item
        resume_pos = queue.elapsed_time
        if (
            resume_item
            and next_item
            and resume_item.duration
            and resume_pos > (resume_item.duration * 0.9)
        ):
            # track is already played for > 90% - skip to next
            resume_item = next_item
            resume_pos = 0
        elif not resume_item and queue.current_index is not None and len(queue_items) > 0:
            resume_item = self.get_item(queue_id, queue.current_index)
            resume_pos = 0
        elif not resume_item and queue.current_index is None and len(queue_items) > 0:
            # items available in queue but no previous track, start at 0
            resume_item = self.get_item(queue_id, 0)
            resume_pos = 0

        if resume_item is not None:
            resume_pos = resume_pos if resume_pos > 10 else 0
            fade_in = fade_in if fade_in is not None else resume_pos > 0
            if resume_item.media_type == MediaType.RADIO:
                # we're not able to skip in online radio so this is pointless
                resume_pos = 0
            await self.play_index(queue_id, resume_item.queue_item_id, resume_pos, fade_in)
        else:
            msg = f"Resume queue requested but queue {queue.display_name} is empty"
            raise QueueEmpty(msg)

    @api_command("player_queues/play_index")
    async def play_index(
        self,
        queue_id: str,
        index: int | str,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> None:
        """Play item at index (or item_id) X in queue."""
        queue = self._queues[queue_id]
        if isinstance(index, str):
            index = self.index_by_id(queue_id, index)
        queue_item = self.get_item(queue_id, index)
        if queue_item is None:
            msg = f"Unknown index/id: {index}"
            raise FileNotFoundError(msg)
        queue.current_index = index
        queue.index_in_buffer = index
        queue.flow_mode_start_index = index
        player_needs_flow_mode = await self.mass.config.get_player_config_value(
            queue_id, CONF_FLOW_MODE
        )
        next_index = self._get_next_index(queue_id, index, allow_repeat=False)
        queue.flow_mode = player_needs_flow_mode and next_index is not None
        queue.stream_finished = False
        queue.end_of_track_reached = False
        # get streamdetails - do this here to catch unavailable items early
        queue_item.streamdetails = await get_stream_details(
            self.mass, queue_item, seek_position=seek_position, fade_in=fade_in
        )
        # send play_media request to player
        await self.mass.players.play_media(
            player_id=queue_id,
            # transform into PlayerMedia to send to the actual player implementation
            media=self.player_media_from_queue_item(queue_item, queue.flow_mode),
        )

    # Interaction with player

    async def on_player_register(self, player: Player) -> None:
        """Register PlayerQueue for given player/queue id."""
        queue_id = player.player_id
        queue = None
        # try to restore previous state
        if prev_state := await self.mass.cache.get(f"queue.state.{queue_id}"):
            try:
                queue = PlayerQueue.from_cache(prev_state)
                prev_items = await self.mass.cache.get(f"queue.items.{queue_id}", default=[])
                queue_items = [QueueItem.from_cache(x) for x in prev_items]
            except Exception as err:
                self.logger.warning(
                    "Failed to restore the queue(items) for %s - %s",
                    player.display_name,
                    str(err),
                )
        if queue is None:
            queue = PlayerQueue(
                queue_id=queue_id,
                active=False,
                display_name=player.display_name,
                available=player.available,
                items=0,
            )
            queue_items = []

        self._queues[queue_id] = queue
        self._queue_items[queue_id] = queue_items
        # always call update to calculate state etc
        self.on_player_update(player, {})
        self.mass.signal_event(EventType.QUEUE_ADDED, object_id=queue_id, data=queue)

    def on_player_update(
        self,
        player: Player,
        changed_values: dict[str, tuple[Any, Any]],
    ) -> None:
        """
        Call when a PlayerQueue needs to be updated (e.g. when player updates).

        NOTE: This is called every second if the player is playing.
        """
        if player.player_id not in self._queues:
            # race condition
            return
        if player.announcement_in_progress:
            # do nothing while the announcement is in progress
            return
        queue_id = player.player_id
        player = self.mass.players.get(queue_id)
        queue = self._queues[queue_id]

        # basic properties
        queue.display_name = player.display_name
        queue.available = player.available
        queue.items = len(self._queue_items[queue_id])
        # determine if this queue is currently active for this player
        queue.active = player.powered and player.active_source == queue.queue_id
        if not queue.active:
            # return early if the queue is not active
            queue.state = PlayerState.IDLE
            if prev_state := self._prev_states.pop(queue_id, None):
                self.signal_update(queue_id)
            return
        # update current item from player report
        if queue.flow_mode:
            # flow mode active, calculate current item
            queue.current_index, queue.elapsed_time = self.__get_queue_stream_index(queue, player)
            queue.elapsed_time_last_updated = time.time()
        else:
            # queue is active and player has one of our tracks loaded, update state
            if item_id := self._parse_player_current_item_id(queue_id, player.current_item_id):
                queue.current_index = self.index_by_id(queue_id, item_id)
            if player.state == PlayerState.PLAYING:
                queue.elapsed_time = int(player.corrected_elapsed_time)
                queue.elapsed_time_last_updated = player.elapsed_time_last_updated

        # only update these attributes if the queue is active
        # and has an item loaded so we are able to resume it
        queue.state = player.state
        queue.current_item = self.get_item(queue_id, queue.current_index)
        queue.next_item = self._get_next_item(queue_id)
        # correct elapsed time when seeking
        if (
            queue.current_item
            and queue.current_item.streamdetails
            and queue.current_item.streamdetails.seek_position
            and player.state in (PlayerState.PLAYING, PlayerState.PAUSED)
            and not queue.flow_mode
        ):
            queue.elapsed_time += queue.current_item.streamdetails.seek_position

        # basic throttle: do not send state changed events if queue did not actually change
        prev_state = self._prev_states.get(
            queue_id,
            CompareState(
                queue_id=queue_id,
                state=PlayerState.IDLE,
                current_index=None,
                elapsed_time=0,
                stream_title=None,
            ),
        )
        new_state = CompareState(
            queue_id=queue_id,
            state=queue.state,
            current_index=queue.current_index,
            elapsed_time=queue.elapsed_time,
            stream_title=queue.current_item.streamdetails.stream_title
            if queue.current_item and queue.current_item.streamdetails
            else None,
        )
        changed_keys = get_changed_keys(prev_state, new_state)
        # return early if nothing changed
        if len(changed_keys) == 0:
            return
        # check if we've reached the end of (the current) track
        if (
            queue.current_item
            and (duration := queue.current_item.duration)
            and (duration - queue.elapsed_time) < 10
        ):
            queue.end_of_track_reached = True
        elif prev_state["current_index"] != new_state["current_index"]:
            queue.end_of_track_reached = False

        # handle enqueuing of next item to play
        if not queue.flow_mode or queue.stream_finished:
            self._check_enqueue_next(player, queue, prev_state, new_state)
        # do not send full updates if only time was updated
        if changed_keys == {"elapsed_time"}:
            self.mass.signal_event(
                EventType.QUEUE_TIME_UPDATED,
                object_id=queue_id,
                data=queue.elapsed_time,
            )
            self._prev_states[queue_id] = new_state
            return
        # handle player was playing and is now stopped
        # if player finished playing a track for 90%, mark current item as finished
        if (
            prev_state["state"] == "playing"
            and queue.state == PlayerState.IDLE
            and (
                queue.current_item
                and queue.current_item.duration
                and prev_state["elapsed_time"] > (queue.current_item.duration * 0.90)
            )
        ):
            queue.current_index += 1
            queue.current_item = None
            queue.next_item = None
        # signal update and store state
        self.signal_update(queue_id)
        self._prev_states[queue_id] = new_state
        # watch dynamic radio items refill if needed
        if "current_index" in changed_keys:
            if (
                queue.radio_source
                and queue.current_index
                and (queue.items - queue.current_index) < 5
            ):
                self.mass.create_task(self._fill_radio_tracks(queue_id))

    def on_player_remove(self, player_id: str) -> None:
        """Call when a player is removed from the registry."""
        self.mass.create_task(self.mass.cache.delete(f"queue.state.{player_id}"))
        self.mass.create_task(self.mass.cache.delete(f"queue.items.{player_id}"))
        self._queues.pop(player_id, None)
        self._queue_items.pop(player_id, None)

    async def preload_next_item(
        self,
        queue_id: str,
        current_item_id_or_index: str | int | None = None,
        allow_repeat: bool = True,
    ) -> QueueItem:
        """Call when a player wants to (pre)load the next item into the buffer.

        Raises QueueEmpty if there are no more tracks left.
        """
        queue = self.get(queue_id)
        if not queue:
            msg = f"PlayerQueue {queue_id} is not available"
            raise PlayerUnavailableError(msg)
        if current_item_id_or_index is None:
            cur_index = queue.index_in_buffer
        elif isinstance(current_item_id_or_index, str):
            cur_index = self.index_by_id(queue_id, current_item_id_or_index)
        else:
            cur_index = current_item_id_or_index
        idx = 0
        while True:
            next_index = self._get_next_index(queue_id, cur_index + idx, allow_repeat=allow_repeat)
            if next_index is None:
                raise QueueEmpty("No more tracks left in the queue.")
            next_item = self.get_item(queue_id, next_index)
            try:
                # Check if the QueueItem is playable. For example, YT Music returns Radio Items
                # that are not playable which will stop playback.
                next_item.streamdetails = await get_stream_details(
                    mass=self.mass, queue_item=next_item
                )
                # Lazy load the full MediaItem for the QueueItem, making sure to get the
                # maximum quality of thumbs
                next_item.media_item = await self.mass.music.get_item_by_uri(next_item.uri)
                break
            except MediaNotFoundError:
                # No stream details found, skip this QueueItem
                next_item = None
                idx += 1
        if next_item is None:
            raise QueueEmpty("No more (playable) tracks left in the queue.")
        return next_item

    # Main queue manipulation methods

    def load(
        self,
        queue_id: str,
        queue_items: list[QueueItem],
        insert_at_index: int = 0,
        keep_remaining: bool = True,
        keep_played: bool = True,
        shuffle: bool = False,
    ) -> None:
        """Load new items at index.

        - queue_id: id of the queue to process this request.
        - queue_items: a list of QueueItems
        - insert_at_index: insert the item(s) at this index
        - keep_remaining: keep the remaining items after the insert
        - shuffle: (re)shuffle the items after insert index
        """
        prev_items = self._queue_items[queue_id][:insert_at_index] if keep_played else []
        next_items = queue_items

        # if keep_remaining, append the old 'next' items
        if keep_remaining:
            next_items += self._queue_items[queue_id][insert_at_index:]

        # we set the original insert order as attribute so we can un-shuffle
        for index, item in enumerate(next_items):
            item.sort_index += insert_at_index + index
        # (re)shuffle the final batch if needed
        if shuffle:
            next_items = random.sample(next_items, len(next_items))
        self.update_items(queue_id, prev_items + next_items)

    def update_items(self, queue_id: str, queue_items: list[QueueItem]) -> None:
        """Update the existing queue items, mostly caused by reordering."""
        self._queue_items[queue_id] = queue_items
        self._queues[queue_id].items = len(self._queue_items[queue_id])
        self.signal_update(queue_id, True)

    # Helper methods

    def get_item(self, queue_id: str, item_id_or_index: int | str | None) -> QueueItem | None:
        """Get queue item by index or item_id."""
        if item_id_or_index is None:
            return None
        queue_items = self._queue_items[queue_id]
        if isinstance(item_id_or_index, int) and len(queue_items) > item_id_or_index:
            return queue_items[item_id_or_index]
        if isinstance(item_id_or_index, str):
            return next((x for x in queue_items if x.queue_item_id == item_id_or_index), None)
        return None

    def signal_update(self, queue_id: str, items_changed: bool = False) -> None:
        """Signal state changed of given queue."""
        queue = self._queues[queue_id]
        if items_changed:
            self.mass.signal_event(EventType.QUEUE_ITEMS_UPDATED, object_id=queue_id, data=queue)
            # save items in cache
            self.mass.create_task(
                self.mass.cache.set(
                    f"queue.items.{queue_id}",
                    [x.to_cache() for x in self._queue_items[queue_id]],
                )
            )

        # always send the base event
        self.mass.signal_event(EventType.QUEUE_UPDATED, object_id=queue_id, data=queue)
        # save state
        self.mass.create_task(
            self.mass.cache.set(
                f"queue.state.{queue_id}",
                queue.to_cache(),
            )
        )

    def index_by_id(self, queue_id: str, queue_item_id: str) -> int | None:
        """Get index by queue_item_id."""
        queue_items = self._queue_items[queue_id]
        for index, item in enumerate(queue_items):
            if item.queue_item_id == queue_item_id:
                return index
        return None

    def player_media_from_queue_item(self, queue_item: QueueItem, flow_mode: bool) -> PlayerMedia:
        """Parse PlayerMedia from QueueItem."""
        media = PlayerMedia(
            uri=self.mass.streams.resolve_stream_url(queue_item, flow_mode=flow_mode),
            media_type=MediaType.FLOW_STREAM if flow_mode else queue_item.media_type,
            title="Music Assistant" if flow_mode else queue_item.name,
            image_url=MASS_LOGO_ONLINE,
            duration=queue_item.duration,
            queue_id=queue_item.queue_id,
            queue_item_id=queue_item.queue_item_id,
        )
        if not flow_mode and queue_item.media_item:
            media.title = queue_item.media_item.name
            media.artist = getattr(queue_item.media_item, "artist_str", "")
            media.album = (
                album.name if (album := getattr(queue_item.media_item, "album", None)) else ""
            )
            if queue_item.image:
                media.image_url = self.mass.metadata.get_image_url(queue_item.image)
        return media

    def _get_next_index(
        self, queue_id: str, cur_index: int | None, is_skip: bool = False, allow_repeat: bool = True
    ) -> int | None:
        """
        Return the next index for the queue, accounting for repeat settings.

        Will return None if there are no (more) items in the queue.
        """
        queue = self._queues[queue_id]
        queue_items = self._queue_items[queue_id]
        if not queue_items or cur_index is None:
            # queue is empty
            return None
        # handle repeat single track
        if queue.repeat_mode == RepeatMode.ONE and not is_skip:
            return cur_index if allow_repeat else None
        # handle cur_index is last index of the queue
        if cur_index >= (len(queue_items) - 1):
            if allow_repeat and queue.repeat_mode == RepeatMode.ALL:
                # if repeat all is enabled, we simply start again from the beginning
                return 0
            return None
        # all other: just the next index
        return cur_index + 1

    def _get_next_item(self, queue_id: str, cur_index: int | None = None) -> QueueItem | None:
        """Return next QueueItem for given queue."""
        if (next_index := self._get_next_index(queue_id, cur_index)) is not None:
            return self.get_item(queue_id, next_index)
        return None

    async def _fill_radio_tracks(self, queue_id: str) -> None:
        """Fill a Queue with (additional) Radio tracks."""
        # we need to debounce, if we're called twice within a short timeframe
        debounce_key = f"fill_radio_{queue_id}"
        if getattr(self, debounce_key, None):
            return
        setattr(self, debounce_key, True)
        tracks = await self._get_radio_tracks(queue_id)
        # fill queue - filter out unavailable items
        queue_items = [QueueItem.from_media_item(queue_id, x) for x in tracks if x.available]
        self.load(
            queue_id,
            queue_items,
            insert_at_index=len(self._queue_items[queue_id]) + 1,
        )
        await asyncio.sleep(5)
        setattr(self, debounce_key, None)

    def _check_enqueue_next(
        self,
        player: Player,
        queue: PlayerQueue,
        prev_state: CompareState,
        new_state: CompareState,
    ) -> None:
        """Check if we need to enqueue the next item to the player itself."""
        if not queue.active:
            return
        if prev_state["state"] != PlayerState.PLAYING:
            return
        if (player := self.mass.players.get(queue.queue_id)) and player.announcement_in_progress:
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return
        current_item = self.get_item(queue.queue_id, queue.current_index)
        if not current_item:
            return  # guard, just in case something bad happened
        if not current_item.duration:
            return
        # NOTE: 'seconds_streamed' can actually be 0 if there was a stream error!
        if current_item.streamdetails and current_item.streamdetails.seconds_streamed is not None:
            duration = current_item.streamdetails.seconds_streamed
        else:
            duration = current_item.duration
        seconds_remaining = int(duration - player.corrected_elapsed_time)

        async def _enqueue_next(current_index: int, supports_enqueue: bool = False) -> None:
            if (
                player := self.mass.players.get(queue.queue_id)
            ) and player.announcement_in_progress:
                self.logger.warning("Ignore queue command: An announcement is in progress")
                return
            with suppress(QueueEmpty):
                next_item = await self.preload_next_item(queue.queue_id, current_index)
                if supports_enqueue:
                    await self.mass.players.enqueue_next_media(
                        player_id=player.player_id,
                        media=self.player_media_from_queue_item(next_item, queue.flow_mode),
                    )
                    return
                await self.play_index(queue.queue_id, next_item.queue_item_id)

        # handle queue fully played - clear it completely once the player stopped
        if (
            queue.stream_finished
            and queue.state == PlayerState.IDLE
            and self._get_next_index(queue.queue_id, queue.current_index) is None
        ):
            self.logger.debug("End of queue reached for %s", queue.display_name)
            self.clear(queue.queue_id)
            return

        # handle native enqueue next support of player
        if PlayerFeature.ENQUEUE_NEXT in player.supported_features:
            # we enqueue the next track after a new track
            # has started playing and (repeat) before the current track ends
            new_track_started = (
                new_state["state"] == PlayerState.PLAYING
                and prev_state["current_index"] != new_state["current_index"]
            )
            if (
                new_track_started
                or seconds_remaining == 15
                or int(player.corrected_elapsed_time) == 1
            ):
                self.mass.create_task(_enqueue_next(queue.current_index, True))
            return

        # player does not support enqueue next feature.
        # we wait for the player to stop after it reaches the end of the track
        if (
            (not queue.flow_mode or queue.repeat_mode == RepeatMode.ALL)
            # we have a couple of guards here to prevent the player starting
            # playback again when its stopped outside of MA's control
            and queue.stream_finished
            and queue.end_of_track_reached
            and queue.state == PlayerState.IDLE
        ):
            queue.stream_finished = None
            self.mass.create_task(_enqueue_next(queue.current_index, False))
            return

    async def _get_radio_tracks(self, queue_id: str) -> list[MediaItemType]:
        """Call the registered music providers for dynamic tracks."""
        queue = self._queues[queue_id]
        assert queue.radio_source, "No Radio item(s) loaded/active!"
        tracks: list[MediaItemType] = []
        # grab dynamic tracks for (all) source items
        # shuffle the source items, just in case
        for radio_item in random.sample(queue.radio_source, len(queue.radio_source)):
            ctrl = self.mass.music.get_controller(radio_item.media_type)
            tracks += await ctrl.dynamic_tracks(radio_item.item_id, radio_item.provider)
            # make sure we do not grab too much items
            if len(tracks) >= 50:
                break
        return tracks

    async def get_artist_tracks(self, artist: Artist) -> list[Track]:
        """Return tracks for given artist, based on user preference."""
        artist_items_conf = self.mass.config.get_raw_core_config_value(
            self.domain,
            CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
            ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE,
        )
        self.logger.debug(
            "Fetching tracks to play for artist %s",
            artist.name,
        )
        if artist_items_conf in ("library_tracks", "all_tracks"):
            all_items = await self.mass.music.artists.tracks(
                artist.item_id,
                artist.provider,
                in_library_only=artist_items_conf == "library_tracks",
            )
            random.shuffle(all_items)
            return all_items

        if artist_items_conf in ("library_album_tracks", "all_album_tracks"):
            all_items: list[Track] = []
            for library_album in await self.mass.music.artists.albums(
                artist.item_id,
                artist.provider,
                in_library_only=artist_items_conf == "library_album_tracks",
            ):
                for album_track in await self.mass.music.albums.tracks(
                    library_album.item_id, library_album.provider
                ):
                    if album_track not in all_items:
                        all_items.append(album_track)
            random.shuffle(all_items)
            return all_items

        return []

    async def get_album_tracks(self, album: Album) -> list[AlbumTrack]:
        """Return tracks for given album, based on user preference."""
        album_items_conf = self.mass.config.get_raw_core_config_value(
            self.domain,
            CONF_DEFAULT_ENQUEUE_SELECT_ALBUM,
            ENQUEUE_SELECT_ALBUM_DEFAULT_VALUE,
        )
        self.logger.debug(
            "Fetching tracks to play for album %s",
            album.name,
        )
        return await self.mass.music.albums.tracks(
            item_id=album.item_id,
            provider_instance_id_or_domain=album.provider,
            in_library_only=album_items_conf == "library_tracks",
        )

    def __get_queue_stream_index(self, queue: PlayerQueue, player: Player) -> tuple[int, int]:
        """Calculate current queue index and current track elapsed time."""
        # player is playing a constant stream so we need to do this the hard way
        queue_index = 0
        elapsed_time_queue = player.corrected_elapsed_time
        total_time = 0
        track_time = 0
        queue_items = self._queue_items[queue.queue_id]
        if queue_items and len(queue_items) > queue.flow_mode_start_index:
            # start_index: holds the position from which the flow stream started
            queue_index = queue.flow_mode_start_index
            queue_track = None
            while len(queue_items) > queue_index:
                # keep enumerating the queue tracks to find current track
                # starting from the start index
                queue_track = queue_items[queue_index]
                if not queue_track.streamdetails:
                    track_time = elapsed_time_queue - total_time
                    break
                track_duration = (
                    # NOTE: 'seconds_streamed' can actually be 0 if there was a stream error!
                    queue_track.streamdetails.seconds_streamed
                    if queue_track.streamdetails.seconds_streamed is not None
                    else (
                        queue_track.streamdetails.duration
                        or queue_track.duration
                        or FALLBACK_DURATION
                    )
                )
                if elapsed_time_queue > (track_duration + total_time):
                    # total elapsed time is more than (streamed) track duration
                    # move index one up
                    total_time += track_duration
                    queue_index += 1
                else:
                    # no more seconds left to divide, this is our track
                    # account for any seeking by adding the skipped/seeked seconds
                    track_sec_skipped = queue_track.streamdetails.seek_position
                    track_time = elapsed_time_queue + track_sec_skipped - total_time
                    break
        return queue_index, track_time

    def _parse_player_current_item_id(self, queue_id: str, current_item_id: str) -> str | None:
        """Parse QueueItem ID from Player's current url."""
        if not current_item_id:
            return None
        if queue_id in current_item_id:
            # try to extract the item id from either a url or queue_id/item_id combi
            current_item_id = current_item_id.rsplit("/")[-1].split(".")[0]
        if self.get_item(queue_id, current_item_id):
            return current_item_id
        return None
