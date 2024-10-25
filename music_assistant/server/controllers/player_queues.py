"""
MusicAssistant Player Queues Controller.

Handles all logic to PLAY Media Items, provided by Music Providers to supported players.

It is loosely coupled to the MusicAssistant Music Controller and Player Controller.
A Music Assistant Player always has a PlayerQueue associated with it
which holds the queue items and state.

The PlayerQueue is in that case the active source of the player,
but it can also be something else, hence the loose coupling.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import TYPE_CHECKING, Any, TypedDict

from music_assistant.common.helpers.util import get_changed_keys
from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant.common.models.enums import (
    CacheCategory,
    ConfigEntryType,
    EventType,
    MediaType,
    PlayerState,
    ProviderFeature,
    QueueOption,
    RepeatMode,
)
from music_assistant.common.models.errors import (
    InvalidCommand,
    MediaNotFoundError,
    MusicAssistantError,
    PlayerUnavailableError,
    QueueEmpty,
    UnsupportedFeaturedException,
)
from music_assistant.common.models.media_items import (
    AudioFormat,
    MediaItemType,
    Playlist,
    media_from_dict,
)
from music_assistant.common.models.player import PlayerMedia
from music_assistant.common.models.player_queue import PlayerQueue
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.constants import CONF_CROSSFADE, CONF_FLOW_MODE, MASS_LOGO_ONLINE
from music_assistant.server.helpers.api import api_command
from music_assistant.server.helpers.audio import get_stream_details
from music_assistant.server.helpers.throttle_retry import BYPASS_THROTTLER
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
RADIO_TRACK_MAX_DURATION_SECS = 20 * 60  # 20 minutes


class CompareState(TypedDict):
    """Simple object where we store the (previous) state of a queue.

    Used for compare actions.
    """

    queue_id: str
    state: PlayerState
    current_index: int | None
    elapsed_time: int
    stream_title: str | None
    content_type: str | None


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
                label="Default enqueue option for Radio item(s).",
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
            return self.get(player.active_source) or self.get(player_id)
        return self.get(player_id)

    # Queue commands

    @api_command("player_queues/shuffle")
    def set_shuffle(self, queue_id: str, shuffle_enabled: bool) -> None:
        """Configure shuffle setting on the the queue."""
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

    @api_command("player_queues/dont_stop_the_music")
    def set_dont_stop_the_music(self, queue_id: str, dont_stop_the_music_enabled: bool) -> None:
        """Configure Don't stop the music setting on the queue."""
        providers_available_with_similar_tracks = any(
            ProviderFeature.SIMILAR_TRACKS in provider.supported_features
            for provider in self.mass.music.providers
        )
        if dont_stop_the_music_enabled and not providers_available_with_similar_tracks:
            raise UnsupportedFeaturedException(
                "Don't stop the music is not supported by any of the available music providers"
            )
        queue = self._queues[queue_id]
        queue.dont_stop_the_music_enabled = dont_stop_the_music_enabled
        self.signal_update(queue_id=queue_id)
        # if this happens to be the last track in the queue, fill the radio source
        if (
            queue.dont_stop_the_music_enabled
            and queue.enqueued_media_items
            and queue.current_index is not None
            and (queue.items - queue.current_index) <= 1
        ):
            queue.radio_source = queue.enqueued_media_items
            task_id = f"fill_radio_tracks_{queue_id}"
            self.mass.call_later(5, self._fill_radio_tracks, queue_id, task_id=task_id)

    @api_command("player_queues/repeat")
    def set_repeat(self, queue_id: str, repeat_mode: RepeatMode) -> None:
        """Configure repeat setting on the the queue."""
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
        # we use a contextvar to bypass the throttler for this asyncio task/context
        # this makes sure that playback has priority over other requests that may be
        # happening in the background
        BYPASS_THROTTLER.set(True)
        queue = self._queues[queue_id]
        # always fetch the underlying player so we can raise early if its not available
        queue_player = self.mass.players.get(queue_id, True)
        if queue_player.announcement_in_progress:
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
        # Clear the 'enqueued media item' list when a new queue is requested
        if option not in (QueueOption.ADD, QueueOption.NEXT):
            queue.enqueued_media_items.clear()

        tracks: list[MediaItemType] = []
        radio_source: list[MediaItemType] = []
        first_track_seen: bool = False
        for item in media:
            try:
                # parse provided uri into a MA MediaItem or Basic QueueItem from URL
                if isinstance(item, str):
                    media_item = await self.mass.music.get_item_by_uri(item)
                elif isinstance(item, dict):
                    media_item = media_from_dict(item)
                else:
                    media_item = item

                # Save requested media item to play on the queue so we can use it as a source
                # for Don't stop the music. Use FIFO list to keep track of the last 10 played items
                if media_item.media_type in (
                    MediaType.TRACK,
                    MediaType.ALBUM,
                    MediaType.PLAYLIST,
                    MediaType.ARTIST,
                ):
                    queue.enqueued_media_items.append(media_item)
                    if len(queue.enqueued_media_items) > 10:
                        queue.enqueued_media_items.pop(0)

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
                    tracks += await self.get_playlist_tracks(media_item, start_item)
                    self.mass.create_task(
                        self.mass.music.mark_item_played(
                            media_item.media_type, media_item.item_id, media_item.provider
                        )
                    )
                elif media_item.media_type == MediaType.ARTIST:
                    tracks += await self.get_artist_tracks(media_item)
                    self.mass.create_task(
                        self.mass.music.mark_item_played(
                            media_item.media_type, media_item.item_id, media_item.provider
                        )
                    )
                elif media_item.media_type == MediaType.ALBUM:
                    tracks += await self.get_album_tracks(media_item, start_item)
                    self.mass.create_task(
                        self.mass.music.mark_item_played(
                            media_item.media_type, media_item.item_id, media_item.provider
                        )
                    )
                else:
                    # single track or radio item
                    tracks += [media_item]

            except MusicAssistantError as err:
                # invalid MA uri or item not found error
                self.logger.warning("Skipping %s: %s", item, str(err))

        # overwrite or append radio source items
        if option not in (QueueOption.ADD, QueueOption.NEXT):
            queue.radio_source = radio_source
        else:
            queue.radio_source += radio_source
        # Use collected media items to calculate the radio if radio mode is on
        if radio_mode:
            tracks = await self._get_radio_tracks(queue_id=queue_id, is_initial_radio_mode=True)

        # only add valid/available items
        queue_items = [QueueItem.from_media_item(queue_id, x) for x in tracks if x and x.available]

        if not queue_items:
            if first_track_seen:
                # edge case: playlist with only one track
                return
            raise MediaNotFoundError("No playable items found")

        # load the items into the queue
        if queue.state in (PlayerState.PLAYING, PlayerState.PAUSED):
            cur_index = queue.index_in_buffer or 0
        else:
            cur_index = queue.current_index or 0
        insert_at_index = cur_index + 1 if self._queue_items.get(queue_id) else 0
        # Radio modes are already shuffled in a pattern we would like to keep.
        shuffle = queue.shuffle_enabled and len(queue_items) > 1 and not radio_mode

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
        queue = self._queues[queue_id]
        queue.radio_source = []
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
        if (queue := self.get(queue_id)) and queue.active:
            queue.resume_pos = queue.corrected_elapsed_time
        # forward the actual command to the player provider
        if player_provider := self.mass.players.get_player_provider(queue.queue_id):
            await player_provider.cmd_stop(queue_id)

    @api_command("player_queues/play")
    async def play(self, queue_id: str) -> None:
        """
        Handle PLAY command for given queue.

        - queue_id: queue_id of the playerqueue to handle the command.
        """
        queue_player: Player = self.mass.players.get(queue_id, True)
        if (
            (queue := self._queues.get(queue_id))
            and queue.active
            and queue_player.state == PlayerState.PAUSED
        ):
            # forward the actual play/unpause command to the player provider
            if player_provider := self.mass.players.get_player_provider(queue.queue_id):
                await player_provider.cmd_play(queue_id)
                return
        # player is not paused, perform resume instead
        await self.resume(queue_id)

    @api_command("player_queues/pause")
    async def pause(self, queue_id: str) -> None:
        """Handle PAUSE command for given queue.

        - queue_id: queue_id of the playerqueue to handle the command.
        """
        if queue := self._queues.get(queue_id):
            queue.resume_pos = queue.corrected_elapsed_time
        # forward the actual command to the player controller
        await self.mass.players.cmd_pause(queue_id)

    @api_command("player_queues/play_pause")
    async def play_pause(self, queue_id: str) -> None:
        """Toggle play/pause on given playerqueue.

        - queue_id: queue_id of the queue to handle the command.
        """
        if (queue := self._queues.get(queue_id)) and queue.state == PlayerState.PLAYING:
            await self.pause(queue_id)
            return
        await self.play(queue_id)

    @api_command("player_queues/next")
    async def next(self, queue_id: str) -> None:
        """Handle NEXT TRACK command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        """
        if (queue := self.get(queue_id)) is None or not queue.active:
            # TODO: forward to underlying player if not active
            return
        idx = self._queues[queue_id].current_index
        while True:
            try:
                if (next_index := self._get_next_index(queue_id, idx, True)) is not None:
                    await self.play_index(queue_id, next_index, debounce=True)
                break
            except MediaNotFoundError:
                self.logger.warning(
                    "Failed to fetch next track for queue %s - trying next item", queue.display_name
                )
                idx += 1

    @api_command("player_queues/previous")
    async def previous(self, queue_id: str) -> None:
        """Handle PREVIOUS TRACK command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        """
        if (queue := self.get(queue_id)) is None or not queue.active:
            # TODO: forward to underlying player if not active
            return
        current_index = self._queues[queue_id].current_index
        if current_index is None:
            return
        await self.play_index(queue_id, max(current_index - 1, 0), debounce=True)

    @api_command("player_queues/skip")
    async def skip(self, queue_id: str, seconds: int = 10) -> None:
        """Handle SKIP command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        - seconds: number of seconds to skip in track. Use negative value to skip back.
        """
        if (queue := self.get(queue_id)) is None or not queue.active:
            # TODO: forward to underlying player if not active
            return
        await self.seek(queue_id, self._queues[queue_id].elapsed_time + seconds)

    @api_command("player_queues/seek")
    async def seek(self, queue_id: str, position: int = 10) -> None:
        """Handle SEEK command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        - position: position in seconds to seek to in the current playing item.
        """
        if not (queue := self.get(queue_id)):
            return
        queue_player: Player = self.mass.players.get(queue_id, True)
        if not queue.current_item:
            raise InvalidCommand(f"Queue {queue_player.display_name} has no item(s) loaded.")
        if (
            queue.current_item.media_item.media_type != MediaType.TRACK
            or not queue.current_item.duration
        ):
            raise InvalidCommand("Can not seek on non track items.")
        position = max(0, int(position))
        if position > queue.current_item.duration:
            raise InvalidCommand("Can not seek outside of duration range.")
        await self.play_index(queue_id, queue.current_index, seek_position=position)

    @api_command("player_queues/resume")
    async def resume(self, queue_id: str, fade_in: bool | None = None) -> None:
        """Handle RESUME command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        """
        queue = self._queues[queue_id]
        queue_items = self._queue_items[queue_id]
        resume_item = queue.current_item
        if queue.state == PlayerState.PLAYING:
            # resume requested while already playing,
            # use current position as resume position
            resume_pos = queue.corrected_elapsed_time
        else:
            resume_pos = queue.resume_pos

        if not resume_item and queue.current_index is not None and len(queue_items) > 0:
            resume_item = self.get_item(queue_id, queue.current_index)
            resume_pos = 0
        elif not resume_item and queue.current_index is None and len(queue_items) > 0:
            # items available in queue but no previous track, start at 0
            resume_item = self.get_item(queue_id, 0)
            resume_pos = 0

        if resume_item is not None:
            resume_pos = resume_pos if resume_pos > 10 else 0
            queue_player = self.mass.players.get(queue_id)
            if fade_in is None and not queue_player.powered:
                fade_in = resume_pos > 0
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
        debounce: bool = False,
    ) -> None:
        """Play item at index (or item_id) X in queue."""
        queue = self._queues[queue_id]
        queue.resume_pos = 0
        if isinstance(index, str):
            index = self.index_by_id(queue_id, index)
        queue_item = self.get_item(queue_id, index)
        if queue_item is None:
            msg = f"Unknown index/id: {index}"
            raise FileNotFoundError(msg)
        queue.current_index = index
        queue.index_in_buffer = index
        queue.flow_mode_stream_log = []
        queue.flow_mode = await self.mass.config.get_player_config_value(queue_id, CONF_FLOW_MODE)
        next_index = self._get_next_index(queue_id, index, allow_repeat=False)
        queue.current_item = queue_item
        queue.next_track_enqueued = None
        self.signal_update(queue_id)

        # work out if we are playing an album and if we should prefer album loudness
        if (
            next_index is not None
            and (next_item := self.get_item(queue_id, next_index))
            and (
                queue_item.media_item
                and hasattr(queue_item.media_item, "album")
                and hasattr(next_item.media_item, "album")
                and queue_item.media_item.album
                and next_item.media_item
                and next_item.media_item.album
                and queue_item.media_item.album.item_id == next_item.media_item.album.item_id
            )
        ):
            prefer_album_loudness = True
        else:
            prefer_album_loudness = False

        # get streamdetails - do this here to catch unavailable items early
        queue_item.streamdetails = await get_stream_details(
            self.mass,
            queue_item,
            seek_position=seek_position,
            fade_in=fade_in,
            prefer_album_loudness=prefer_album_loudness,
        )

        # allow stripping silence from the end of the track if crossfade is enabled
        # this will allow for smoother crossfades
        if await self.mass.config.get_player_config_value(queue_id, CONF_CROSSFADE):
            queue_item.streamdetails.strip_silence_end = True
        # send play_media request to player
        # NOTE that we debounce this a bit to account for someone hitting the next button
        # like a madman. This will prevent the player from being overloaded with requests.
        self.mass.call_later(
            1 if debounce else 0.1,
            self.mass.players.play_media,
            player_id=queue_id,
            # transform into PlayerMedia to send to the actual player implementation
            media=self.player_media_from_queue_item(queue_item, queue.flow_mode),
            task_id=f"play_media_{queue_id}",
        )
        self.signal_update(queue_id)

    @api_command("player_queues/transfer")
    async def transfer_queue(
        self,
        source_queue_id: str,
        target_queue_id: str,
        auto_play: bool | None = None,
    ) -> None:
        """Transfer queue to another queue."""
        if not (source_queue := self.get(source_queue_id)):
            raise PlayerUnavailableError("Queue {source_queue_id} is not available")
        if not (target_queue := self.get(target_queue_id)):
            raise PlayerUnavailableError("Queue {target_queue_id} is not available")
        if auto_play is None:
            auto_play = source_queue.state == PlayerState.PLAYING

        target_player = self.mass.players.get(target_queue_id)
        if target_player.active_group or target_player.synced_to:
            # edge case: the user wants to move playback from the group as a whole, to a single
            # player in the group or it is grouped and the command targeted at the single player.
            # We need to dissolve the group first.
            await self.mass.players.cmd_power(
                target_player.active_group or target_player.synced_to, False
            )
            await asyncio.sleep(3)

        source_items = self._queue_items[source_queue_id]
        target_queue.repeat_mode = source_queue.repeat_mode
        target_queue.shuffle_enabled = source_queue.shuffle_enabled
        target_queue.dont_stop_the_music_enabled = source_queue.dont_stop_the_music_enabled
        target_queue.radio_source = source_queue.radio_source
        target_queue.resume_pos = source_queue.elapsed_time
        target_queue.current_index = source_queue.current_index
        if source_queue.current_item:
            target_queue.current_item = source_queue.current_item
            target_queue.current_item.queue_id = target_queue_id
        self.clear(source_queue_id)

        self.load(target_queue_id, source_items, keep_remaining=False, keep_played=False)
        for item in source_items:
            item.queue_id = target_queue_id
        self.update_items(target_queue_id, source_items)
        if auto_play:
            await self.resume(target_queue_id)

    # Interaction with player

    async def on_player_register(self, player: Player) -> None:
        """Register PlayerQueue for given player/queue id."""
        queue_id = player.player_id
        queue = None
        # try to restore previous state
        if prev_state := await self.mass.cache.get(
            "state", category=CacheCategory.PLAYER_QUEUE_STATE, base_key=queue_id
        ):
            try:
                queue = PlayerQueue.from_cache(prev_state)
                prev_items = await self.mass.cache.get(
                    "items",
                    default=[],
                    category=CacheCategory.PLAYER_QUEUE_STATE,
                    base_key=queue_id,
                )
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
                dont_stop_the_music_enabled=False,
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
            queue.current_index, queue.elapsed_time = self._get_flow_queue_stream_index(
                queue, player
            )
            queue.elapsed_time_last_updated = time.time()
        else:
            # queue is active and player has one of our tracks loaded, update state
            if item_id := self._parse_player_current_item_id(queue_id, player):
                queue.current_index = self.index_by_id(queue_id, item_id)
            if player.state in (PlayerState.PLAYING, PlayerState.PAUSED):
                queue.elapsed_time = int(player.corrected_elapsed_time or 0)
                queue.elapsed_time_last_updated = player.elapsed_time_last_updated or 0

        # only update these attributes if the queue is active
        # and has an item loaded so we are able to resume it
        queue.state = player.state or PlayerState.IDLE
        queue.current_item = self.get_item(queue_id, queue.current_index)
        queue.next_item = (
            self.get_item(queue_id, queue.next_track_enqueued)
            if queue.next_track_enqueued
            else self._get_next_item(queue_id, queue.current_index)
        )

        # correct elapsed time when seeking
        if (
            queue.current_item
            and queue.current_item.streamdetails
            and queue.current_item.streamdetails.seek_position
            and player.state in (PlayerState.PLAYING, PlayerState.PAUSED)
            and not queue.flow_mode
        ):
            queue.elapsed_time += queue.current_item.streamdetails.seek_position

        # enqueue next track if needed
        if (
            queue.state == PlayerState.PLAYING
            and queue.next_item is not None
            and not queue.next_track_enqueued
            and queue.corrected_elapsed_time > 2
        ):
            self._check_enqueue_next(queue)

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
            content_type=queue.current_item.streamdetails.audio_format.output_format_str
            if queue.current_item and queue.current_item.streamdetails
            else None,
        )
        changed_keys = get_changed_keys(prev_state, new_state)
        # return early if nothing changed
        if len(changed_keys) == 0:
            return

        # do not send full updates if only time was updated
        if changed_keys == {"elapsed_time"}:
            self.mass.signal_event(
                EventType.QUEUE_TIME_UPDATED,
                object_id=queue_id,
                data=queue.elapsed_time,
            )
            self._prev_states[queue_id] = new_state
            return

        # signal update and store state
        self.signal_update(queue_id)
        self._prev_states[queue_id] = new_state

        # detect change in current index to report that a item has been played
        end_of_queue_reached = (
            prev_state["state"] == PlayerState.PLAYING
            and new_state["state"] == PlayerState.IDLE
            and queue.current_item is not None
            and queue.next_item is None
        )
        if (
            prev_state["current_index"] is not None
            and (prev_state["current_index"] != new_state["current_index"] or end_of_queue_reached)
            and (queue_item := self.get_item(queue_id, prev_state["current_index"]))
            and (stream_details := queue_item.streamdetails)
        ):
            seconds_streamed = prev_state["elapsed_time"]
            if music_prov := self.mass.get_provider(stream_details.provider):
                if seconds_streamed > 10:
                    self.mass.create_task(music_prov.on_streamed(stream_details, seconds_streamed))
            if queue_item.media_item and seconds_streamed > 10:
                # signal 'media item played' event,
                # which is useful for plugins that want to do scrobbling
                self.mass.signal_event(
                    EventType.MEDIA_ITEM_PLAYED,
                    object_id=queue_item.media_item.uri,
                    data=round(seconds_streamed, 2),
                )

        if end_of_queue_reached:
            # end of queue reached, clear items
            self.mass.call_later(
                5, self._check_clear_queue, queue, task_id=f"clear_queue_{queue_id}"
            )

        # clear 'next track enqueued' flag if new track is loaded
        if prev_state["current_index"] != new_state["current_index"]:
            queue.next_track_enqueued = None

        # watch dynamic radio items refill if needed
        if "current_index" in changed_keys:
            if (
                queue.dont_stop_the_music_enabled
                and queue.enqueued_media_items
                and queue.current_index is not None
                and (queue.items - queue.current_index) <= 1
            ):
                # We have received the last item in the queue and Don't stop the music is enabled
                # set the played media item(s) as radio items (which will refill the queue)
                # note that this will fail if there are no media items for which we have
                # a dynamic radio source.
                queue.radio_source = queue.enqueued_media_items
            if (
                queue.radio_source
                and queue.current_index is not None
                and (queue.items - queue.current_index) < 5
            ):
                task_id = f"fill_radio_tracks_{queue_id}"
                self.mass.call_later(5, self._fill_radio_tracks, queue_id, task_id=task_id)

    def on_player_remove(self, player_id: str) -> None:
        """Call when a player is removed from the registry."""
        self.mass.create_task(self.mass.cache.delete(f"queue.state.{player_id}"))
        self.mass.create_task(self.mass.cache.delete(f"queue.items.{player_id}"))
        self._queues.pop(player_id, None)
        self._queue_items.pop(player_id, None)

    async def load_next_item(
        self,
        queue_id: str,
        current_item_id_or_index: str | int | None = None,
    ) -> QueueItem:
        """Call when a player wants to (pre)load the next item into the buffer.

        Raises QueueEmpty if there are no more tracks left.
        """
        queue = self.get(queue_id)
        if not queue:
            msg = f"PlayerQueue {queue_id} is not available"
            raise PlayerUnavailableError(msg)
        if current_item_id_or_index is None:
            cur_index = queue.index_in_buffer or queue.current_index or 0
        elif isinstance(current_item_id_or_index, str):
            cur_index = self.index_by_id(queue_id, current_item_id_or_index)
        else:
            cur_index = current_item_id_or_index
        idx = 0
        while True:
            next_item: QueueItem | None = None
            next_index = self._get_next_index(queue_id, cur_index + idx)
            if next_index is None:
                raise QueueEmpty("No more tracks left in the queue.")
            queue_item = self.get_item(queue_id, next_index)
            if queue_item is None:
                raise QueueEmpty("No more tracks left in the queue.")

            # work out if we are playing an album and if we should prefer album loudness
            if (
                next_index is not None
                and (next_item := self.get_item(queue_id, next_index))
                and (
                    queue_item.media_item
                    and hasattr(queue_item.media_item, "album")
                    and queue_item.media_item.album
                    and next_item.media_item
                    and hasattr(next_item.media_item, "album")
                    and next_item.media_item.album
                    and queue_item.media_item.album.item_id == next_item.media_item.album.item_id
                )
            ):
                prefer_album_loudness = True
            else:
                prefer_album_loudness = False

            try:
                # Check if the QueueItem is playable. For example, YT Music returns Radio Items
                # that are not playable which will stop playback.
                queue_item.streamdetails = await get_stream_details(
                    mass=self.mass,
                    queue_item=queue_item,
                    prefer_album_loudness=prefer_album_loudness,
                )
                # Preload the full MediaItem for the QueueItem, making sure to get the
                # maximum quality of thumbs
                if queue_item.media_item:
                    queue_item.media_item = await self.mass.music.get_item_by_uri(queue_item.uri)
                # allow stripping silence from the begin/end of the track if crossfade is enabled
                # this will allow for (much) smoother crossfades
                if await self.mass.config.get_player_config_value(queue_id, CONF_CROSSFADE):
                    queue_item.streamdetails.strip_silence_end = True
                    queue_item.streamdetails.strip_silence_begin = True
                # we're all set, this is our next item
                next_item = queue_item
                break
            except MediaNotFoundError:
                # No stream details found, skip this QueueItem
                self.logger.debug("Skipping unplayable item: %s", next_item)
                # we need to set a fake streamdetails object on the item
                # otherwise our flow mode logic will break which
                # calculates where we are in the queue
                playlog = queue_item.streamdetails.play_log if queue_item.streamdetails else []
                playlog.append(0.0)
                queue_item.streamdetails = StreamDetails(
                    provider=queue_item.media_item.provider if queue_item.media_item else "unknown",
                    item_id=queue_item.media_item.item_id if queue_item.media_item else "unknown",
                    audio_format=AudioFormat(),
                    media_type=queue_item.media_type,
                    seconds_streamed=0,
                )
                idx += 1
        if next_item is None:
            raise QueueEmpty("No more (playable) tracks left in the queue.")
        return next_item

    def track_loaded_in_buffer(self, queue_id: str, item_id: str) -> None:
        """Call when a player has (started) loading a track in the buffer."""
        queue = self.get(queue_id)
        if not queue:
            msg = f"PlayerQueue {queue_id} is not available"
            raise PlayerUnavailableError(msg)
        # store the index of the item that is currently (being) loaded in the buffer
        # which helps us a bit to determine how far the player has buffered ahead
        queue.index_in_buffer = self.index_by_id(queue_id, item_id)
        if queue.flow_mode:
            return  # nothing to do when flow mode is active
        self.signal_update(queue_id)

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
        self._queues[queue_id].next_track_enqueued = None

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
                    "items",
                    [x.to_cache() for x in self._queue_items[queue_id]],
                    category=CacheCategory.PLAYER_QUEUE_STATE,
                    base_key=queue_id,
                )
            )
        # always send the base event
        self.mass.signal_event(EventType.QUEUE_UPDATED, object_id=queue_id, data=queue)
        # save state
        self.mass.create_task(
            self.mass.cache.set(
                "state",
                queue.to_cache(),
                category=CacheCategory.PLAYER_QUEUE_STATE,
                base_key=queue_id,
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

    async def get_album_tracks(self, album: Album, start_item: str | None) -> list[Track]:
        """Return tracks for given album, based on user preference."""
        album_items_conf = self.mass.config.get_raw_core_config_value(
            self.domain,
            CONF_DEFAULT_ENQUEUE_SELECT_ALBUM,
            ENQUEUE_SELECT_ALBUM_DEFAULT_VALUE,
        )
        result: list[Track] = []
        start_item_found = False
        self.logger.debug(
            "Fetching tracks to play for album %s",
            album.name,
        )
        for album_track in await self.mass.music.albums.tracks(
            item_id=album.item_id,
            provider_instance_id_or_domain=album.provider,
            in_library_only=album_items_conf == "library_tracks",
        ):
            if not album_track.available:
                continue
            if start_item in (album_track.item_id, album_track.uri):
                start_item_found = True
            if start_item is not None and not start_item_found:
                continue
            result.append(album_track)
        return result

    async def get_playlist_tracks(self, playlist: Playlist, start_item: str | None) -> list[Track]:
        """Return tracks for given playlist, based on user preference."""
        result: list[Track] = []
        start_item_found = False
        self.logger.debug(
            "Fetching tracks to play for playlist %s",
            playlist.name,
        )
        # TODO: Handle other sort options etc.
        async for playlist_track in self.mass.music.playlists.tracks(
            playlist.item_id, playlist.provider
        ):
            if not playlist_track.available:
                continue
            if start_item in (playlist_track.item_id, playlist_track.uri):
                start_item_found = True
            if start_item is not None and not start_item_found:
                continue
            result.append(playlist_track)
        return result

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
        tracks = await self._get_radio_tracks(queue_id=queue_id, is_initial_radio_mode=False)
        # fill queue - filter out unavailable items
        queue_items = [QueueItem.from_media_item(queue_id, x) for x in tracks if x.available]
        self.load(
            queue_id,
            queue_items,
            insert_at_index=len(self._queue_items[queue_id]) + 1,
        )

    def _check_enqueue_next(self, queue: PlayerQueue) -> None:
        """Enqueue the next item in the queue (if needed)."""
        if queue.flow_mode:
            return
        if queue.next_item is None:
            return
        if queue.next_track_enqueued == queue.next_item.queue_item_id:
            return

        async def _enqueue_next():
            next_item = await self.load_next_item(queue.queue_id, queue.current_index)
            queue.next_track_enqueued = next_item.queue_item_id
            await self.mass.players.enqueue_next_media(
                player_id=queue.queue_id,
                media=self.player_media_from_queue_item(next_item, False),
            )

        self.mass.create_task(_enqueue_next())

    async def _get_radio_tracks(
        self, queue_id: str, is_initial_radio_mode: bool = False
    ) -> list[Track]:
        """Call the registered music providers for dynamic tracks."""
        queue = self._queues[queue_id]
        if not queue.radio_source:
            # this may happen during race conditions as this method is called delayed
            return None
        available_base_tracks: list[Track] = []
        base_track_sample_size = 5
        # Grab all the available base tracks based on the selected source items.
        # shuffle the source items, just in case
        for radio_item in random.sample(queue.radio_source, len(queue.radio_source)):
            ctrl = self.mass.music.get_controller(radio_item.media_type)
            try:
                available_base_tracks += [
                    track
                    for track in await ctrl.dynamic_base_tracks(
                        radio_item.item_id, radio_item.provider
                    )
                    # Avoid duplicate base tracks
                    if track not in available_base_tracks
                ]
            except UnsupportedFeaturedException:
                self.logger.debug(
                    "Skip loading radio items for %s: - "
                    "Provider %s does not support dynamic (base) tracks",
                    radio_item.uri,
                    radio_item.provider,
                )
        # Sample tracks from the base tracks, which will be used to calculate the dynamic ones
        base_tracks = random.sample(
            available_base_tracks, min(base_track_sample_size, len(available_base_tracks))
        )
        # Use a set to avoid duplicate dynamic tracks
        dynamic_tracks: set[Track] = set()
        track_ctrl = self.mass.music.get_controller(MediaType.TRACK)
        # Use base tracks + Trackcontroller to obtain similar tracks for every base Track
        for base_track in base_tracks:
            [
                dynamic_tracks.add(track)
                for track in await track_ctrl.get_provider_similar_tracks(
                    base_track.item_id, base_track.provider
                )
                if track not in base_tracks
                # Ignore tracks that are too long for radio mode, e.g. mixes
                and track.duration <= RADIO_TRACK_MAX_DURATION_SECS
            ]
            if len(dynamic_tracks) >= 50:
                break
        queue_tracks: list[Track] = []
        dynamic_tracks = list(dynamic_tracks)
        # Only include the sampled base tracks when the radio mode is first initialized
        if is_initial_radio_mode:
            queue_tracks += [base_tracks[0]]
            # Exhaust base tracks with the pattern of BDDBDDBDD (1 base track + 2 dynamic tracks)
            if len(base_tracks) > 1:
                for base_track in base_tracks[1:]:
                    queue_tracks += [base_track]
                    queue_tracks += random.sample(dynamic_tracks, 2)
        # Add dynamic tracks to the queue, make sure to exclude already picked tracks
        remaining_dynamic_tracks = [t for t in dynamic_tracks if t not in queue_tracks]
        queue_tracks += random.sample(
            remaining_dynamic_tracks, min(len(remaining_dynamic_tracks), 25)
        )
        return queue_tracks

    async def _check_clear_queue(self, queue: PlayerQueue) -> None:
        """Check if the queue should be cleared after the current item."""
        for _ in range(5):
            await asyncio.sleep(1)
            if queue.state != PlayerState.IDLE:
                return
            if queue.next_item is not None:
                return
            if not (queue.current_index >= len(self._queue_items[queue.queue_id]) - 1):
                return
        self.logger.info("End of queue reached, clearing items")
        self.clear(queue.queue_id)

    def _get_flow_queue_stream_index(
        self, queue: PlayerQueue, player: Player
    ) -> tuple[int | None, int]:
        """Calculate current queue index and current track elapsed time when flow mode is active."""
        elapsed_time_queue_total = player.corrected_elapsed_time or 0
        if queue.current_index is None:
            return None, elapsed_time_queue_total

        # For each track that has been streamed/buffered to the player,
        # a playlog entry will be created with the queue item id
        # and the amount of seconds streamed. We traverse the playlog to figure
        # out where we are in the queue, accounting for actual streamed
        # seconds (and not duration) and skipped seconds. If a track has been repeated,
        # it will simply be in the playlog multiple times.
        played_time = 0
        queue_index = queue.current_index or 0
        track_time = 0
        for play_log_entry in queue.flow_mode_stream_log:
            queue_item_duration = (
                # NOTE: 'seconds_streamed' can actually be 0 if there was a stream error!
                play_log_entry.seconds_streamed
                if play_log_entry.seconds_streamed is not None
                else play_log_entry.duration
            )
            if elapsed_time_queue_total > (queue_item_duration + played_time):
                # total elapsed time is more than (streamed) track duration
                # this track has been fully played, move in.
                played_time += queue_item_duration
            else:
                # no more seconds left to divide, this is our track
                # account for any seeking by adding the skipped/seeked seconds
                queue_index = self.index_by_id(queue.queue_id, play_log_entry.queue_item_id)
                queue_item = self.get_item(queue.queue_id, queue_index)
                if queue_item and queue_item.streamdetails:
                    track_sec_skipped = queue_item.streamdetails.seek_position
                else:
                    track_sec_skipped = 0
                track_time = elapsed_time_queue_total + track_sec_skipped - played_time
                break

        return queue_index, track_time

    def _parse_player_current_item_id(self, queue_id: str, player: Player) -> str | None:
        """Parse QueueItem ID from Player's current url."""
        if not player.current_media:
            return None
        if player.current_media.queue_id and player.current_media.queue_id != queue_id:
            return None
        if player.current_media.queue_item_id:
            return player.current_media.queue_item_id
        if not player.current_media.uri:
            return None
        if queue_id in player.current_media.uri:
            # try to extract the item id from either a url or queue_id/item_id combi
            current_item_id = player.current_media.uri.rsplit("/")[-1].split(".")[0]
            if self.get_item(queue_id, current_item_id):
                return current_item_id
        return None
