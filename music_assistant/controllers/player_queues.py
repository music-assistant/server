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
from contextlib import suppress
from types import NoneType
from typing import TYPE_CHECKING, Any, TypedDict, cast

import shortuuid
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    EventType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    ProviderFeature,
    QueueOption,
    RepeatMode,
)
from music_assistant_models.errors import (
    AudioError,
    InvalidCommand,
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    PlayerUnavailableError,
    QueueEmpty,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    PlayableMediaItemType,
    Playlist,
    Podcast,
    PodcastEpisode,
    Track,
    UniqueList,
    media_from_dict,
)
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import (
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    CONF_FLOW_MODE,
    MASS_LOGO_ONLINE,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.api import api_command
from music_assistant.helpers.audio import get_stream_details, get_stream_dsp_details
from music_assistant.helpers.throttle_retry import BYPASS_THROTTLER
from music_assistant.helpers.util import get_changed_keys, percentage
from music_assistant.models.core_controller import CoreController
from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant_models.auth import User
    from music_assistant_models.media_items.metadata import MediaItemImage

    from music_assistant import MusicAssistant
    from music_assistant.models.player import Player


CONF_DEFAULT_ENQUEUE_SELECT_ARTIST = "default_enqueue_select_artist"
CONF_DEFAULT_ENQUEUE_SELECT_ALBUM = "default_enqueue_select_album"

ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE = "all_tracks"
ENQUEUE_SELECT_ALBUM_DEFAULT_VALUE = "all_tracks"

CONF_DEFAULT_ENQUEUE_OPTION_ARTIST = "default_enqueue_option_artist"
CONF_DEFAULT_ENQUEUE_OPTION_ALBUM = "default_enqueue_option_album"
CONF_DEFAULT_ENQUEUE_OPTION_TRACK = "default_enqueue_option_track"
CONF_DEFAULT_ENQUEUE_OPTION_RADIO = "default_enqueue_option_radio"
CONF_DEFAULT_ENQUEUE_OPTION_PLAYLIST = "default_enqueue_option_playlist"
CONF_DEFAULT_ENQUEUE_OPTION_AUDIOBOOK = "default_enqueue_option_audiobook"
CONF_DEFAULT_ENQUEUE_OPTION_PODCAST = "default_enqueue_option_podcast"
CONF_DEFAULT_ENQUEUE_OPTION_PODCAST_EPISODE = "default_enqueue_option_podcast_episode"
CONF_DEFAULT_ENQUEUE_OPTION_FOLDER = "default_enqueue_option_folder"
CONF_DEFAULT_ENQUEUE_OPTION_UNKNOWN = "default_enqueue_option_unknown"
RADIO_TRACK_MAX_DURATION_SECS = 20 * 60  # 20 minutes
CACHE_CATEGORY_PLAYER_QUEUE_STATE = 0
CACHE_CATEGORY_PLAYER_QUEUE_ITEMS = 1


class CompareState(TypedDict):
    """Simple object where we store the (previous) state of a queue.

    Used for compare actions.
    """

    queue_id: str
    state: PlaybackState
    current_item_id: str | None
    next_item_id: str | None
    current_item: QueueItem | None
    elapsed_time: int
    # last_playing_elapsed_time: elapsed time from the last PLAYING state update
    # used to determine if a track was fully played when transitioning to idle
    last_playing_elapsed_time: int
    stream_title: str | None
    codec_type: ContentType | None
    output_formats: list[str] | None


class PlayerQueuesController(CoreController):
    """Controller holding all logic to enqueue music for players."""

    domain: str = "player_queues"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize core controller."""
        super().__init__(mass)
        self._queues: dict[str, PlayerQueue] = {}
        self._queue_items: dict[str, list[QueueItem]] = {}
        self._prev_states: dict[str, CompareState] = {}
        self._transitioning_players: set[str] = set()
        self.manifest.name = "Player Queues controller"
        self.manifest.description = (
            "Music Assistant's core controller which manages the queues for all players."
        )
        self.manifest.icon = "playlist-music"

    async def close(self) -> None:
        """Cleanup on exit."""
        # stop all playback
        for queue in self.all():
            if queue.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
                await self.stop(queue.queue_id)

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        enqueue_options = [ConfigValueOption(x.name, x.value) for x in QueueOption]
        return (
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
                type=ConfigEntryType.STRING,
                default_value=ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE,
                label="Items to select when you play a (in-library) artist.",
                options=[
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
                ],
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_SELECT_ALBUM,
                type=ConfigEntryType.STRING,
                default_value=ENQUEUE_SELECT_ALBUM_DEFAULT_VALUE,
                label="Items to select when you play a (in-library) album.",
                options=[
                    ConfigValueOption(
                        title="Only in-library tracks",
                        value="library_tracks",
                    ),
                    ConfigValueOption(
                        title="All tracks for album on (streaming) provider",
                        value="all_tracks",
                    ),
                ],
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
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_AUDIOBOOK,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.REPLACE.value,
                label="Default enqueue option for Audiobook item(s).",
                options=enqueue_options,
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_PODCAST,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.REPLACE.value,
                label="Default enqueue option for Podcast item(s).",
                options=enqueue_options,
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_PODCAST_EPISODE,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.REPLACE.value,
                label="Default enqueue option for Podcast-episode item(s).",
                options=enqueue_options,
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_FOLDER,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.REPLACE.value,
                label="Default enqueue option for Folder item(s).",
                options=enqueue_options,
                hidden=True,
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
    def get_active_queue(self, player_id: str) -> PlayerQueue | None:
        """Return the current active/synced queue for a player."""
        if player := self.mass.players.get(player_id):
            return self.mass.players.get_active_queue(player)
        return None

    # Queue commands

    @api_command("player_queues/shuffle")
    async def set_shuffle(self, queue_id: str, shuffle_enabled: bool) -> None:
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
        await self.load(
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
        if (
            queue.state == PlaybackState.PLAYING
            and queue.index_in_buffer is not None
            and queue.index_in_buffer == queue.current_index
        ):
            # if the queue is playing,
            # ensure to (re)queue the next track because it might have changed
            # note that we only do this if the player has loaded the current track
            # if not, we wait until it has loaded to prevent conflicts
            if next_item := self.get_next_item(queue_id, queue.index_in_buffer):
                self._enqueue_next_item(queue_id, next_item)

    @api_command("player_queues/play_media")
    async def play_media(
        self,
        queue_id: str,
        media: MediaItemType | ItemMapping | str | list[MediaItemType | ItemMapping | str],
        option: QueueOption | None = None,
        radio_mode: bool = False,
        start_item: PlayableMediaItemType | str | None = None,
        username: str | None = None,
    ) -> None:
        """Play media item(s) on the given queue.

        :param queue_id: The queue_id of the queue to play media on.
        :param media: Media that should be played (MediaItem(s) and/or uri's).
        :param option: Which enqueue mode to use.
        :param radio_mode: Enable radio mode for the given item(s).
        :param start_item: Optional item to start the playlist or album from.
        :param username: The username of the user requesting the playback.
            Setting the username allows for overriding the logged-in user
            to account for playback history per user when the play_media is
            called from a shared context (like a web hook or automation).
        """
        # ruff: noqa: PLR0915
        # we use a contextvar to bypass the throttler for this asyncio task/context
        # this makes sure that playback has priority over other requests that may be
        # happening in the background
        BYPASS_THROTTLER.set(True)
        if not (queue := self.get(queue_id)):
            raise PlayerUnavailableError(f"Queue {queue_id} is not available")
        # always fetch the underlying player so we can raise early if its not available
        queue_player = self.mass.players.get(queue_id, True)
        assert queue_player is not None  # for type checking
        if queue_player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS):
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return

        # save the user requesting the playback
        playback_user: User | None
        if username and (user := await self.mass.webserver.auth.get_user_by_username(username)):
            playback_user = user
        else:
            playback_user = get_current_user()
        queue.userid = playback_user.user_id if playback_user else None

        # a single item or list of items may be provided
        media_list = media if isinstance(media, list) else [media]

        # clear queue if needed
        if option == QueueOption.REPLACE:
            self.clear(queue_id)
        # Clear the 'enqueued media item' list when a new queue is requested
        if option not in (QueueOption.ADD, QueueOption.NEXT):
            queue.enqueued_media_items.clear()

        media_items: list[MediaItemType] = []
        radio_source: list[MediaItemType] = []
        # resolve all media items
        for item in media_list:
            try:
                # parse provided uri into a MA MediaItem or Basic QueueItem from URL
                media_item: MediaItemType | ItemMapping | BrowseFolder
                if isinstance(item, str):
                    media_item = await self.mass.music.get_item_by_uri(item)
                elif isinstance(item, dict):  # type: ignore[unreachable]
                    # TODO: Investigate why the API parser sometimes passes raw dicts instead of
                    # converting them to MediaItem objects. The parse_value function in api.py
                    # should handle dict-to-object conversion, but dicts are slipping through
                    # in some cases. This is defensive handling for that parser bug.
                    media_item = media_from_dict(item)  # type: ignore[unreachable]
                    self.logger.debug("Converted to: %s", type(media_item))
                else:
                    # item is MediaItemType | ItemMapping at this point
                    media_item = item

                # Save requested media item to play on the queue so we can use it as a source
                # for Don't stop the music. Use FIFO list to keep track of the last 10 played items
                # Skip ItemMapping and BrowseFolder - only queue full MediaItemType objects
                if not isinstance(
                    media_item, (ItemMapping, BrowseFolder)
                ) and media_item.media_type in (
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
                    config_value = await self.mass.config.get_core_config_value(
                        self.domain,
                        f"default_enqueue_option_{media_item.media_type.value}",
                        return_type=str,
                    )
                    option = QueueOption(config_value)
                    if option == QueueOption.REPLACE:
                        self.clear(queue_id, skip_stop=True)

                # collect media_items to play
                if radio_mode:
                    # Type guard for mypy - only add full MediaItemType to radio_source
                    if not isinstance(media_item, (ItemMapping, BrowseFolder)):
                        radio_source.append(media_item)
                else:
                    # Convert start_item to string URI if needed
                    start_item_uri: str | None = None
                    if isinstance(start_item, str):
                        start_item_uri = start_item
                    elif start_item is not None:
                        start_item_uri = start_item.uri
                    media_items += await self._resolve_media_items(
                        media_item, start_item_uri, queue_id=queue_id
                    )

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
            radio_tracks = await self._get_radio_tracks(
                queue_id=queue_id, is_initial_radio_mode=True
            )
            media_items = list(radio_tracks)

        # only add valid/available items
        queue_items: list[QueueItem] = []
        for x in media_items:
            if not x or not x.available:
                continue
            queue_items.append(
                QueueItem.from_media_item(queue_id, cast("PlayableMediaItemType", x))
            )

        if not queue_items:
            raise MediaNotFoundError("No playable items found")

        # load the items into the queue
        if queue.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            cur_index = queue.index_in_buffer or queue.current_index or 0
        else:
            cur_index = queue.current_index or 0
        insert_at_index = cur_index + 1
        # Radio modes are already shuffled in a pattern we would like to keep.
        shuffle = queue.shuffle_enabled and len(queue_items) > 1 and not radio_mode

        # handle replace: clear all items and replace with the new items
        if option == QueueOption.REPLACE:
            await self.load(
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
            await self.load(
                queue_id,
                queue_items=queue_items,
                insert_at_index=insert_at_index,
                shuffle=shuffle,
            )
            return
        if option == QueueOption.REPLACE_NEXT:
            await self.load(
                queue_id,
                queue_items=queue_items,
                insert_at_index=insert_at_index,
                keep_remaining=False,
                shuffle=shuffle,
            )
            return
        # handle play: replace current loaded/playing index with new item(s)
        if option == QueueOption.PLAY:
            await self.load(
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
            await self.load(
                queue_id=queue_id,
                queue_items=queue_items,
                insert_at_index=insert_at_index
                if queue.shuffle_enabled
                else len(self._queue_items[queue_id]) + 1,
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
        if item_index is None:
            raise InvalidDataError(f"Item {queue_item_id} not found in queue")
        if queue.index_in_buffer is not None and item_index <= queue.index_in_buffer:
            msg = f"{item_index} is already played/buffered"
            raise IndexError(msg)

        queue_items = self._queue_items[queue_id]
        queue_items = queue_items.copy()

        if pos_shift == 0 and queue.state == PlaybackState.PLAYING:
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
            if item_index is None:
                raise InvalidDataError(f"Item {item_id_or_index} not found in queue")
        else:
            item_index = item_id_or_index
        queue = self._queues[queue_id]
        if queue.index_in_buffer is not None and item_index <= queue.index_in_buffer:
            # ignore request if track already loaded in the buffer
            # the frontend should guard so this is just in case
            self.logger.warning("delete requested for item already loaded in buffer")
            return
        queue_items = self._queue_items[queue_id]
        queue_items.pop(item_index)
        self.update_items(queue_id, queue_items)

    @api_command("player_queues/clear")
    def clear(self, queue_id: str, skip_stop: bool = False) -> None:
        """Clear all items in the queue."""
        queue = self._queues[queue_id]
        queue.radio_source = []
        if queue.state != PlaybackState.IDLE and not skip_stop:
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
        queue_player = self.mass.players.get(queue_id, True)
        if queue_player is None:
            raise PlayerUnavailableError(f"Player {queue_id} is not available")
        if (queue := self.get(queue_id)) and queue.active:
            if queue.state == PlaybackState.PLAYING:
                queue.resume_pos = int(queue.corrected_elapsed_time)
            # forward the actual command to the player
            if temp_player := self.mass.players.get(queue_id):
                await temp_player.stop()

    @api_command("player_queues/play")
    async def play(self, queue_id: str) -> None:
        """
        Handle PLAY command for given queue.

        - queue_id: queue_id of the playerqueue to handle the command.
        """
        queue_player = self.mass.players.get(queue_id, True)
        if queue_player is None:
            raise PlayerUnavailableError(f"Player {queue_id} is not available")
        if (
            (queue := self._queues.get(queue_id))
            and queue.active
            and queue.state == PlaybackState.PAUSED
        ):
            # forward the actual play/unpause command to the player
            await queue_player.play()
            return
        # player is not paused, perform resume instead
        await self.resume(queue_id)

    @api_command("player_queues/pause")
    async def pause(self, queue_id: str) -> None:
        """Handle PAUSE command for given queue.

        - queue_id: queue_id of the playerqueue to handle the command.
        """
        if queue := self._queues.get(queue_id):
            if queue.state == PlaybackState.PLAYING:
                queue.resume_pos = int(queue.corrected_elapsed_time)
        # forward the actual command to the player controller
        queue_player = self.mass.players.get(queue_id)
        assert queue_player is not None  # for type checking
        if not (self.mass.players.get_player_provider(queue_id)):
            return  # guard

        if PlayerFeature.PAUSE not in queue_player.supported_features:
            # if player does not support pause, we need to send stop
            await queue_player.stop()
            return
        await queue_player.pause()

        async def _watch_pause() -> None:
            count = 0
            # wait for pause
            while count < 5 and queue_player.playback_state == PlaybackState.PLAYING:
                count += 1
                await asyncio.sleep(1)
            # wait for unpause
            if queue_player.playback_state != PlaybackState.PAUSED:
                return
            count = 0
            while count < 30 and queue_player.playback_state == PlaybackState.PAUSED:
                count += 1
                await asyncio.sleep(1)
            # if player is still paused when the limit is reached, send stop
            if queue_player.playback_state == PlaybackState.PAUSED:
                await queue_player.stop()

        # we auto stop a player from paused when its paused for 30 seconds
        if not queue_player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS):
            self.mass.create_task(_watch_pause())

    @api_command("player_queues/play_pause")
    async def play_pause(self, queue_id: str) -> None:
        """Toggle play/pause on given playerqueue.

        - queue_id: queue_id of the queue to handle the command.
        """
        if (queue := self._queues.get(queue_id)) and queue.state == PlaybackState.PLAYING:
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
        if idx is None:
            self.logger.warning("Queue %s has no current index", queue.display_name)
            return
        attempts = 5
        while attempts:
            try:
                if (next_index := self._get_next_index(queue_id, idx, True)) is not None:
                    await self.play_index(queue_id, next_index, debounce=True)
                break
            except MediaNotFoundError:
                self.logger.warning(
                    "Failed to fetch next track for queue %s - trying next item",
                    queue.display_name,
                )
                idx += 1
                attempts -= 1

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
        next_index = int(current_index)
        # restart current track if current track has played longer than 4
        # otherwise skip to previous track
        if self._queues[queue_id].elapsed_time < 5:
            next_index = max(current_index - 1, 0)
        await self.play_index(queue_id, next_index, debounce=True)

    @api_command("player_queues/skip")
    async def skip(self, queue_id: str, seconds: int = 10) -> None:
        """Handle SKIP command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        - seconds: number of seconds to skip in track. Use negative value to skip back.
        """
        if (queue := self.get(queue_id)) is None or not queue.active:
            # TODO: forward to underlying player if not active
            return
        await self.seek(queue_id, int(self._queues[queue_id].elapsed_time + seconds))

    @api_command("player_queues/seek")
    async def seek(self, queue_id: str, position: int = 10) -> None:
        """Handle SEEK command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        - position: position in seconds to seek to in the current playing item.
        """
        if not (queue := self.get(queue_id)):
            return
        queue_player = self.mass.players.get(queue_id, True)
        if queue_player is None:
            raise PlayerUnavailableError(f"Player {queue_id} is not available")
        if not queue.current_item:
            raise InvalidCommand(f"Queue {queue_player.display_name} has no item(s) loaded.")
        if not queue.current_item.duration:
            raise InvalidCommand("Can not seek items without duration.")
        position = max(0, int(position))
        if position > queue.current_item.duration:
            raise InvalidCommand("Can not seek outside of duration range.")
        if queue.current_index is None:
            raise InvalidCommand(f"Queue {queue_player.display_name} has no current index.")
        await self.play_index(queue_id, queue.current_index, seek_position=position)

    @api_command("player_queues/resume")
    async def resume(self, queue_id: str, fade_in: bool | None = None) -> None:
        """Handle RESUME command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        """
        queue = self._queues[queue_id]
        queue_items = self._queue_items[queue_id]
        resume_item = queue.current_item
        if queue.state == PlaybackState.PLAYING:
            # resume requested while already playing,
            # use current position as resume position
            resume_pos = queue.corrected_elapsed_time
            fade_in = False
        else:
            resume_pos = queue.resume_pos or queue.elapsed_time

        if not resume_item and queue.current_index is not None and len(queue_items) > 0:
            resume_item = self.get_item(queue_id, queue.current_index)
            resume_pos = 0
        elif not resume_item and queue.current_index is None and len(queue_items) > 0:
            # items available in queue but no previous track, start at 0
            resume_item = self.get_item(queue_id, 0)
            resume_pos = 0

        if resume_item is not None:
            queue_player = self.mass.players.get(queue_id)
            if queue_player is None:
                raise PlayerUnavailableError(f"Player {queue_id} is not available")
            if (
                fade_in is None
                and queue_player.playback_state == PlaybackState.IDLE
                and (time.time() - queue.elapsed_time_last_updated) > 60
            ):
                # enable fade in effect if the player is idle for a while
                fade_in = resume_pos > 0
            if resume_item.media_type == MediaType.RADIO:
                # we're not able to skip in online radio so this is pointless
                resume_pos = 0
            await self.play_index(
                queue_id, resume_item.queue_item_id, int(resume_pos), fade_in or False
            )
        else:
            # Queue is empty, try to resume from playlog
            if await self._try_resume_from_playlog(queue):
                return
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
            temp_index = self.index_by_id(queue_id, index)
            if temp_index is None:
                raise InvalidDataError(f"Item {index} not found in queue")
            index = temp_index
        # At this point index is guaranteed to be int
        queue.current_index = index
        # update current item and elapsed time and signal update
        # this way the UI knows immediately that a new item is loading
        queue.current_item = self.get_item(queue_id, index)
        queue.elapsed_time = seek_position
        self.signal_update(queue_id)
        queue.index_in_buffer = index
        queue.flow_mode_stream_log = []
        target_player = self.mass.players.get(queue_id)
        if target_player is None:
            raise PlayerUnavailableError(f"Player {queue_id} is not available")
        enqueue_supported = PlayerFeature.ENQUEUE in target_player.supported_features
        queue.next_item_id_enqueued = None
        # always update session id when we start a new playback session
        queue.session_id = shortuuid.random(length=8)
        # handle resume point of audiobook(chapter) or podcast(episode)
        if (
            not seek_position
            and (queue_item := self.get_item(queue_id, index))
            and (resume_position_ms := getattr(queue_item.media_item, "resume_position_ms", 0))
        ):
            seek_position = max(0, int((resume_position_ms - 500) / 1000))

        # send play_media request to player
        # NOTE that we debounce this a bit to account for someone hitting the next button
        # like a madman. This will prevent the player from being overloaded with requests.
        async def _play_index(index: int, debounce: bool) -> None:
            for attempt in range(5):
                try:
                    queue_item = self.get_item(queue_id, index)
                    if not queue_item:
                        continue  # guard
                    await self._load_item(
                        queue_item,
                        self._get_next_index(queue_id, index),
                        is_start=True,
                        seek_position=seek_position if attempt == 0 else 0,
                        fade_in=fade_in if attempt == 0 else False,
                    )
                    # if we reach this point, loading the item succeeded, break the loop
                    queue.current_index = index
                    queue.current_item = queue_item
                    break
                except (MediaNotFoundError, AudioError):
                    # the requested index can not be played.
                    if queue_item:
                        self.logger.warning(
                            "Skipping unplayable item %s (%s)",
                            queue_item.name,
                            queue_item.uri,
                        )
                        queue_item.available = False
                    next_index = self._get_next_index(queue_id, index, allow_repeat=False)
                    if next_index is None:
                        raise MediaNotFoundError("No next item available")
                    index = next_index
            else:
                # all attempts to find a playable item failed
                raise MediaNotFoundError("No playable item found to start playback")

            # work out if we need to use flow mode
            prefer_flow_mode = await self.mass.config.get_player_config_value(
                queue_id, CONF_FLOW_MODE, default=False
            )
            flow_mode = (
                prefer_flow_mode or not enqueue_supported
            ) and queue_item.media_type not in (
                # don't use flow mode for duration-less streams
                MediaType.RADIO,
                MediaType.PLUGIN_SOURCE,
            )
            await asyncio.sleep(0.5 if debounce else 0.1)
            queue.flow_mode = flow_mode
            await self.mass.players.play_media(
                player_id=queue_id,
                media=await self.player_media_from_queue_item(queue_item, flow_mode),
            )
            queue.current_index = index
            queue.current_item = queue_item
            await asyncio.sleep(2)
            self._transitioning_players.discard(queue_id)

        # we set a flag to notify the update logic that we're transitioning to a new track
        self._transitioning_players.add(queue_id)

        # we debounce the play_index command to handle the case where someone
        # is spamming next/previous on the player
        task_id = f"play_index_{queue_id}"
        if existing_task := self.mass.get_task(task_id):
            existing_task.cancel()
            with suppress(asyncio.CancelledError):
                await existing_task
        task = self.mass.create_task(
            _play_index,
            index,
            debounce,
            task_id=task_id,
        )
        await task
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
            raise PlayerUnavailableError(f"Queue {source_queue_id} is not available")
        if not (target_queue := self.get(target_queue_id)):
            raise PlayerUnavailableError(f"Queue {target_queue_id} is not available")
        if auto_play is None:
            auto_play = source_queue.state == PlaybackState.PLAYING

        target_player = self.mass.players.get(target_queue_id)
        if target_player is None:
            raise PlayerUnavailableError(f"Player {target_queue_id} is not available")
        if target_player.active_group or target_player.synced_to:
            # edge case: the user wants to move playback from the group as a whole, to a single
            # player in the group or it is grouped and the command targeted at the single player.
            # We need to dissolve the group first.
            group_id = target_player.active_group or target_player.synced_to
            assert group_id is not None  # checked in if condition above
            await self.mass.players.cmd_ungroup(group_id)
            await asyncio.sleep(3)

        source_items = self._queue_items[source_queue_id]
        target_queue.repeat_mode = source_queue.repeat_mode
        target_queue.shuffle_enabled = source_queue.shuffle_enabled
        target_queue.dont_stop_the_music_enabled = source_queue.dont_stop_the_music_enabled
        target_queue.radio_source = source_queue.radio_source
        target_queue.enqueued_media_items = source_queue.enqueued_media_items
        target_queue.resume_pos = int(source_queue.elapsed_time)
        target_queue.current_index = source_queue.current_index
        if source_queue.current_item:
            target_queue.current_item = source_queue.current_item
            target_queue.current_item.queue_id = target_queue_id
        self.clear(source_queue_id)

        await self.load(target_queue_id, source_items, keep_remaining=False, keep_played=False)
        for item in source_items:
            item.queue_id = target_queue_id
        self.update_items(target_queue_id, source_items)
        if auto_play:
            await self.resume(target_queue_id)

    # Interaction with player

    async def on_player_register(self, player: Player) -> None:
        """Register PlayerQueue for given player/queue id."""
        queue_id = player.player_id
        queue: PlayerQueue | None = None
        queue_items: list[QueueItem] = []
        # try to restore previous state
        if prev_state := await self.mass.cache.get(
            key=queue_id,
            provider=self.domain,
            category=CACHE_CATEGORY_PLAYER_QUEUE_STATE,
        ):
            try:
                queue = PlayerQueue.from_dict(prev_state)
                prev_items = await self.mass.cache.get(
                    key=queue_id,
                    provider=self.domain,
                    category=CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
                    default=[],
                )
                queue_items = []
                for idx, item_data in enumerate(prev_items):
                    qi = QueueItem.from_cache(item_data)
                    if not qi.media_item:
                        # Skip items with missing media_item - this can happen if
                        # MA was killed during shutdown while cache was being written
                        self.logger.debug(
                            "Skipping queue item %s (index %d) restored from cache "
                            "without media_item",
                            qi.name,
                            idx,
                        )
                        continue
                    queue_items.append(qi)
                if queue.enqueued_media_items:
                    # we need to restore the MediaItem objects for the enqueued media items
                    # Items from cache may be dicts that need deserialization
                    restored_enqueued_items: list[MediaItemType] = []
                    cached_items: list[dict[str, Any] | MediaItemType] = cast(
                        "list[dict[str, Any] | MediaItemType]", queue.enqueued_media_items
                    )
                    for item in cached_items:
                        if isinstance(item, dict):
                            restored_item = media_from_dict(item)
                            restored_enqueued_items.append(cast("MediaItemType", restored_item))
                        else:
                            restored_enqueued_items.append(item)
                    queue.enqueued_media_items = restored_enqueued_items
            except Exception as err:
                self.logger.warning(
                    "Failed to restore the queue(items) for %s - %s",
                    player.display_name,
                    str(err),
                )
                # Reset to clean state on failure
                queue = None
                queue_items = []
        if queue is None:
            queue = PlayerQueue(
                queue_id=queue_id,
                active=False,
                display_name=player.display_name,
                available=player.available,
                dont_stop_the_music_enabled=False,
                items=0,
            )

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
        queue_id = player.player_id
        if (queue := self._queues.get(queue_id)) is None:
            # race condition
            return
        if player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS):
            # do nothing while the announcement is in progress
            return
        # determine if this queue is currently active for this player
        queue.active = player.active_source in (queue.queue_id, None)
        if not queue.active and queue_id not in self._prev_states:
            queue.state = PlaybackState.IDLE
            # return early if the queue is not active and we have no previous state
            return
        if queue.queue_id in self._transitioning_players:
            # we're currently transitioning to a new track,
            # ignore updates from the player during this time
            return

        # queue is active and preflight checks passed, update the queue details
        self._update_queue_from_player(player)

    def on_player_remove(self, player_id: str, permanent: bool) -> None:
        """Call when a player is removed from the registry."""
        if permanent:
            # if the player is permanently removed, we also remove the cached queue data
            self.mass.create_task(
                self.mass.cache.delete(
                    key=player_id,
                    provider=self.domain,
                    category=CACHE_CATEGORY_PLAYER_QUEUE_STATE,
                )
            )
            self.mass.create_task(
                self.mass.cache.delete(
                    key=player_id,
                    provider=self.domain,
                    category=CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
                )
            )
        self._queues.pop(player_id, None)
        self._queue_items.pop(player_id, None)

    async def load_next_queue_item(
        self,
        queue_id: str,
        current_item_id: str,
    ) -> QueueItem:
        """
        Call when a player wants the next queue item to play.

        Raises QueueEmpty if there are no more tracks left.
        """
        queue = self.get(queue_id)
        if not queue:
            msg = f"PlayerQueue {queue_id} is not available"
            raise PlayerUnavailableError(msg)
        cur_index = self.index_by_id(queue_id, current_item_id)
        if cur_index is None:
            # this is just a guard for bad data
            raise QueueEmpty("Invalid item id for queue given.")
        next_item: QueueItem | None = None
        idx = 0
        while True:
            next_index = self._get_next_index(queue_id, cur_index + idx)
            if next_index is None:
                raise QueueEmpty("No more tracks left in the queue.")
            queue_item = self.get_item(queue_id, next_index)
            if queue_item is None:
                raise QueueEmpty("No more tracks left in the queue.")
            if idx >= 10:
                # we only allow 10 retries to prevent infinite loops
                raise QueueEmpty("No more (playable) tracks left in the queue.")
            try:
                await self._load_item(queue_item, next_index)
                # we're all set, this is our next item
                next_item = queue_item
                break
            except (MediaNotFoundError, AudioError):
                # No stream details found, skip this QueueItem
                self.logger.warning(
                    "Skipping unplayable item %s (%s)", queue_item.name, queue_item.uri
                )
                queue_item.available = False
                idx += 1
        if idx != 0:
            # we skipped some items, signal a queue items update
            self.update_items(queue_id, self._queue_items[queue_id])
        if next_item is None:
            raise QueueEmpty("No more (playable) tracks left in the queue.")

        return next_item

    async def _load_item(
        self,
        queue_item: QueueItem,
        next_index: int | None,
        is_start: bool = False,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> None:
        """Try to load the stream details for the given queue item."""
        queue_id = queue_item.queue_id
        queue = self._queues[queue_id]

        # we use a contextvar to bypass the throttler for this asyncio task/context
        # this makes sure that playback has priority over other requests that may be
        # happening in the background
        BYPASS_THROTTLER.set(True)

        self.logger.debug(
            "(pre)loading (next) item for queue %s...",
            queue.display_name,
        )

        if not queue_item.available:
            raise MediaNotFoundError(f"Item {queue_item.uri} is not available")

        # work out if we are playing an album and if we should prefer album
        # loudness
        next_track_from_same_album = (
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
        )
        current_index = self.index_by_id(queue_id, queue_item.queue_item_id)
        if current_index is None:
            previous_track_from_same_album = False
        else:
            previous_index = max(current_index - 1, 0)
            previous_track_from_same_album = (
                previous_index > 0
                and (previous_item := self.get_item(queue_id, previous_index)) is not None
                and previous_item.media_item is not None
                and hasattr(previous_item.media_item, "album")
                and previous_item.media_item.album is not None
                and queue_item.media_item is not None
                and hasattr(queue_item.media_item, "album")
                and queue_item.media_item.album is not None
                and queue_item.media_item.album.item_id == previous_item.media_item.album.item_id
            )
        playing_album_tracks = next_track_from_same_album or previous_track_from_same_album
        if queue_item.media_item and isinstance(queue_item.media_item, Track):
            album = queue_item.media_item.album
            # prefer the full library media item so we have all metadata and provider(quality) info
            # always request the full library item as there might be other qualities available
            if library_item := await self.mass.music.get_library_item_by_prov_id(
                queue_item.media_item.media_type,
                queue_item.media_item.item_id,
                queue_item.media_item.provider,
            ):
                queue_item.media_item = cast("Track", library_item)
            elif not queue_item.media_item.image or queue_item.media_item.provider.startswith(
                "ytmusic"
            ):
                # Youtube Music has poor thumbs by default, so we always fetch the full item
                # this also catches the case where they have an unavailable item in a listing
                fetched_item = await self.mass.music.get_item_by_uri(queue_item.uri)
                queue_item.media_item = cast("Track", fetched_item)

            # ensure we got the full (original) album set
            if album and (
                library_album := await self.mass.music.get_library_item_by_prov_id(
                    album.media_type,
                    album.item_id,
                    album.provider,
                )
            ):
                queue_item.media_item.album = cast("Album", library_album)
            elif album:
                # Restore original album if we have no better alternative from the library
                queue_item.media_item.album = album
            # prefer album image over track image
            if queue_item.media_item.album and queue_item.media_item.album.image:
                org_images: list[MediaItemImage] = queue_item.media_item.metadata.images or []
                queue_item.media_item.metadata.images = UniqueList(
                    [
                        queue_item.media_item.album.image,
                        *org_images,
                    ]
                )
        # Fetch the streamdetails, which could raise in case of an unplayable item.
        # For example, YT Music returns Radio Items that are not playable.
        queue_item.streamdetails = await get_stream_details(
            mass=self.mass,
            queue_item=queue_item,
            seek_position=seek_position,
            fade_in=fade_in,
            prefer_album_loudness=bool(playing_album_tracks),
        )

    def track_loaded_in_buffer(self, queue_id: str, item_id: str) -> None:
        """Call when a player has (started) loading a track in the buffer."""
        queue = self.get(queue_id)
        if not queue:
            msg = f"PlayerQueue {queue_id} is not available"
            raise PlayerUnavailableError(msg)
        # store the index of the item that is currently (being) loaded in the buffer
        # which helps us a bit to determine how far the player has buffered ahead
        queue.index_in_buffer = self.index_by_id(queue_id, item_id)
        self.logger.debug("PlayerQueue %s loaded item %s in buffer", queue.display_name, item_id)
        self.signal_update(queue_id)
        # preload next streamdetails
        self._preload_next_item(queue_id, item_id)

    # Main queue manipulation methods

    async def load(
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
            next_items = await _smart_shuffle(next_items)
        self.update_items(queue_id, prev_items + next_items)

    def update_items(self, queue_id: str, queue_items: list[QueueItem]) -> None:
        """Update the existing queue items, mostly caused by reordering."""
        self._queue_items[queue_id] = queue_items
        queue = self._queues[queue_id]
        queue.items = len(self._queue_items[queue_id])
        # to track if the queue items changed we set a timestamp
        # this is a simple way to detect changes in the list of items
        # without having to compare the entire list
        queue.items_last_updated = time.time()
        self.signal_update(queue_id, True)
        if (
            queue.state == PlaybackState.PLAYING
            and queue.index_in_buffer is not None
            and queue.index_in_buffer == queue.current_index
        ):
            # if the queue is playing,
            # ensure to (re)queue the next track because it might have changed
            # note that we only do this if the player has loaded the current track
            # if not, we wait until it has loaded to prevent conflicts
            if next_item := self.get_next_item(queue_id, queue.index_in_buffer):
                self._enqueue_next_item(queue_id, next_item)

    # Helper methods

    def get_item(self, queue_id: str, item_id_or_index: int | str | None) -> QueueItem | None:
        """Get queue item by index or item_id."""
        if item_id_or_index is None:
            return None
        if (queue_items := self._queue_items.get(queue_id)) is None:
            return None
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
            # save items in cache - only cache items with valid media_item
            cache_data = [
                x.to_cache() for x in self._queue_items[queue_id] if x.media_item is not None
            ]
            self.mass.create_task(
                self.mass.cache.set(
                    key=queue_id,
                    data=cache_data,
                    provider=self.domain,
                    category=CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
                )
            )
        # always send the base event
        self.mass.signal_event(EventType.QUEUE_UPDATED, object_id=queue_id, data=queue)
        # also signal update to the player itself so it can update its current_media
        self.mass.players.trigger_player_update(queue_id)
        # save state
        self.mass.create_task(
            self.mass.cache.set(
                key=queue_id,
                data=queue.to_cache(),
                provider=self.domain,
                category=CACHE_CATEGORY_PLAYER_QUEUE_STATE,
            )
        )

    def index_by_id(self, queue_id: str, queue_item_id: str) -> int | None:
        """Get index by queue_item_id."""
        queue_items = self._queue_items[queue_id]
        for index, item in enumerate(queue_items):
            if item.queue_item_id == queue_item_id:
                return index
        return None

    async def player_media_from_queue_item(
        self, queue_item: QueueItem, flow_mode: bool
    ) -> PlayerMedia:
        """Parse PlayerMedia from QueueItem."""
        queue = self._queues[queue_item.queue_id]
        if flow_mode:
            duration = None
        elif queue_item.streamdetails:
            # prefer netto duration
            # when seeking, the player only receives the remaining duration
            duration = queue_item.streamdetails.duration or queue_item.duration
            if duration and queue_item.streamdetails.seek_position:
                duration = duration - queue_item.streamdetails.seek_position
        else:
            duration = queue_item.duration
        if queue.session_id is None:
            # handle error or return early
            raise InvalidDataError("Queue session_id is None")
        media = PlayerMedia(
            uri=await self.mass.streams.resolve_stream_url(
                queue.session_id, queue_item, flow_mode=flow_mode
            ),
            media_type=MediaType.FLOW_STREAM if flow_mode else queue_item.media_type,
            title="Music Assistant" if flow_mode else queue_item.name,
            image_url=MASS_LOGO_ONLINE,
            duration=duration,
            source_id=queue_item.queue_id,
            queue_item_id=queue_item.queue_item_id,
        )
        if not flow_mode and queue_item.media_item:
            media.title = queue_item.media_item.name
            media.artist = getattr(queue_item.media_item, "artist_str", "")
            media.album = (
                album.name if (album := getattr(queue_item.media_item, "album", None)) else ""
            )
            if queue_item.image:
                # the image format needs to be 500x500 jpeg for maximum compatibility with players
                # we prefer the imageproxy on the streamserver here because this request is sent
                # to the player itself which may not be able to reach the regular webserver
                media.image_url = self.mass.metadata.get_image_url(
                    queue_item.image, size=500, prefer_stream_server=True
                )
        return media

    async def get_artist_tracks(self, artist: Artist) -> list[Track]:
        """Return tracks for given artist, based on user preference."""
        artist_items_conf = self.mass.config.get_raw_core_config_value(
            self.domain,
            CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
            ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE,
        )
        self.logger.info(
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
            all_tracks: list[Track] = []
            for library_album in await self.mass.music.artists.albums(
                artist.item_id,
                artist.provider,
                in_library_only=artist_items_conf == "library_album_tracks",
            ):
                for album_track in await self.mass.music.albums.tracks(
                    library_album.item_id, library_album.provider
                ):
                    if album_track not in all_tracks:
                        all_tracks.append(album_track)
            random.shuffle(all_tracks)
            return all_tracks
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
        self.logger.info(
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
        self.logger.info(
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

    async def get_audiobook_resume_point(
        self, audio_book: Audiobook, chapter: str | int | None = None, userid: str | None = None
    ) -> int:
        """Return resume point (in milliseconds) for given audio book."""
        self.logger.debug(
            "Fetching resume point to play for audio book %s",
            audio_book.name,
        )
        if chapter is not None:
            # user explicitly selected a chapter to play
            start_chapter = int(chapter) if isinstance(chapter, str) else chapter
            if chapters := audio_book.metadata.chapters:
                if _chapter := next((x for x in chapters if x.position == start_chapter), None):
                    return int(_chapter.start * 1000)
            raise InvalidDataError(
                f"Unable to resolve chapter to play for Audiobook {audio_book.name}"
            )
        full_played, resume_position_ms = await self.mass.music.get_resume_position(
            audio_book, userid=userid
        )
        return 0 if full_played else resume_position_ms

    async def get_next_podcast_episodes(
        self,
        podcast: Podcast | None,
        episode: PodcastEpisode | str | None,
        userid: str | None = None,
    ) -> UniqueList[PodcastEpisode]:
        """Return (next) episode(s) and resume point for given podcast."""
        if podcast is None and isinstance(episode, str | NoneType):
            raise InvalidDataError("Either podcast or episode must be provided")
        if podcast is None:
            # single podcast episode requested
            assert isinstance(episode, PodcastEpisode)  # checked above
            self.logger.debug(
                "Fetching resume point to play for Podcast episode %s",
                episode.name,
            )
            (
                fully_played,
                resume_position_ms,
            ) = await self.mass.music.get_resume_position(episode, userid=userid)
            episode.fully_played = fully_played
            episode.resume_position_ms = 0 if fully_played else resume_position_ms
            return UniqueList([episode])
        # podcast with optional start episode requested
        self.logger.debug(
            "Fetching episode(s) and resume point to play for Podcast %s",
            podcast.name,
        )
        all_episodes = [
            x async for x in self.mass.music.podcasts.episodes(podcast.item_id, podcast.provider)
        ]
        all_episodes.sort(key=lambda x: x.position)
        # if a episode was provided, a user explicitly selected a episode to play
        # so we need to find the index of the episode in the list
        resolved_episode: PodcastEpisode | None = None
        if isinstance(episode, PodcastEpisode):
            resolved_episode = next((x for x in all_episodes if x.uri == episode.uri), None)
            if resolved_episode:
                # ensure we have accurate resume info
                (
                    fully_played,
                    resume_position_ms,
                ) = await self.mass.music.get_resume_position(resolved_episode, userid=userid)
                resolved_episode.resume_position_ms = 0 if fully_played else resume_position_ms
        elif isinstance(episode, str):
            resolved_episode = next(
                (x for x in all_episodes if episode in (x.uri, x.item_id)), None
            )
            if resolved_episode:
                # ensure we have accurate resume info
                (
                    fully_played,
                    resume_position_ms,
                ) = await self.mass.music.get_resume_position(resolved_episode, userid=userid)
                resolved_episode.resume_position_ms = 0 if fully_played else resume_position_ms
        else:
            # get first episode that is not fully played
            for ep in all_episodes:
                if ep.fully_played:
                    continue
                # ensure we have accurate resume info
                (
                    fully_played,
                    resume_position_ms,
                ) = await self.mass.music.get_resume_position(ep, userid=userid)
                if fully_played:
                    continue
                ep.resume_position_ms = resume_position_ms
                resolved_episode = ep
                break
            else:
                # no episodes found that are not fully played, so we start at the beginning
                resolved_episode = next((x for x in all_episodes), None)
        if resolved_episode is None:
            raise InvalidDataError(f"Unable to resolve episode to play for Podcast {podcast.name}")
        # get the index of the episode
        episode_index = all_episodes.index(resolved_episode)
        # return the (remaining) episode(s) to play
        return UniqueList(all_episodes[episode_index:])

    def _get_next_index(
        self,
        queue_id: str,
        cur_index: int | None,
        is_skip: bool = False,
        allow_repeat: bool = True,
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

    def get_next_item(self, queue_id: str, cur_index: int | str) -> QueueItem | None:
        """Return next QueueItem for given queue."""
        index: int
        if isinstance(cur_index, str):
            resolved_index = self.index_by_id(queue_id, cur_index)
            if resolved_index is None:
                return None  # guard
            index = resolved_index
        else:
            index = cur_index
        # At this point index is guaranteed to be int
        for skip in range(5):
            if (next_index := self._get_next_index(queue_id, index + skip)) is None:
                break
            next_item = self.get_item(queue_id, next_index)
            if next_item is None:
                continue
            if not next_item.available:
                # ensure that we skip unavailable items (set by load_next track logic)
                continue
            return next_item
        return None

    async def _fill_radio_tracks(self, queue_id: str) -> None:
        """Fill a Queue with (additional) Radio tracks."""
        self.logger.debug(
            "Filling radio tracks for queue %s",
            queue_id,
        )
        tracks = await self._get_radio_tracks(queue_id=queue_id, is_initial_radio_mode=False)
        # fill queue - filter out unavailable items
        queue_items = [QueueItem.from_media_item(queue_id, x) for x in tracks if x.available]
        await self.load(
            queue_id,
            queue_items,
            insert_at_index=len(self._queue_items[queue_id]) + 1,
        )

    def _enqueue_next_item(self, queue_id: str, next_item: QueueItem | None) -> None:
        """Enqueue the next item on the player."""
        if not next_item:
            # no next item, nothing to do...
            return

        queue = self._queues[queue_id]
        if queue.flow_mode:
            # ignore this for flow mode
            return

        async def _enqueue_next_item_on_player(next_item: QueueItem) -> None:
            await self.mass.players.enqueue_next_media(
                player_id=queue_id,
                media=await self.player_media_from_queue_item(next_item, False),
            )
            if queue.next_item_id_enqueued != next_item.queue_item_id:
                queue.next_item_id_enqueued = next_item.queue_item_id
                self.logger.debug(
                    "Enqueued next track %s on queue %s",
                    next_item.name,
                    self._queues[queue_id].display_name,
                )

        task_id = f"enqueue_next_item_{queue_id}"
        self.mass.call_later(0.5, _enqueue_next_item_on_player, next_item, task_id=task_id)

    def _preload_next_item(self, queue_id: str, item_id_in_buffer: str) -> None:
        """
        Preload the streamdetails for the next item in the queue/buffer.

        This basically ensures the item is playable and fetches the stream details.
        If an error occurs, the item will be skipped and the next item will be loaded.
        """
        queue = self._queues[queue_id]

        async def _preload_streamdetails(item_id_in_buffer: str) -> None:
            try:
                # wait for the item that was loaded in the buffer is the actually playing item
                # this prevents a race condition when we preload the next item too soon
                # while the player is actually preloading the previously enqueued item.
                retries = 120
                while retries > 0:
                    if not queue.current_item:
                        return  # guard
                    if queue.current_item.queue_item_id == item_id_in_buffer:
                        break
                    retries -= 1
                    await asyncio.sleep(1)
                if next_item := await self.load_next_queue_item(queue_id, item_id_in_buffer):
                    self.logger.debug(
                        "Preloaded next item %s for queue %s",
                        next_item.name,
                        queue.display_name,
                    )
                    # enqueue the next item on the player
                    self._enqueue_next_item(queue_id, next_item)

            except QueueEmpty:
                return

        if not (current_item := self.get_item(queue_id, item_id_in_buffer)):
            # this should not happen, but guard anyways
            return
        if current_item.media_type == MediaType.RADIO or not current_item.duration:
            # radio items or no duration, nothing to do
            return

        task_id = f"preload_next_item_{queue_id}"
        self.mass.create_task(
            _preload_streamdetails,
            item_id_in_buffer,
            task_id=task_id,
            abort_existing=True,
        )

    async def _resolve_media_items(
        self,
        media_item: MediaItemType | ItemMapping | BrowseFolder,
        start_item: str | None = None,
        userid: str | None = None,
        queue_id: str | None = None,
    ) -> list[MediaItemType]:
        """Resolve/unwrap media items to enqueue."""
        # resolve Itemmapping to full media item
        if isinstance(media_item, ItemMapping):
            if media_item.uri is None:
                raise InvalidDataError("ItemMapping has no URI")
            media_item = await self.mass.music.get_item_by_uri(media_item.uri)
        if media_item.media_type == MediaType.PLAYLIST:
            media_item = cast("Playlist", media_item)
            self.mass.create_task(
                self.mass.music.mark_item_played(
                    media_item, userid=userid, queue_id=queue_id, user_initiated=True
                )
            )
            return list(await self.get_playlist_tracks(media_item, start_item))
        if media_item.media_type == MediaType.ARTIST:
            media_item = cast("Artist", media_item)
            self.mass.create_task(
                self.mass.music.mark_item_played(media_item, queue_id=queue_id, user_initiated=True)
            )
            return list(await self.get_artist_tracks(media_item))
        if media_item.media_type == MediaType.ALBUM:
            media_item = cast("Album", media_item)
            self.mass.create_task(
                self.mass.music.mark_item_played(
                    media_item, userid=userid, queue_id=queue_id, user_initiated=True
                )
            )
            return list(await self.get_album_tracks(media_item, start_item))
        if media_item.media_type == MediaType.AUDIOBOOK:
            media_item = cast("Audiobook", media_item)
            # ensure we grab the correct/latest resume point info
            media_item.resume_position_ms = await self.get_audiobook_resume_point(
                media_item, start_item, userid=userid
            )
            return [media_item]
        if media_item.media_type == MediaType.PODCAST:
            media_item = cast("Podcast", media_item)
            self.mass.create_task(
                self.mass.music.mark_item_played(
                    media_item, userid=userid, queue_id=queue_id, user_initiated=True
                )
            )
            return list(await self.get_next_podcast_episodes(media_item, start_item, userid=userid))
        if media_item.media_type == MediaType.PODCAST_EPISODE:
            media_item = cast("PodcastEpisode", media_item)
            return list(await self.get_next_podcast_episodes(None, media_item, userid=userid))
        if media_item.media_type == MediaType.FOLDER:
            media_item = cast("BrowseFolder", media_item)
            return list(await self._get_folder_tracks(media_item))
        # all other: single track or radio item
        return [cast("MediaItemType", media_item)]

    async def _try_resume_from_playlog(self, queue: PlayerQueue) -> bool:
        """Try to resume playback from playlog when queue is empty.

        Attempts to find user-initiated recently played items in the following order:
        1. By userid AND queue_id
        2. By queue_id only
        3. By userid only (if available)
        4. Any recently played item

        :param queue: The queue to resume playback on.
        :return: True if playback was started, False otherwise.
        """
        # Try different filter combinations in order of specificity
        filter_attempts: list[tuple[str | None, str | None, str]] = []
        if queue.userid:
            filter_attempts.append((queue.userid, queue.queue_id, "userid + queue_id match"))
        filter_attempts.append((None, queue.queue_id, "queue_id match"))
        if queue.userid:
            filter_attempts.append((queue.userid, None, "userid match"))
        filter_attempts.append((None, None, "any recent item"))

        for userid, queue_id, match_type in filter_attempts:
            items = await self.mass.music.recently_played(
                limit=5,
                fully_played_only=False,
                user_initiated_only=True,
                userid=userid,
                queue_id=queue_id,
            )
            for item in items:
                if not item.uri:
                    continue
                try:
                    await self.play_media(queue.queue_id, item)
                    self.logger.info(
                        "Resumed queue %s from playlog (%s)", queue.display_name, match_type
                    )
                    return True
                except MusicAssistantError as err:
                    self.logger.debug("Failed to resume with item %s: %s", item.name, err)
                    continue

        return False

    async def _get_radio_tracks(
        self, queue_id: str, is_initial_radio_mode: bool = False
    ) -> list[Track]:
        """Call the registered music providers for dynamic tracks."""
        queue = self._queues[queue_id]
        queue_track_items: list[Track] = [
            q.media_item
            for q in self._queue_items[queue_id]
            if q.media_item and isinstance(q.media_item, Track)
        ]
        if not queue.radio_source:
            # this may happen during race conditions as this method is called delayed
            return []
        self.logger.info(
            "Fetching radio tracks for queue %s based on: %s",
            queue.display_name,
            ", ".join([x.name for x in queue.radio_source]),
        )

        # Get user's preferred provider instances for steering provider selection
        preferred_provider_instances: list[str] | None = None
        if (
            queue.userid
            and (playback_user := await self.mass.webserver.auth.get_user(queue.userid))
            and playback_user.provider_filter
        ):
            preferred_provider_instances = playback_user.provider_filter

        available_base_tracks: list[Track] = []
        base_track_sample_size = 5
        # Some providers have very deterministic similar track algorithms when providing
        # a single track item. When we have a radio mode based on 1 track and we have to
        # refill the queue (ie not initial radio mode), we use the play history as base tracks
        if (
            len(queue.radio_source) == 1
            and queue.radio_source[0].media_type == MediaType.TRACK
            and not is_initial_radio_mode
        ):
            available_base_tracks = queue_track_items
        else:
            # Grab all the available base tracks based on the selected source items.
            # shuffle the source items, just in case
            for radio_item in random.sample(queue.radio_source, len(queue.radio_source)):
                ctrl = self.mass.music.get_controller(radio_item.media_type)
                try:
                    available_base_tracks += [
                        track
                        for track in await ctrl.radio_mode_base_tracks(
                            radio_item,  # type: ignore[arg-type]
                            preferred_provider_instances,
                        )
                        # Avoid duplicate base tracks
                        if track not in available_base_tracks
                    ]
                except UnsupportedFeaturedException as err:
                    self.logger.debug(
                        "Skip loading radio items for %s: %s ",
                        radio_item.uri,
                        str(err),
                    )
            if not available_base_tracks:
                raise UnsupportedFeaturedException("Radio mode not available for source items")

        # Sample tracks from the base tracks, which will be used to calculate the dynamic ones
        base_tracks = random.sample(
            available_base_tracks,
            min(base_track_sample_size, len(available_base_tracks)),
        )
        # Use a set to avoid duplicate dynamic tracks
        dynamic_tracks: set[Track] = set()
        # Use base tracks + Trackcontroller to obtain similar tracks for every base Track
        for allow_lookup in (False, True):
            if dynamic_tracks:
                break
            for base_track in base_tracks:
                try:
                    _similar_tracks = await self.mass.music.tracks.similar_tracks(
                        base_track.item_id,
                        base_track.provider,
                        allow_lookup=allow_lookup,
                        preferred_provider_instances=preferred_provider_instances,
                    )
                except MediaNotFoundError:
                    # Some providers don't have similar tracks for all items. For example,
                    # Tidal can sometimes return a 404 when the 'similar_tracks' endpoint is called.
                    # in that case, just skip the track.
                    self.logger.debug("Similar tracks not found for track %s", base_track.name)
                    continue
                for track in _similar_tracks:
                    if (
                        track not in base_tracks
                        # Exclude tracks we have already played / queued
                        and track not in queue_track_items
                        # Ignore tracks that are too long for radio mode, e.g. mixes
                        and track.duration <= RADIO_TRACK_MAX_DURATION_SECS
                    ):
                        dynamic_tracks.add(track)
                if len(dynamic_tracks) >= 50:
                    break
        queue_tracks: list[Track] = []
        dynamic_tracks_list = list(dynamic_tracks)
        # Only include the sampled base tracks when the radio mode is first initialized
        if is_initial_radio_mode:
            queue_tracks += [base_tracks[0]]
            # Exhaust base tracks with the pattern of BDDBDDBDD (1 base track + 2 dynamic tracks)
            if len(base_tracks) > 1:
                for base_track in base_tracks[1:]:
                    queue_tracks += [base_track]
                    if len(dynamic_tracks_list) > 2:
                        queue_tracks += random.sample(dynamic_tracks_list, 2)
                    else:
                        queue_tracks += dynamic_tracks_list
        # Add dynamic tracks to the queue, make sure to exclude already picked tracks
        remaining_dynamic_tracks = [t for t in dynamic_tracks_list if t not in queue_tracks]
        if remaining_dynamic_tracks:
            queue_tracks += random.sample(
                remaining_dynamic_tracks, min(len(remaining_dynamic_tracks), 25)
            )
        return queue_tracks

    async def _get_folder_tracks(self, folder: BrowseFolder) -> list[Track]:
        """Fetch (playable) tracks for given browse folder."""
        self.logger.info(
            "Fetching tracks to play for folder %s",
            folder.name,
        )
        tracks: list[Track] = []
        for item in await self.mass.music.browse(folder.path):
            if not item.is_playable:
                continue
            # recursively fetch tracks from all media types
            resolved = await self._resolve_media_items(item)
            tracks += [x for x in resolved if isinstance(x, Track)]

        return tracks

    def _update_queue_from_player(
        self,
        player: Player,
    ) -> None:
        """Update the Queue when the player state changed."""
        queue_id = player.player_id
        queue = self._queues[queue_id]

        # basic properties
        queue.display_name = player.display_name
        queue.available = player.available
        queue.items = len(self._queue_items[queue_id])

        queue.state = (
            player.playback_state or PlaybackState.IDLE if queue.active else PlaybackState.IDLE
        )
        # update current item/index from player report
        if queue.active and queue.state in (
            PlaybackState.PLAYING,
            PlaybackState.PAUSED,
        ):
            # NOTE: If the queue is not playing (yet) we will not update the current index
            # to ensure we keep the previously known current index
            if queue.flow_mode:
                # flow mode active, the player is playing one long stream
                # so we need to calculate the current index and elapsed time
                current_index, elapsed_time = self._get_flow_queue_stream_index(queue, player)
            elif item_id := self._parse_player_current_item_id(queue_id, player):
                # normal mode, the player itself will report the current item
                elapsed_time = int(player.corrected_elapsed_time or 0)
                current_index = self.index_by_id(queue_id, item_id)
            else:
                # this may happen if the player is still transitioning between tracks
                # we ignore this for now and keep the current index as is
                return

            # get current/next item based on current index
            queue.current_index = current_index
            queue.current_item = current_item = self.get_item(queue_id, current_index)
            queue.next_item = (
                self.get_next_item(queue_id, current_index)
                if current_item and current_index is not None
                else None
            )

            # correct elapsed time when seeking
            if (
                not queue.flow_mode
                and current_item
                and current_item.streamdetails
                and current_item.streamdetails.seek_position
            ):
                elapsed_time += current_item.streamdetails.seek_position
            queue.elapsed_time = elapsed_time
            queue.elapsed_time_last_updated = time.time()

        elif not queue.current_item and queue.current_index is not None:
            current_index = queue.current_index
            queue.current_item = current_item = self.get_item(queue_id, current_index)
            queue.next_item = (
                self.get_next_item(queue_id, current_index)
                if current_item and current_index is not None
                else None
            )

        # This is enough to detect any changes in the DSPDetails
        # (so child count changed, or any output format changed)
        output_formats = []
        if output_format := player.extra_data.get("output_format"):
            output_formats.append(str(output_format))
        for child_id in player.group_members:
            if (child := self.mass.players.get(child_id)) and (
                output_format := child.extra_data.get("output_format")
            ):
                output_formats.append(str(output_format))
            else:
                output_formats.append("unknown")

        # basic throttle: do not send state changed events if queue did not actually change
        prev_state: CompareState = self._prev_states.get(
            queue_id,
            CompareState(
                queue_id=queue_id,
                state=PlaybackState.IDLE,
                current_item_id=None,
                next_item_id=None,
                current_item=None,
                elapsed_time=0,
                last_playing_elapsed_time=0,
                stream_title=None,
                codec_type=None,
                output_formats=None,
            ),
        )
        # update last_playing_elapsed_time only when the player is actively playing
        # use corrected_elapsed_time which accounts for time since last update
        # this preserves the last known elapsed time when transitioning to idle/paused
        prev_playing_elapsed = prev_state["last_playing_elapsed_time"]
        prev_item_id = prev_state["current_item_id"]
        current_item_id = queue.current_item.queue_item_id if queue.current_item else None
        if queue.state == PlaybackState.PLAYING:
            current_elapsed = int(queue.corrected_elapsed_time)
            if current_item_id != prev_item_id:
                # new track started, reset the elapsed time tracker
                last_playing_elapsed_time = current_elapsed
            else:
                # same track, use the max of current and previous to handle timing issues
                last_playing_elapsed_time = max(current_elapsed, prev_playing_elapsed)
        else:
            last_playing_elapsed_time = prev_playing_elapsed
        new_state = CompareState(
            queue_id=queue_id,
            state=queue.state,
            current_item_id=queue.current_item.queue_item_id if queue.current_item else None,
            next_item_id=queue.next_item.queue_item_id if queue.next_item else None,
            current_item=queue.current_item,
            elapsed_time=int(queue.elapsed_time),
            last_playing_elapsed_time=last_playing_elapsed_time,
            stream_title=(
                queue.current_item.streamdetails.stream_title
                if queue.current_item and queue.current_item.streamdetails
                else None
            ),
            codec_type=(
                queue.current_item.streamdetails.audio_format.codec_type
                if queue.current_item and queue.current_item.streamdetails
                else None
            ),
            output_formats=output_formats,
        )
        changed_keys = get_changed_keys(dict(prev_state), dict(new_state))
        with suppress(KeyError):
            changed_keys.remove("next_item_id")
        with suppress(KeyError):
            changed_keys.remove("last_playing_elapsed_time")

        # store the new state
        if queue.active:
            self._prev_states[queue_id] = new_state
        else:
            self._prev_states.pop(queue_id, None)

        # return early if nothing changed
        if len(changed_keys) == 0:
            return

        # signal update and store state
        send_update = True
        if changed_keys == {"elapsed_time"}:
            # only elapsed time changed, do not send full queue update
            send_update = False
            prev_time = prev_state.get("elapsed_time") or 0
            cur_time = new_state.get("elapsed_time") or 0
            if abs(cur_time - prev_time) > 2:
                # send dedicated event for time updates when seeking
                self.mass.signal_event(
                    EventType.QUEUE_TIME_UPDATED,
                    object_id=queue_id,
                    data=queue.elapsed_time,
                )
                # also signal update to the player itself so it can update its current_media
                self.mass.players.trigger_player_update(queue_id)

        if send_update:
            self.signal_update(queue_id)

        if "output_formats" in changed_keys:
            # refresh DSP details since they may have changed
            dsp = get_stream_dsp_details(self.mass, queue_id)
            if queue.current_item and queue.current_item.streamdetails:
                queue.current_item.streamdetails.dsp = dsp
            if queue.next_item and queue.next_item.streamdetails:
                queue.next_item.streamdetails.dsp = dsp

        # handle updating stream_metadata if needed
        if (
            queue.current_item
            and (streamdetails := queue.current_item.streamdetails)
            and streamdetails.stream_metadata_update_callback
            and (
                streamdetails.stream_metadata_last_updated is None
                or (
                    time.time() - streamdetails.stream_metadata_last_updated
                    >= streamdetails.stream_metadata_update_interval
                )
            )
        ):
            streamdetails.stream_metadata_last_updated = time.time()
            self.mass.create_task(
                streamdetails.stream_metadata_update_callback(
                    streamdetails, int(queue.corrected_elapsed_time)
                )
            )

        # handle sending a playback progress report
        # we do this every 30 seconds or when the state changes
        if (
            changed_keys.intersection({"state", "current_item_id"})
            or int(queue.elapsed_time) % 30 == 0
        ):
            self._handle_playback_progress_report(queue, prev_state, new_state)

        # check if we need to clear the queue if we reached the end
        if "state" in changed_keys and queue.state == PlaybackState.IDLE:
            self._handle_end_of_queue(queue, prev_state, new_state)

        # watch dynamic radio items refill if needed
        if "current_item_id" in changed_keys:
            # auto enable radio mode if dont stop the music is enabled
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
                self.logger.debug(
                    "End of queue detected and Don't stop the music is enabled for %s"
                    " - setting enqueued media items as radio source: %s",
                    queue.display_name,
                    ", ".join([x.uri for x in queue.enqueued_media_items]),  # type: ignore[misc]  # uri set in __post_init__
                )
                queue.radio_source = queue.enqueued_media_items
            # auto fill radio tracks if less than 5 tracks left in the queue
            if (
                queue.radio_source
                and queue.current_index is not None
                and (queue.items - queue.current_index) < 5
            ):
                task_id = f"fill_radio_tracks_{queue_id}"
                self.mass.call_later(5, self._fill_radio_tracks, queue_id, task_id=task_id)

    def _get_flow_queue_stream_index(
        self, queue: PlayerQueue, player: Player
    ) -> tuple[int | None, int]:
        """Calculate current queue index and current track elapsed time when flow mode is active."""
        elapsed_time_queue_total = player.corrected_elapsed_time or 0
        if queue.current_index is None and not queue.flow_mode_stream_log:
            return queue.current_index, int(queue.elapsed_time)

        # For each track that has been streamed/buffered to the player,
        # a playlog entry will be created with the queue item id
        # and the amount of seconds streamed. We traverse the playlog to figure
        # out where we are in the queue, accounting for actual streamed
        # seconds (and not duration) and skipped seconds. If a track has been repeated,
        # it will simply be in the playlog multiple times.
        played_time = 0.0
        queue_index: int | None = queue.current_index or 0
        track_time = 0.0
        for play_log_entry in queue.flow_mode_stream_log:
            queue_item_duration = (
                # NOTE: 'seconds_streamed' can actually be 0 if there was a stream error!
                play_log_entry.seconds_streamed
                if play_log_entry.seconds_streamed is not None
                else play_log_entry.duration or 3600 * 24 * 7
            )
            if elapsed_time_queue_total > (queue_item_duration + played_time):
                # total elapsed time is more than (streamed) track duration
                # this track has been fully played, move on.
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
        if player.playback_state != PlaybackState.PLAYING:
            # if the player is not playing, we can't be sure that the elapsed time is correct
            # so we just return the queue index and the elapsed time
            return queue.current_index, int(queue.elapsed_time)
        return queue_index, int(track_time)

    def _parse_player_current_item_id(self, queue_id: str, player: Player) -> str | None:
        """Parse QueueItem ID from Player's current url."""
        if not player._current_media:
            # YES, we use player._current_media on purpose here because we need the raw metadata
            return None
        # prefer queue_id and queue_item_id within the current media
        if player._current_media.source_id == queue_id and player._current_media.queue_item_id:
            return player._current_media.queue_item_id
        # special case for sonos players
        if player._current_media.uri and player._current_media.uri.startswith(f"mass:{queue_id}"):
            if player._current_media.queue_item_id:
                return player._current_media.queue_item_id
            return player._current_media.uri.split(":")[-1]
        # try to extract the item id from a mass stream url
        if (
            player._current_media.uri
            and queue_id in player._current_media.uri
            and self.mass.streams.base_url in player._current_media.uri
        ):
            current_item_id = player._current_media.uri.rsplit("/")[-1].split(".")[0]
            if self.get_item(queue_id, current_item_id):
                return current_item_id
        # try to extract the item id from a queue_id/item_id combi
        if (
            player._current_media.uri
            and queue_id in player._current_media.uri
            and "/" in player._current_media.uri
        ):
            current_item_id = player._current_media.uri.split("/")[1]
            if self.get_item(queue_id, current_item_id):
                return current_item_id

        return None

    def _handle_end_of_queue(
        self, queue: PlayerQueue, prev_state: CompareState, new_state: CompareState
    ) -> None:
        """Check if the queue should be cleared after the current item."""
        # check if queue state changed to stopped (from playing/paused to idle)
        if not (
            prev_state["state"] in (PlaybackState.PLAYING, PlaybackState.PAUSED)
            and new_state["state"] == PlaybackState.IDLE
        ):
            return
        # check if no more items in the queue (next_item should be None at end of queue)
        if queue.next_item is not None:
            return
        # check if we had a previous item playing
        if prev_state["current_item_id"] is None:
            return

        async def _clear_queue_delayed() -> None:
            for _ in range(5):
                await asyncio.sleep(1)
                if queue.state != PlaybackState.IDLE:
                    return
                if queue.next_item is not None:
                    return
            self.logger.info("End of queue reached, clearing items")
            self.clear(queue.queue_id)

        # all checks passed, we stopped playback at the last (or single) track of the queue
        # now determine if the item was fully played before clearing

        # For flow mode, check if the last track was fully streamed using the stream log
        # This is more reliable than elapsed_time which can be reset/incorrect
        if queue.flow_mode and queue.flow_mode_stream_log:
            last_log_entry = queue.flow_mode_stream_log[-1]
            if last_log_entry.seconds_streamed is not None:
                # The last track finished streaming, safe to clear queue
                self.mass.create_task(_clear_queue_delayed())
            return

        # For non-flow mode, use prev_state values since queue state may have been updated/reset
        prev_item = prev_state["current_item"]
        if prev_item and (streamdetails := prev_item.streamdetails):
            duration = streamdetails.duration or prev_item.duration or 24 * 3600
        elif prev_item:
            duration = prev_item.duration or 24 * 3600
        else:
            # No current item means player has already cleared it, safe to clear queue
            self.mass.create_task(_clear_queue_delayed())
            return

        # use last_playing_elapsed_time which preserves the elapsed time from when the player
        # was still playing (before transitioning to idle where elapsed_time may be reset to 0)
        seconds_played = int(prev_state["last_playing_elapsed_time"])
        # debounce this a bit to make sure we're not clearing the queue by accident
        # only clear if the last track was played to near completion (within 5 seconds of end)
        if seconds_played >= (duration or 3600) - 5:
            self.mass.create_task(_clear_queue_delayed())

    def _handle_playback_progress_report(
        self, queue: PlayerQueue, prev_state: CompareState, new_state: CompareState
    ) -> None:
        """Handle playback progress report."""
        # detect change in current index to report that a item has been played
        prev_item_id = prev_state["current_item_id"]
        cur_item_id = new_state["current_item_id"]
        if prev_item_id is None and cur_item_id is None:
            return

        if prev_item_id is not None and prev_item_id != cur_item_id:
            # we have a new item, so we need report the previous one
            is_current_item = False
            item_to_report = prev_state["current_item"]
            seconds_played = int(prev_state["elapsed_time"])
        else:
            # report on current item
            is_current_item = True
            item_to_report = self.get_item(queue.queue_id, cur_item_id) or new_state["current_item"]
            seconds_played = int(new_state["elapsed_time"])

        if not item_to_report:
            return  # guard against invalid items

        if not (media_item := item_to_report.media_item):
            # only report on media items
            return
        assert media_item.uri is not None  # uri is set in __post_init__

        if item_to_report.streamdetails and item_to_report.streamdetails.duration:
            duration = int(item_to_report.streamdetails.duration)
        else:
            duration = int(item_to_report.duration or 3 * 3600)

        if seconds_played < 5:
            # ignore items that have been played less than 5 seconds
            # this also filters out a bounce effect where the previous item
            # gets reported with 0 elapsed seconds after a new item starts playing
            return

        # determine if item is fully played
        # for podcasts and audiobooks we account for the last 60 seconds
        percentage_played = percentage(seconds_played, duration)
        if not is_current_item and item_to_report.media_type in (
            MediaType.AUDIOBOOK,
            MediaType.PODCAST_EPISODE,
        ):
            fully_played = seconds_played >= duration - 60
        elif not is_current_item:
            # 90% of the track must be played to be considered fully played
            fully_played = percentage_played >= 90
        else:
            fully_played = seconds_played >= duration - 10

        is_playing = is_current_item and queue.state == PlaybackState.PLAYING
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            self.logger.debug(
                "%s %s '%s' (%s) - Fully played: %s - Progress: %s (%s/%ss)",
                queue.display_name,
                "is playing" if is_playing else "played",
                item_to_report.name,
                item_to_report.uri,
                fully_played,
                f"{percentage_played}%",
                seconds_played,
                duration,
            )
        # add entry to playlog - this also handles resume of podcasts/audiobooks
        self.mass.create_task(
            self.mass.music.mark_item_played(
                media_item,
                fully_played=fully_played,
                seconds_played=seconds_played,
                is_playing=is_playing,
                userid=queue.userid,
                queue_id=queue.queue_id,
                user_initiated=False,
            )
        )

        album: Album | ItemMapping | None = getattr(media_item, "album", None)
        # signal 'media item played' event,
        # which is useful for plugins that want to do scrobbling
        artists: list[Artist | ItemMapping] = getattr(media_item, "artists", [])
        artists_names = [a.name for a in artists]
        self.mass.signal_event(
            EventType.MEDIA_ITEM_PLAYED,
            object_id=media_item.uri,
            data=MediaItemPlaybackProgressReport(
                uri=media_item.uri,
                media_type=media_item.media_type,
                name=media_item.name,
                version=getattr(media_item, "version", None),
                artist=(
                    getattr(media_item, "artist_str", None) or artists_names[0]
                    if artists_names
                    else None
                ),
                artists=artists_names,
                artist_mbids=[a.mbid for a in artists if a.mbid] if artists else None,
                album=album.name if album else None,
                album_mbid=album.mbid if album else None,
                album_artist=(album.artist_str if isinstance(album, Album) else None),
                album_artist_mbids=(
                    [a.mbid for a in album.artists if a.mbid] if isinstance(album, Album) else None
                ),
                image_url=(
                    self.mass.metadata.get_image_url(
                        item_to_report.media_item.image, prefer_proxy=False
                    )
                    if item_to_report.media_item.image
                    else None
                ),
                duration=duration,
                mbid=(getattr(media_item, "mbid", None)),
                seconds_played=seconds_played,
                fully_played=fully_played,
                is_playing=is_playing,
                userid=queue.userid,
            ),
        )


async def _smart_shuffle(items: list[QueueItem]) -> list[QueueItem]:
    """Shuffle queue items, avoiding identical tracks next to each other.

    Best-effort approach to prevent the same track from appearing adjacent.
    Does a random shuffle first, then makes a limited number of passes to
    swap adjacent duplicates with a random item further in the list.

    :param items: List of queue items to shuffle.
    """
    if len(items) <= 2:
        return random.sample(items, len(items)) if len(items) == 2 else items

    # Start with a random shuffle
    shuffled = random.sample(items, len(items))

    # Make a few passes to fix adjacent duplicates
    max_passes = 3
    for _ in range(max_passes):
        swapped = False
        for i in range(len(shuffled) - 1):
            if shuffled[i].name == shuffled[i + 1].name:
                # Found adjacent duplicate - swap with random position at least 2 away
                swap_candidates = [j for j in range(len(shuffled)) if abs(j - i - 1) >= 2]
                if swap_candidates:
                    swap_pos = random.choice(swap_candidates)
                    shuffled[i + 1], shuffled[swap_pos] = shuffled[swap_pos], shuffled[i + 1]
                    swapped = True
        if not swapped:
            break
        # Yield to event loop between passes
        await asyncio.sleep(0)

    return shuffled
