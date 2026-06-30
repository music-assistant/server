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
from typing import TYPE_CHECKING, Any, cast

import shortuuid
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    CrossfadeMode,
    EventType,
    MediaType,
    PlaybackState,
    PlayerType,
    ProviderFeature,
    QueueOption,
    RepeatMode,
)
from music_assistant_models.errors import (
    AudioError,
    InsufficientPermissions,
    InvalidCommand,
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    PlayerUnavailableError,
    QueueEmpty,
)
from music_assistant_models.media_items import (
    Audiobook,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    PlayableMediaItemType,
    Playlist,
    PodcastEpisode,
    Track,
    media_from_dict,
)
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import (
    ATTR_ACTIVE_PLAYLIST,
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    CONF_CROSSFADE_MODE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_VOLUME_NORMALIZATION,
    MASS_LOGO_ONLINE,
    PLAYLIST_MEDIA_TYPES,
)
from music_assistant.controllers.player_queues.autoplay import (
    AUTOPLAY_MODE_DEFAULT_VALUE,
    Autoplay,
    AutoplayMode,
)
from music_assistant.controllers.player_queues.constants import (
    CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
    CACHE_CATEGORY_PLAYER_QUEUE_STATE,
    CONF_AUTOPLAY_LABEL,
    CONF_AUTOPLAY_MODE,
    CONF_AUTOPLAY_PLAYLIST,
    CONF_CROSSFADE_LABEL,
    CONF_DEFAULT_ENQUEUE_OPTION_ALBUM,
    CONF_DEFAULT_ENQUEUE_OPTION_ARTIST,
    CONF_DEFAULT_ENQUEUE_OPTION_AUDIOBOOK,
    CONF_DEFAULT_ENQUEUE_OPTION_FOLDER,
    CONF_DEFAULT_ENQUEUE_OPTION_GENRE,
    CONF_DEFAULT_ENQUEUE_OPTION_LIVE_SOURCES,
    CONF_DEFAULT_ENQUEUE_OPTION_PLAYLIST,
    CONF_DEFAULT_ENQUEUE_OPTION_PODCAST,
    CONF_DEFAULT_ENQUEUE_OPTION_PODCAST_EPISODE,
    CONF_DEFAULT_ENQUEUE_OPTION_TRACK,
    CONF_DEFAULT_ENQUEUE_SELECT_ALBUM,
    CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
    CONF_SMART_SHUFFLE_ARTIST_RECENCY,
    CONF_SMART_SHUFFLE_DUPLICATE_GAP,
    CONF_SMART_SHUFFLE_ENABLED,
    CONF_SMART_SHUFFLE_LABEL,
    CONF_SMART_SHUFFLE_SONG_RECENCY,
    ENQUEUE_SELECT_ALBUM_DEFAULT_VALUE,
    ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE,
    SMART_SHUFFLE_ARTIST_RECENCY_DEFAULT,
    SMART_SHUFFLE_ARTIST_RECENCY_OPTIONS,
    SMART_SHUFFLE_DUPLICATE_GAP_DEFAULT,
    SMART_SHUFFLE_DUPLICATE_GAP_OPTIONS,
    SMART_SHUFFLE_ENABLED_DEFAULT,
    SMART_SHUFFLE_SONG_RECENCY_DEFAULT,
    SMART_SHUFFLE_SONG_RECENCY_OPTIONS,
)
from music_assistant.controllers.player_queues.helpers import (
    get_current_playback_speed,
    handle_play_action,
    has_dynamic_source,
)
from music_assistant.controllers.player_queues.managed_pool import ManagedPool
from music_assistant.controllers.player_queues.media_resolver import MediaResolver
from music_assistant.controllers.player_queues.playback_tracker import PlaybackTracker
from music_assistant.controllers.player_queues.queue_loader import QueueLoader
from music_assistant.controllers.player_queues.smart_shuffle import SmartShuffle
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.player_queues.stream_feeder import StreamFeeder
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    ImpersonatedUser,
    get_current_user,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.throttle_retry import BYPASS_THROTTLER
from music_assistant.models.core_controller import CoreController
from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant_models import BackgroundTask

    from music_assistant import MusicAssistant
    from music_assistant.models.player import Player
    from music_assistant.providers.radio_playlist import RadioPlaylistProvider


class PlayerQueuesController(CoreController):
    """Controller holding all logic to enqueue music for players."""

    domain: str = "player_queues"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize core controller."""
        super().__init__(mass)
        # server-side per-queue records, keyed by queue_id; each bundles the wire PlayerQueue with
        # its items, dynamic-source items and runtime-only state (see PlayerQueueData)
        self._queue_data: dict[str, PlayerQueueData] = {}
        self._autoplay = Autoplay(self)
        self._smart_shuffle = SmartShuffle(self)
        self._managed_pool = ManagedPool(self)
        self._media_resolver = MediaResolver(self)
        self._playback_tracker = PlaybackTracker(self)
        self._stream_feeder = StreamFeeder(self)
        self._queue_loader = QueueLoader(self)
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
        enqueue_options = [
            ConfigValueOption(QueueOption.PLAY.value),
            ConfigValueOption(QueueOption.REPLACE.value),
        ]
        return (
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_SELECT_ARTIST,
                type=ConfigEntryType.STRING,
                default_value=ENQUEUE_SELECT_ARTIST_DEFAULT_VALUE,
                options=[
                    ConfigValueOption("top_tracks"),
                    ConfigValueOption("library_tracks"),
                    ConfigValueOption("prefer_library"),
                    ConfigValueOption("all_tracks"),
                ],
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_SELECT_ALBUM,
                type=ConfigEntryType.STRING,
                default_value=ENQUEUE_SELECT_ALBUM_DEFAULT_VALUE,
                options=[
                    ConfigValueOption("library_tracks"),
                    ConfigValueOption("all_tracks"),
                ],
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_ARTIST,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.REPLACE.value,
                options=enqueue_options,
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_ALBUM,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.REPLACE.value,
                options=enqueue_options,
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_TRACK,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.PLAY.value,
                options=enqueue_options,
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_GENRE,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.REPLACE.value,
                options=enqueue_options,
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_LIVE_SOURCES,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.REPLACE.value,
                options=enqueue_options,
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_PLAYLIST,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.REPLACE.value,
                options=enqueue_options,
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_AUDIOBOOK,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.REPLACE.value,
                options=enqueue_options,
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_PODCAST,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.REPLACE.value,
                options=enqueue_options,
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_PODCAST_EPISODE,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.REPLACE.value,
                options=enqueue_options,
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_DEFAULT_ENQUEUE_OPTION_FOLDER,
                type=ConfigEntryType.STRING,
                default_value=QueueOption.REPLACE.value,
                options=enqueue_options,
                hidden=True,
            ),
        )

    def get_queue_config_entries(
        self, playlist_options: list[ConfigValueOption] | None = None
    ) -> list[ConfigEntry]:
        """
        Return the per-queue config entries.

        The autoplay_mode select disables the 'similar' option when no provider can supply
        similar tracks. The crossfade_mode select's options and default depend on whether smart
        fades are available: the smart option is disabled (shown but not selectable) and the
        default falls back to standard crossfade when smart fades can't be used on this server.

        :param playlist_options: Library playlists to offer for the 'playlist' autoplay mode.
            Only populated when serving the entries to the UI; the parse path can omit it.
        """
        similar_tracks_available = any(
            ProviderFeature.SIMILAR_TRACKS in provider.supported_features
            for provider in self.mass.music.providers
        )
        autoplay_entries = [
            ConfigEntry(
                key=CONF_AUTOPLAY_LABEL,
                type=ConfigEntryType.LABEL,
                category="autoplay",
            ),
            ConfigEntry(
                key=CONF_AUTOPLAY_MODE,
                type=ConfigEntryType.STRING,
                options=[
                    ConfigValueOption(AutoplayMode.AUTO.value),
                    ConfigValueOption(
                        AutoplayMode.SIMILAR.value, disabled=not similar_tracks_available
                    ),
                    ConfigValueOption(AutoplayMode.LIBRARY.value),
                    ConfigValueOption(AutoplayMode.PLAYLIST.value),
                ],
                default_value=AUTOPLAY_MODE_DEFAULT_VALUE,
                category="autoplay",
            ),
            ConfigEntry(
                key=CONF_AUTOPLAY_PLAYLIST,
                type=ConfigEntryType.STRING,
                options=playlist_options or [],
                default_value=None,
                required=False,
                depends_on=CONF_AUTOPLAY_MODE,
                depends_on_value=AutoplayMode.PLAYLIST.value,
                category="autoplay",
            ),
        ]
        smart_fades_available = self.mass.streams.smart_fades_available
        crossfade_mode_entry = ConfigEntry(
            key=CONF_CROSSFADE_MODE,
            type=ConfigEntryType.STRING,
            options=[
                ConfigValueOption(CrossfadeMode.STANDARD_CROSSFADE.value),
                ConfigValueOption(
                    CrossfadeMode.SMART_CROSSFADE.value, disabled=not smart_fades_available
                ),
            ],
            default_value=(
                CrossfadeMode.SMART_CROSSFADE.value
                if smart_fades_available
                else CrossfadeMode.STANDARD_CROSSFADE.value
            ),
            category="crossfade",
            requires_reload=True,
        )
        crossfade_entries = [
            ConfigEntry(
                key=CONF_CROSSFADE_LABEL,
                type=ConfigEntryType.LABEL,
                category="crossfade",
            ),
            crossfade_mode_entry,
            CONF_ENTRY_CROSSFADE_DURATION,
        ]
        smart_shuffle_entries = [
            ConfigEntry(
                key=CONF_SMART_SHUFFLE_LABEL,
                type=ConfigEntryType.LABEL,
                category="smart_shuffle",
            ),
            ConfigEntry(
                key=CONF_SMART_SHUFFLE_ENABLED,
                type=ConfigEntryType.BOOLEAN,
                default_value=SMART_SHUFFLE_ENABLED_DEFAULT,
                category="smart_shuffle",
            ),
            ConfigEntry(
                key=CONF_SMART_SHUFFLE_SONG_RECENCY,
                type=ConfigEntryType.STRING,
                options=[
                    ConfigValueOption(str(seconds))
                    for seconds in SMART_SHUFFLE_SONG_RECENCY_OPTIONS
                ],
                default_value=str(SMART_SHUFFLE_SONG_RECENCY_DEFAULT),
                category="smart_shuffle",
                depends_on=CONF_SMART_SHUFFLE_ENABLED,
                depends_on_value=True,
            ),
            ConfigEntry(
                key=CONF_SMART_SHUFFLE_ARTIST_RECENCY,
                type=ConfigEntryType.STRING,
                options=[
                    ConfigValueOption(str(seconds))
                    for seconds in SMART_SHUFFLE_ARTIST_RECENCY_OPTIONS
                ],
                default_value=str(SMART_SHUFFLE_ARTIST_RECENCY_DEFAULT),
                category="smart_shuffle",
                depends_on=CONF_SMART_SHUFFLE_ENABLED,
                depends_on_value=True,
            ),
            ConfigEntry(
                key=CONF_SMART_SHUFFLE_DUPLICATE_GAP,
                type=ConfigEntryType.STRING,
                options=[
                    ConfigValueOption(str(seconds))
                    for seconds in SMART_SHUFFLE_DUPLICATE_GAP_OPTIONS
                ],
                default_value=str(SMART_SHUFFLE_DUPLICATE_GAP_DEFAULT),
                category="smart_shuffle",
                depends_on=CONF_SMART_SHUFFLE_ENABLED,
                depends_on_value=True,
                advanced=True,
            ),
        ]
        return [
            *smart_shuffle_entries,
            *autoplay_entries,
            *crossfade_entries,
            CONF_ENTRY_VOLUME_NORMALIZATION,
        ]

    def __iter__(self) -> Iterator[PlayerQueue]:
        """Iterate over (available) players."""
        return iter(data.queue for data in self._queue_data.values())

    @api_command("player_queues/all")
    def all(self) -> tuple[PlayerQueue, ...]:
        """Return all registered PlayerQueues."""
        return tuple(data.queue for data in self._queue_data.values())

    @api_command("player_queues/get")
    def get(self, queue_id: str) -> PlayerQueue | None:
        """Return PlayerQueue by queue_id or None if not found."""
        data = self._queue_data.get(queue_id)
        return data.queue if data else None

    @api_command("player_queues/items")
    def items(self, queue_id: str, limit: int = 500, offset: int = 0) -> list[QueueItem]:
        """Return all QueueItems for given PlayerQueue."""
        if (data := self._queue_data.get(queue_id)) is None:
            return []
        return data.items[offset : offset + limit]

    @api_command("player_queues/get_active_queue")
    def get_active_queue(self, player_id: str) -> PlayerQueue | None:
        """Return the current active/synced queue for a player."""
        if player := self.mass.players.get_player(player_id):
            return self.mass.players.get_active_queue(player)
        return None

    # Queue commands

    @api_command("player_queues/shuffle")
    async def set_shuffle(self, queue_id: str, shuffle_enabled: bool) -> None:
        """Configure shuffle setting on the the queue."""
        queue = self._queue_data[queue_id].queue
        if queue.shuffle_enabled == shuffle_enabled:
            return  # no change
        queue.shuffle_enabled = shuffle_enabled
        queue.smart_shuffle_active = self.is_smart_shuffle_active(queue)
        queue_items = self._queue_data[queue_id].items
        cur_index = (
            queue.index_in_buffer if queue.index_in_buffer is not None else queue.current_index
        )
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

    def is_smart_shuffle_active(self, queue: PlayerQueue) -> bool:
        """
        Return whether smart shuffle is currently in effect for the queue.

        Radio mode implies smart selection, so it always counts as active; otherwise smart shuffle
        is active when shuffle is on and the per-queue smart-shuffle setting is enabled.

        :param queue: The queue to evaluate.
        """
        if queue.sources:
            return True
        return queue.shuffle_enabled and self._smart_shuffle.is_enabled(queue.queue_id)

    @api_command("player_queues/autoplay")
    def set_autoplay(self, queue_id: str, autoplay_enabled: bool) -> None:
        """Configure Autoplay setting on the queue."""
        queue = self._queue_data[queue_id].queue
        queue.autoplay_enabled = autoplay_enabled
        # if we're already at/near the end of the queue, kick off a refill right away
        # (an active dynamic source manages its own refills, so leave it be)
        if (
            queue.autoplay_enabled
            and queue.enqueued_media_items
            and not queue.sources
            and queue.current_index is not None
            and (queue.items - queue.current_index) < 5
        ):
            task_id = f"fill_autoplay_tracks_{queue_id}"
            self.mass.call_later(
                5, self._queue_loader._fill_autoplay_tracks, queue_id, task_id=task_id
            )
        self.signal_update(queue_id=queue_id)

    @api_command("player_queues/dont_stop_the_music", alias=True)
    def set_dont_stop_the_music(self, queue_id: str, dont_stop_the_music_enabled: bool) -> None:
        """Backwards-compatible alias for the autoplay command, used by older clients."""
        self.set_autoplay(queue_id, dont_stop_the_music_enabled)

    @api_command("player_queues/repeat")
    def set_repeat(self, queue_id: str, repeat_mode: RepeatMode) -> None:
        """Configure repeat setting on the the queue."""
        queue = self._queue_data[queue_id].queue
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
                self._stream_feeder._enqueue_next_item(queue_id, next_item)

    @api_command("player_queues/crossfade")
    def set_crossfade(self, queue_id: str, crossfade_enabled: bool) -> None:
        """Enable or disable crossfade on the queue."""
        queue = self._queue_data[queue_id].queue
        if queue.crossfade_enabled == crossfade_enabled:
            return  # no change
        queue.crossfade_enabled = crossfade_enabled
        # refresh the derived smart-fades indicator so the update we signal reflects the new state
        queue.smart_fades_active = self.mass.streams.is_smart_fades_active(queue)
        self.signal_update(queue_id)
        if (
            queue.state == PlaybackState.PLAYING
            and queue.index_in_buffer is not None
            and queue.index_in_buffer == queue.current_index
        ):
            # re-enqueue the next track so the new crossfade behaviour applies to the
            # upcoming transition (only when the player has already loaded the current track)
            if next_item := self.get_next_item(queue_id, queue.index_in_buffer):
                self._stream_feeder._enqueue_next_item(queue_id, next_item)

    # Two timebases are used in this controller when variable playback speed is in
    # effect (atempo applied server-side):
    #   "stream-time"  — seconds of audio the player has played (post-atempo).
    #   "media-time"   — seconds of the original content the listener has heard.
    #                    What the user expects to see on the progress bar and what
    #                    we use for resume positions.
    # Conversion: media-time = stream-time x playback_speed.
    @api_command("player_queues/set_playback_speed")
    async def set_playback_speed(
        self, queue_id: str, speed: float, queue_item_id: str | None = None
    ) -> None:
        """
        Set the playback speed for the given queue item.

        Variable playback speed is supported only for audiobooks and podcast episodes.

        If queue_item_id is not provided,
        the speed will be set for the current item in the queue.

        :param queue_id: queue_id of the queue to configure.
        :param speed: playback speed multiplier (0.5 to 3.0). 1.0 = normal speed.
        """
        if not (0.5 <= speed <= 3.0):
            raise InvalidDataError(f"Playback speed must be between 0.5 and 3.0, got {speed}")
        queue = self._queue_data[queue_id].queue
        if not queue.current_item:
            raise QueueEmpty("Cannot set playback speed: queue is empty")
        queue_item_id = queue_item_id or queue.current_item.queue_item_id
        queue_item = self.get_item(queue_id, queue_item_id)
        if not queue_item:
            raise InvalidDataError(f"Queue item {queue_item_id} not found in queue")
        if queue_item.media_type not in (MediaType.AUDIOBOOK, MediaType.PODCAST_EPISODE):
            raise InvalidCommand(
                "Variable playback speed is only supported for audiobooks and podcast episodes"
            )
        if not queue_item.duration:
            raise InvalidCommand("Cannot set playback speed for items with unknown duration")
        current_speed = float(queue_item.extra_attributes.get("playback_speed") or 1.0)
        if abs(current_speed - speed) < 0.001:
            return  # no change
        # use extra_attributes of the queue item to store the playback speed
        queue_item.extra_attributes["playback_speed"] = speed
        # mirror onto the queue so corrected_elapsed_time advances in media-time
        # immediately, before the next on_player_elapsed_time_corrected snapshot.
        if queue.current_item and queue.current_item.queue_item_id == queue_item_id:
            # close off the wallclock seconds that already ticked by at the old speed
            # before switching, so corrected_elapsed_time doesn't multiply them by the new speed
            if queue.state == PlaybackState.PLAYING:
                queue.elapsed_time = queue.corrected_elapsed_time
                queue.elapsed_time_last_updated = time.time()
            queue.playback_speed = speed
        self.signal_update(queue_id)
        if queue.state == PlaybackState.PLAYING:
            await self.resume(queue_id)

    @api_command("player_queues/play_media")
    async def play_media(
        self,
        queue_id: str,
        media: MediaItemType | ItemMapping | str | list[MediaItemType | ItemMapping | str],
        option: QueueOption | None = None,
        radio_mode: bool = False,
        start_item: PlayableMediaItemType | str | None = None,
        username: str | None = None,
        sort_by: str | None = None,
    ) -> None:
        """
        Play media item(s) on the given queue.

        :param queue_id: The queue_id of the queue to play media on.
        :param media: Media that should be played (MediaItem(s) and/or uri's).
        :param option: Which enqueue mode to use.
        :param radio_mode: Deprecated — translated to a radio_playlist:// dynamic playlist;
            prefer enqueuing that URI directly.
        :param start_item: Optional item to start the playlist or album from.
        :param username: The username of the user requesting the playback.
            Setting the username allows for overriding the logged-in user
            to account for playback history per user when the play_media is
            called from a shared context (like a web hook or automation).
        :param sort_by: Optional sort key to order tracks before applying start_item.
        """
        self._check_player_permission(queue_id)
        if not self.get(queue_id):
            raise PlayerUnavailableError(f"Queue {queue_id} is not available")
        # Lock is acquired by the @handle_play_action decorator on the internal handler
        async with ImpersonatedUser(self.mass, username):
            await self._handle_play_media(queue_id, media, option, radio_mode, start_item, sort_by)

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
        queue = self._queue_data[queue_id].queue
        item_index = self.index_by_id(queue_id, queue_item_id)
        if item_index is None:
            raise InvalidDataError(f"Item {queue_item_id} not found in queue")
        if queue.index_in_buffer is not None and item_index <= queue.index_in_buffer:
            msg = f"{item_index} is already played/buffered"
            raise IndexError(msg)

        queue_items = self._queue_data[queue_id].items
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

    @api_command("player_queues/move_item_end")
    def move_item_end(self, queue_id: str, queue_item_id: str) -> None:
        """
        Move queue item to the end the queue.

        - queue_id: id of the queue to process this request.
        - queue_item_id: the item_id of the queueitem that needs to be moved.
        """
        queue = self._queue_data[queue_id].queue
        item_index = self.index_by_id(queue_id, queue_item_id)
        if item_index is None:
            raise InvalidDataError(f"Item {queue_item_id} not found in queue")
        if queue.index_in_buffer is not None and item_index <= queue.index_in_buffer:
            msg = f"{item_index} is already played/buffered"
            raise IndexError(msg)

        queue_items = self._queue_data[queue_id].items
        if item_index == (len(queue_items) - 1):
            return
        queue_items = queue_items.copy()

        new_index = len(self._queue_data[queue_id].items) - 1

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
        queue = self._queue_data[queue_id].queue
        if queue.index_in_buffer is not None and item_index <= queue.index_in_buffer:
            # ignore request if track already loaded in the buffer
            # the frontend should guard so this is just in case
            self.logger.warning("delete requested for item already loaded in buffer")
            return
        queue_items = self._queue_data[queue_id].items.copy()
        queue_items.pop(item_index)
        self.update_items(queue_id, queue_items)

    @api_command("player_queues/clear")
    def clear(self, queue_id: str, skip_stop: bool = False) -> None:
        """Clear all items in the queue."""
        queue = self._queue_data[queue_id].queue
        self._store_sources(queue, [])
        queue.is_dynamic = False
        if queue.state != PlaybackState.IDLE and not skip_stop:
            self.mass.create_task(self.stop(queue_id))
        queue.current_index = None
        queue.current_item = None
        queue.elapsed_time = 0
        queue.elapsed_time_last_updated = time.time()
        queue.index_in_buffer = None
        self.mass.create_task(self._stream_feeder._cleanup_queue_audio_data(queue_id))
        self.update_items(queue_id, [])

    @api_command("player_queues/save_as_playlist")
    async def save_as_playlist(self, queue_id: str, name: str) -> BackgroundTask:
        """
        Save the current queue items as a new playlist.

        :param queue_id: The queue_id of the queue to save.
        :param name: The name for the new playlist.
        """
        if not self.get(queue_id):
            raise PlayerUnavailableError(f"Queue {queue_id} is not available")
        queue_items = data.items if (data := self._queue_data.get(queue_id)) else []
        if not queue_items:
            raise QueueEmpty("Cannot save an empty queue as a playlist.")
        # collect URIs from queue items that are playlist-compatible
        uris: list[str] = []
        for item in queue_items:
            if item.uri and item.media_type in PLAYLIST_MEDIA_TYPES:
                uris.append(item.uri)
        if not uris:
            raise InvalidDataError("No valid items in queue to save as playlist.")
        playlist = await self.mass.music.playlists.create_playlist(name)
        return await self.mass.music.playlists.add_playlist_tracks(playlist.item_id, uris)

    @api_command("player_queues/stop")
    @handle_play_action
    async def stop(self, queue_id: str) -> None:
        """
        Handle STOP command for given queue.

        - queue_id: queue_id of the playerqueue to handle the command.
        """
        self._check_player_permission(queue_id)
        # cancel any pending play_index calls for this queue to prevent conflicts
        self.mass.cancel_timer(f"queue_play_index_{queue_id}")
        # cancel in-flight preload/enqueue-next so it can't enqueue after stop
        self.mass.cancel_task(f"preload_next_item_{queue_id}")
        self.mass.cancel_timer(f"enqueue_next_item_{queue_id}")
        self.mass.cancel_task(f"enqueue_next_item_{queue_id}")
        self._set_transitioning(queue_id, False)
        queue_player = self.mass.players.get_player(queue_id, True)
        if queue_player is None:
            raise PlayerUnavailableError(f"Player {queue_id} is not available")
        if (queue := self.get(queue_id)) and queue.active:
            if queue.state == PlaybackState.PLAYING:
                queue.resume_pos = int(queue.corrected_elapsed_time)
        # Use internal handler to avoid circular redirect:
        # public cmd_stop redirects to queue.stop when a queue is active,
        # which would loop back here indefinitely.
        await self.mass.players._handle_cmd_stop(queue_id)
        self.mass.create_task(self._stream_feeder._cleanup_queue_audio_data(queue_id))

    @api_command("player_queues/play")
    async def play(self, queue_id: str) -> None:
        """
        Handle PLAY command for given queue.

        :param queue_id: queue_id of the playerqueue to handle the command.
        """
        self._check_player_permission(queue_id)
        if not self.get(queue_id):
            raise PlayerUnavailableError(f"Queue {queue_id} is not available")
        await self._handle_play(queue_id)

    @api_command("player_queues/pause")
    async def pause(self, queue_id: str) -> None:
        """
        Handle PAUSE command for given queue.

        - queue_id: queue_id of the playerqueue to handle the command.
        """
        self._check_player_permission(queue_id)
        # cancel any pending play_index calls for this queue to prevent conflicts
        self.mass.cancel_timer(f"queue_play_index_{queue_id}")
        self._set_transitioning(queue_id, False)
        if not (queue := self.get(queue_id)):
            return
        queue_active = queue.active
        if queue.active and queue.state == PlaybackState.PLAYING:
            queue.resume_pos = int(queue.corrected_elapsed_time)
        # Use internal handler to avoid circular redirect
        # (cmd_pause redirects to queue.pause, which calls cmd_pause again)
        await self.mass.players._handle_cmd_pause(queue_id)

        async def _watch_pause(player: Player) -> None:
            count = 0
            # wait for pause
            while count < 5 and player.state.playback_state == PlaybackState.PLAYING:
                count += 1
                await asyncio.sleep(1)
            # wait for unpause
            if player.state.playback_state != PlaybackState.PAUSED:
                return
            count = 0
            while count < 30 and player.state.playback_state == PlaybackState.PAUSED:
                count += 1
                await asyncio.sleep(1)
            # if player is still paused when the limit is reached, send stop
            if player.state.playback_state == PlaybackState.PAUSED:
                await self.stop(queue_id)

        # we auto stop a player from paused when its paused for 30 seconds
        if (
            queue_active
            and (queue_player := self.mass.players.get_player(queue_id))
            and not queue_player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS)
        ):
            self.mass.create_task(_watch_pause(queue_player))

    @api_command("player_queues/play_pause")
    async def play_pause(self, queue_id: str) -> None:
        """
        Toggle play/pause on given playerqueue.

        - queue_id: queue_id of the queue to handle the command.
        """
        if (queue := self.get(queue_id)) and queue.state == PlaybackState.PLAYING:
            await self.pause(queue_id)
            return
        await self.play(queue_id)

    @api_command("player_queues/next")
    @handle_play_action
    async def next(self, queue_id: str) -> None:
        """
        Handle NEXT TRACK command for given queue.

        :param queue_id: queue_id of the queue to handle the command.
        """
        self._check_player_permission(queue_id)
        if (queue := self.get(queue_id)) is None or not queue.active:
            raise InvalidCommand(f"Queue {queue_id} is not active")
        self._set_transitioning(queue_id, True)
        idx = self._queue_data[queue_id].queue.current_index
        if idx is None:
            self.logger.warning("Queue %s has no current index", queue.display_name)
            self._set_transitioning(queue_id, False)
            return
        next_index = self._queue_loader._get_next_index(queue_id, idx, True)
        if next_index is None:
            self._set_transitioning(queue_id, False)
            return

        # immediately update current item so UI shows the new track right away
        queue.current_index = next_index
        queue.current_item = self.get_item(queue_id, next_index)
        queue.elapsed_time = 0
        queue.elapsed_time_last_updated = time.time()
        self.signal_update(queue_id)
        if queue_player := self.mass.players.get_player(queue_id, True):
            queue_player.update_state()

        # debounce rapid next button presses using call_later
        self.mass.call_later(
            1,
            self.play_index,
            queue_id,
            next_index,
            task_id=f"queue_play_index_{queue_id}",
        )

    @api_command("player_queues/previous")
    @handle_play_action
    async def previous(self, queue_id: str) -> None:
        """
        Handle PREVIOUS TRACK command for given queue.

        :param queue_id: queue_id of the queue to handle the command.
        """
        self._check_player_permission(queue_id)
        if (queue := self.get(queue_id)) is None or not queue.active:
            raise InvalidCommand(f"Queue {queue_id} is not active")
        self._set_transitioning(queue_id, True)
        current_index = self._queue_data[queue_id].queue.current_index
        if current_index is None:
            self._set_transitioning(queue_id, False)
            return
        prev_index = int(current_index)
        # restart current track if elapsed > 5s, otherwise go to previous
        if self._queue_data[queue_id].queue.elapsed_time < 5:
            prev_index = max(current_index - 1, 0)

        # immediately update current item so UI shows the new track right away
        queue.current_index = prev_index
        queue.current_item = self.get_item(queue_id, prev_index)
        queue.elapsed_time = 0
        queue.elapsed_time_last_updated = time.time()
        self.signal_update(queue_id)
        if queue_player := self.mass.players.get_player(queue_id, True):
            queue_player.update_state()

        # debounce rapid previous button presses using call_later
        self.mass.call_later(
            1,
            self.play_index,
            queue_id,
            prev_index,
            task_id=f"queue_play_index_{queue_id}",
        )

    @api_command("player_queues/skip")
    async def skip(self, queue_id: str, seconds: int = 10) -> None:
        """
        Handle SKIP command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        - seconds: number of seconds to skip in track. Use negative value to skip back.
        """
        if (queue := self.get(queue_id)) is None or not queue.active:
            raise InvalidCommand(f"Queue {queue_id} is not active")
        await self.seek(queue_id, int(self._queue_data[queue_id].queue.elapsed_time + seconds))

    @api_command("player_queues/seek")
    async def seek(self, queue_id: str, position: int = 10) -> None:
        """
        Handle SEEK command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        - position: position in seconds to seek to in the current playing item.
        """
        if (queue := self.get(queue_id)) is None or not queue.active:
            raise InvalidCommand(f"Queue {queue_id} is not active")
        queue_player = self.mass.players.get_player(queue_id, True)
        if queue_player is None:
            raise PlayerUnavailableError(f"Player {queue_id} is not available")
        if not queue.current_item:
            raise InvalidCommand(f"Queue {queue_player.state.name} has no item(s) loaded.")
        if not queue.current_item.duration:
            raise InvalidCommand("Can not seek items without duration.")
        position = max(0, int(position))
        if position > queue.current_item.duration:
            raise InvalidCommand("Can not seek outside of duration range.")
        if queue.current_index is None:
            raise InvalidCommand(f"Queue {queue_player.state.name} has no current index.")
        await self.play_index(queue_id, queue.current_index, seek_position=position)

    @api_command("player_queues/resume")
    @handle_play_action
    async def resume(self, queue_id: str, fade_in: bool | None = None) -> None:
        """
        Handle RESUME command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        """
        self._check_player_permission(queue_id)
        queue = self._queue_data[queue_id].queue
        queue_items = self._queue_data[queue_id].items
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
            queue_player = self.mass.players.get_player(queue_id)
            if queue_player is None:
                raise PlayerUnavailableError(f"Player {queue_id} is not available")
            if (
                fade_in is None
                and queue_player.state.playback_state == PlaybackState.IDLE
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
            if await self._queue_loader._try_resume_from_playlog(queue):
                return
            msg = f"Resume queue requested but queue {queue.display_name} is empty"
            raise QueueEmpty(msg)

    @api_command("player_queues/play_index")
    @handle_play_action
    async def play_index(
        self,
        queue_id: str,
        index: int | str,
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> None:
        """Play item at index (or item_id) X in queue."""
        self._check_player_permission(queue_id)
        # cancel any pending play_index calls for this queue to prevent conflicts
        self.mass.cancel_timer(f"queue_play_index_{queue_id}")
        # we set a flag to notify the update logic that we're transitioning to a new track
        self._set_transitioning(queue_id, True)
        try:
            queue = self._queue_data[queue_id].queue
            queue.resume_pos = 0
            if isinstance(index, str):
                temp_index = self.index_by_id(queue_id, index)
                if temp_index is None:
                    raise InvalidDataError(f"Item {index} not found in queue")
                index = temp_index
            # At this point index is guaranteed to be int
            queue.index_in_buffer = index
            queue.flow_mode_stream_log = []
            self._queue_data[queue_id].flow_buffer_completed = None
            target_player = self.mass.players.get_player(queue_id)
            if target_player is None:
                raise PlayerUnavailableError(f"Player {queue_id} is not available")
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

            # restore the persisted playback speed for a freshly queued audiobook/episode
            # (an in-session item already carries its speed in extra_attributes)
            if (
                (queue_item := self.get_item(queue_id, index))
                and queue_item.media_item is not None
                and queue_item.media_type in (MediaType.AUDIOBOOK, MediaType.PODCAST_EPISODE)
                and "playback_speed" not in queue_item.extra_attributes
            ):
                stored_speed = await self.mass.music.get_playback_speed(
                    cast("Audiobook | PodcastEpisode", queue_item.media_item),
                    userid=queue.userid,
                )
                if stored_speed != 1.0:
                    queue_item.extra_attributes["playback_speed"] = stored_speed

            # try to load the item, retry with next item if it fails
            for attempt in range(5):
                try:
                    queue_item = self.get_item(queue_id, index)
                    if not queue_item:
                        continue  # guard
                    await self._queue_loader._load_item(
                        queue_item,
                        self._queue_loader._get_next_index(queue_id, index),
                        is_start=True,
                        seek_position=seek_position if attempt == 0 else 0,
                        fade_in=fade_in if attempt == 0 else False,
                    )
                    # if we reach this point, loading the item succeeded, break the loop
                    queue.current_index = index
                    queue.current_item = queue_item
                    break
                except (MediaNotFoundError, AudioError) as err:
                    item_name = queue_item.name if queue_item else "unknown"
                    # Only MediaNotFoundError (item unreachable) is persistent;
                    # keep AudioError items available so a retry can resurface
                    # the same actionable error.
                    if queue_item and isinstance(err, MediaNotFoundError):
                        queue_item.available = False
                    next_index = self._queue_loader._get_next_index(
                        queue_id, index, allow_repeat=False
                    )
                    if next_index is None:
                        # Surface an AudioError's own (actionable) message;
                        # MediaNotFoundError gets the generic wording.
                        if isinstance(err, AudioError) and str(err):
                            msg = str(err)
                        else:
                            msg = f"Playback failed for {item_name} - no more tracks available"
                        self.logger.error(msg)
                        await self.stop(queue_id)
                        raise MediaNotFoundError(msg) from err
                    self.logger.warning(
                        "Skipping unplayable item %s",
                        item_name,
                    )
                    index = next_index
            else:
                # all attempts to find a playable item failed
                await self.stop(queue_id)
                raise MediaNotFoundError("No playable item found to start playback")

            # Reset flow_mode - the streams controller will set it if flow mode is used.
            queue.flow_mode = False
            await self.mass.players.play_media(
                queue_id,
                await self.player_media_from_queue_item(queue_item),
            )
            queue.current_index = index
            queue.current_item = queue_item
            self.signal_update(queue_id)
        finally:
            self._set_transitioning(queue_id, False)

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

        target_player = self.mass.players.get_player(target_queue_id)
        if target_player is None:
            raise PlayerUnavailableError(f"Player {target_queue_id} is not available")
        if target_player.state.active_group or target_player.state.synced_to:
            # edge case: the user wants to move playback from the group as a whole, to a single
            # player in the group or it is grouped and the command targeted at the single player.
            # We need to dissolve the group/sync first, and wait for the state to actually
            # propagate before we hand the queue over to the target player.
            group_id = target_player.state.active_group or target_player.state.synced_to
            assert group_id is not None  # checked in if condition above
            # For an ad-hoc sync group (target is a sync member of a regular leader),
            # ungroup the target itself so only it is freed - ungrouping the leader would
            # transfer leadership to a remaining member and recurse back into this method.
            # For a virtual group player (active_group), release the group so its static
            # members are handled correctly.
            ungroup_target = (
                target_queue_id
                if target_player.state.synced_to and not target_player.state.active_group
                else group_id
            )
            async with self.mass.players.wait_for_player_update(
                target_queue_id,
                attribute_name=(
                    "active_group" if target_player.state.active_group else "synced_to"
                ),
                attribute_value=None,
                timeout=5,
            ):
                await self.mass.players.cmd_ungroup(ungroup_target)

        # capture source state before stopping (stop resets these)
        source_items = self._queue_data[source_queue_id].items
        if source_queue.state == PlaybackState.PLAYING:
            # use the live playback clock while actively playing
            source_resume_pos = int(source_queue.corrected_elapsed_time)
        else:
            # when not playing the live clock is stale, so use the stored resume position
            source_resume_pos = int(source_queue.resume_pos or source_queue.elapsed_time or 0)
        source_current_index = source_queue.current_index
        source_current_item = source_queue.current_item

        # stop the source player synchronously to prevent the async stop from
        # clear() racing with the target's sync group formation/protocol switching
        if source_queue.state != PlaybackState.IDLE:
            await self.stop(source_queue_id)

        target_queue.repeat_mode = source_queue.repeat_mode
        target_queue.shuffle_enabled = source_queue.shuffle_enabled
        target_queue.crossfade_enabled = source_queue.crossfade_enabled
        # refresh the derived smart-fades indicator for the target's own config/availability
        target_queue.smart_fades_active = self.mass.streams.is_smart_fades_active(target_queue)
        target_queue.autoplay_enabled = source_queue.autoplay_enabled
        self._queue_data[target_queue_id].source_items = list(
            self._queue_data[source_queue_id].source_items
        )
        target_queue.sources = list(source_queue.sources)
        target_queue.is_dynamic = source_queue.is_dynamic
        target_queue.smart_shuffle_active = self.is_smart_shuffle_active(target_queue)
        target_queue.enqueued_media_items = source_queue.enqueued_media_items
        target_queue.resume_pos = source_resume_pos
        target_queue.current_index = source_current_index
        if source_current_item:
            target_queue.current_item = source_current_item
            target_queue.current_item.queue_id = target_queue_id
        self.clear(source_queue_id, skip_stop=True)

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
        data: PlayerQueueData | None = None
        # try to restore previous state
        if prev_state := await self.mass.cache.get(
            key=queue_id,
            provider=self.domain,
            category=CACHE_CATEGORY_PLAYER_QUEUE_STATE,
        ):
            try:
                prev_items = await self.mass.cache.get(
                    key=queue_id,
                    provider=self.domain,
                    category=CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
                    default=[],
                )
                data = PlayerQueueData.from_cache(prev_state, prev_items)
            except Exception as err:
                self.logger.warning(
                    "Failed to restore the queue(items) for %s - %s",
                    player.state.name,
                    str(err),
                )
                # Reset to clean state on failure
                data = None
        if data is None:
            data = PlayerQueueData(
                queue=PlayerQueue(
                    queue_id=queue_id,
                    active=False,
                    display_name=player.state.name,
                    available=player.state.available,
                    autoplay_enabled=False,
                    items=0,
                )
            )

        self._queue_data[queue_id] = data
        # always call update to calculate state etc
        self.on_player_update(player, {})
        self.mass.signal_event(EventType.QUEUE_ADDED, object_id=queue_id, data=data.queue)

    def on_player_update(
        self,
        player: Player,
        changed_values: dict[str, tuple[Any, Any]],
    ) -> None:
        """
        Call when a PlayerQueue needs to be updated (e.g. when player updates).

        NOTE: This is called every second if the player is playing.
        """
        if player.type == PlayerType.PROTOCOL:
            # protocol players do not have a queue on their own
            return
        queue_id = player.player_id
        if (queue := self.get(queue_id)) is None:
            # race condition
            return
        if player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS):
            # do nothing while the announcement is in progress
            return
        # determine if this queue is currently active for this player
        queue.active = player.state.active_source in (queue.queue_id, None)
        if not queue.active and self._queue_data[queue_id].prev_state is None:
            queue.state = PlaybackState.IDLE
            # return early if the queue is not active and we have no previous state
            return
        if self._queue_data[queue_id].transitioning:
            # we're currently transitioning to a new track,
            # ignore updates from the player during this time
            return
        # queue is active and preflight checks passed, update the queue details
        self._playback_tracker._update_queue_from_player(player)

    def on_player_elapsed_time_corrected(self, player: Player) -> None:
        """Correct the queue's timing base if the player's real elapsed_time diverged."""
        if player.type == PlayerType.PROTOCOL:
            return
        queue_id = player.player_id
        if (queue := self.get(queue_id)) is None:
            return
        if not queue.active:
            return
        player_elapsed = player.state.corrected_elapsed_time
        if player_elapsed is None:
            return
        now = time.time()
        # queue.elapsed_time is stored in media-time so it can be displayed and
        # used as a resume position directly. The player reports stream-time
        # (post-atempo), so we scale by the current item's playback_speed.
        speed = get_current_playback_speed(queue)
        if queue.flow_mode:
            # _get_flow_queue_stream_index returns media-time in the current item
            # using each playlog entry's recorded speed.
            _, elapsed_time = self._playback_tracker._get_flow_queue_stream_index(queue, player)
        else:
            elapsed_time = player_elapsed * speed
            if queue.current_item and queue.current_item.streamdetails:
                if seek_pos := queue.current_item.streamdetails.seek_position:
                    elapsed_time += seek_pos
        queue.elapsed_time = elapsed_time
        queue.elapsed_time_last_updated = now
        queue.playback_speed = speed
        self.mass.signal_event(
            EventType.QUEUE_TIME_UPDATED,
            object_id=queue_id,
            data=queue.elapsed_time,
        )

    def on_player_remove(self, player_id: str, permanent: bool) -> None:
        """Call when a player is removed from the registry."""
        # cancel any pending play_index calls for this queue to prevent conflicts
        self.mass.cancel_timer(f"queue_play_index_{player_id}")
        self._set_transitioning(player_id, False)
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
        self._queue_data.pop(player_id, None)
        self._managed_pool.forget(player_id)

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
            next_index = self._queue_loader._get_next_index(queue_id, cur_index + idx)
            if next_index is None:
                raise QueueEmpty("No more tracks left in the queue.")
            queue_item = self.get_item(queue_id, next_index)
            if queue_item is None:
                raise QueueEmpty("No more tracks left in the queue.")
            if idx >= 10:
                # we only allow 10 retries to prevent infinite loops
                raise QueueEmpty("No more (playable) tracks left in the queue.")
            try:
                await self._queue_loader._load_item(queue_item, next_index)
                # we're all set, this is our next item
                next_item = queue_item
                break
            except MediaNotFoundError, AudioError:
                # No stream details found, skip this QueueItem
                self.logger.warning(
                    "Skipping unplayable item %s (%s)", queue_item.name, queue_item.uri
                )
                queue_item.available = False
                idx += 1
        if idx != 0:
            # we skipped some items, signal a queue items update
            self.update_items(queue_id, self._queue_data[queue_id].items)
        if next_item is None:
            raise QueueEmpty("No more (playable) tracks left in the queue.")

        # carry playback_speed forward across consecutive audiobook/podcast items
        current_item = self.get_item(queue_id, current_item_id)
        if (
            current_item
            and current_item.media_type in (MediaType.AUDIOBOOK, MediaType.PODCAST_EPISODE)
            and next_item.media_type in (MediaType.AUDIOBOOK, MediaType.PODCAST_EPISODE)
        ):
            next_item.extra_attributes["playback_speed"] = current_item.extra_attributes.get(
                "playback_speed", 1.0
            )

        return next_item

    def track_loaded_in_buffer(self, queue_id: str, item_id: str) -> None:
        """Call when a player has (started) loading a track in the buffer."""
        queue = self.get(queue_id)
        if not queue:
            msg = f"PlayerQueue {queue_id} is not available"
            raise PlayerUnavailableError(msg)
        # store the index of the item that is currently (being) loaded in the buffer
        # which helps us a bit to determine how far the player has buffered ahead
        current_index = self.index_by_id(queue_id, item_id)
        queue.index_in_buffer = current_index
        self.logger.debug("PlayerQueue %s loaded item %s in buffer", queue.display_name, item_id)
        self.signal_update(queue_id)
        # preload next streamdetails
        self._stream_feeder._preload_next_item(queue_id, item_id)
        # clean up stale audio buffers for old queue items to prevent memory leaks
        if current_index is not None:
            self.mass.create_task(
                self._stream_feeder._cleanup_stale_queue_buffers(queue_id, current_index)
            )

    def queue_buffer_completed(self, queue_id: str) -> None:
        """
        Call when the flow stream has finished generating all audio data for a queue.

        At this point all audio data for the queue has been passed to the encoding pipeline.
        The player will go idle once it finishes playing the remaining buffered audio.

        We start a background task that waits for the player to go idle and checks if new
        items have been added to the queue in the meantime, resuming playback if so.

        :param queue_id: The queue ID.
        """
        queue = self.get(queue_id)
        if not queue:
            return
        self.logger.debug("Queue flow buffer completed for %s", queue.display_name)

        # capture session_id so we can bail out if playback restarts
        original_session_id = queue.session_id
        # record so player providers can detect flow EOF without an idle report
        if original_session_id is not None:
            self._queue_data[queue_id].flow_buffer_completed = original_session_id

        async def _resume_on_idle() -> None:
            # wait for the player to finish playing the buffered audio and go idle
            idle_detected = False
            for _ in range(60):
                await asyncio.sleep(1)
                if not queue.active or queue.session_id != original_session_id:
                    return
                if queue.state == PlaybackState.IDLE:
                    idle_detected = True
                    break
            if not idle_detected:
                return
            # player went idle, give it a brief moment to settle
            await asyncio.sleep(1)
            if queue.state != PlaybackState.IDLE or queue.session_id != original_session_id:
                return
            # check if new items were added to the queue after the flow stream ended
            if queue.current_index is not None and (
                next_item := self.get_next_item(queue_id, queue.current_index)
            ):
                next_index = self.index_by_id(queue_id, next_item.queue_item_id)
                if next_index is not None:
                    self.logger.info(
                        "Resuming playback after flow stream completed for %s",
                        queue.display_name,
                    )
                    await self.play_index(queue_id, next_index)

        task_id = f"queue_buffer_completed_{queue_id}"
        self.mass.create_task(_resume_on_idle(), task_id=task_id)

    def flow_stream_finished(self, queue_id: str) -> bool:
        """
        Return whether the flow stream for the current playback session is fully generated.

        Lets player providers detect flow EOF when the device does not report idle
        (e.g. a Cast group that underruns the LIVE flow stream and keeps reporting playing).

        :param queue_id: The queue ID.
        """
        queue = self.get(queue_id)
        if queue is None or queue.session_id is None:
            return False
        return self._queue_data[queue_id].flow_buffer_completed == queue.session_id

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
        """
        Load new items at index.

        - queue_id: id of the queue to process this request.
        - queue_items: a list of QueueItems
        - insert_at_index: insert the item(s) at this index
        - keep_remaining: keep the remaining items after the insert
        - shuffle: (re)shuffle the items after insert index
        """
        prev_items = self._queue_data[queue_id].items[:insert_at_index] if keep_played else []
        next_items = queue_items

        # if keep_remaining, append the old 'next' items
        if keep_remaining:
            next_items += self._queue_data[queue_id].items[insert_at_index:]

        # we set the original insert order as attribute so we can un-shuffle
        for index, item in enumerate(next_items):
            item.sort_index += insert_at_index + index
        # (re)shuffle the final batch if needed: smart shuffle when enabled, else pure random
        if shuffle:
            queue = self._queue_data[queue_id].queue
            if self._smart_shuffle.is_enabled(queue_id):
                next_items = await self._smart_shuffle.arrange(queue, next_items)
            else:
                next_items = random.sample(next_items, len(next_items))
        self.update_items(queue_id, prev_items + next_items)

    def update_items(self, queue_id: str, queue_items: list[QueueItem]) -> None:
        """Update the existing queue items, mostly caused by reordering."""
        self._queue_data[queue_id].items = queue_items
        queue = self._queue_data[queue_id].queue
        queue.items = len(self._queue_data[queue_id].items)
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
                self._stream_feeder._enqueue_next_item(queue_id, next_item)

    # Helper methods

    def get_item(self, queue_id: str, item_id_or_index: int | str | None) -> QueueItem | None:
        """Get queue item by index or item_id."""
        if item_id_or_index is None:
            return None
        if (data := self._queue_data.get(queue_id)) is None:
            return None
        queue_items = data.items
        if isinstance(item_id_or_index, int) and len(queue_items) > item_id_or_index:
            return queue_items[item_id_or_index]
        if isinstance(item_id_or_index, str):
            return next((x for x in queue_items if x.queue_item_id == item_id_or_index), None)
        return None

    def signal_update(self, queue_id: str, items_changed: bool = False) -> None:
        """Signal state changed of given queue."""
        if (data := self._queue_data.get(queue_id)) is None:
            return
        queue = data.queue
        # set 'active_playlist' in extra attributes as a human readable list
        # of the enqueued media items for API clients to display if they want to
        queue.extra_attributes[ATTR_ACTIVE_PLAYLIST] = " / ".join(
            [x.name for x in queue.enqueued_media_items]
        )
        if items_changed:
            self.mass.signal_event(EventType.QUEUE_ITEMS_UPDATED, object_id=queue_id, data=queue)
            # save items in cache - only cache items with valid media_item
            self.mass.create_task(
                self.mass.cache.set(
                    key=queue_id,
                    data=data.items_to_cache(),
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
                data=data.to_cache(),
                provider=self.domain,
                category=CACHE_CATEGORY_PLAYER_QUEUE_STATE,
            )
        )

    def index_by_id(self, queue_id: str, queue_item_id: str) -> int | None:
        """Get index by queue_item_id."""
        if (data := self._queue_data.get(queue_id)) is None:
            return None
        for index, item in enumerate(data.items):
            if item.queue_item_id == queue_item_id:
                return index
        return None

    async def get_tracks_for_playback(self, media_item: MediaItemType) -> list[Track]:
        """
        Return the playable tracks a media item resolves to, honoring the user's selection prefs.

        :param media_item: The media item to resolve to playable tracks.
        """
        return await self._media_resolver.get_tracks_for_playback(media_item)

    async def player_media_from_queue_item(self, queue_item: QueueItem) -> PlayerMedia:
        """
        Parse PlayerMedia from QueueItem.

        :param queue_item: The queue item to create media from.
        """
        queue = self._queue_data[queue_item.queue_id].queue
        if queue_item.streamdetails:
            # prefer netto duration
            # when seeking, the player only receives the remaining duration
            duration = queue_item.streamdetails.duration or queue_item.duration
            if duration and queue_item.streamdetails.seek_position:
                duration = int(duration - queue_item.streamdetails.seek_position)
        else:
            duration = queue_item.duration
        if queue.session_id is None:
            raise InvalidDataError("Queue session_id is None")
        media = PlayerMedia(
            uri=queue_item.uri,
            media_type=queue_item.media_type,
            title=queue_item.name,
            image_url=MASS_LOGO_ONLINE,
            duration=duration,
            source_id=queue_item.queue_id,
            queue_item_id=queue_item.queue_item_id,
            custom_data={
                "session_id": queue.session_id,
                "original_uri": queue_item.uri,
            },
        )
        if queue_item.media_item:
            media.title = queue_item.media_item.name
            media.artist = getattr(queue_item.media_item, "artist_str", "")
            media.album = (
                album.name if (album := getattr(queue_item.media_item, "album", None)) else ""
            )
            if queue_item.image:
                # the image format needs to be 512x512 jpeg for maximum compatibility with players
                # we prefer the imageproxy on the streamserver here because this request is sent
                # to the player itself which may not be able to reach the regular webserver
                media.image_url = self.mass.metadata.get_image_url(
                    queue_item.image, size=512, image_format="jpeg", prefer_stream_server=True
                )
        return media

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
            if (next_index := self._queue_loader._get_next_index(queue_id, index + skip)) is None:
                break
            next_item = self.get_item(queue_id, next_index)
            if next_item is None:
                continue
            if not next_item.available:
                # ensure that we skip unavailable items (set by load_next track logic)
                continue
            return next_item
        return None

    def _check_player_permission(self, queue_id: str) -> None:
        """
        Check if the current user has permission to control this player/queue.

        :param queue_id: The queue/player ID to check access for.
        :raises InsufficientPermissions: If the user lacks access.
        """
        current_user = get_current_user()
        if (
            current_user
            and current_user.player_filter
            and queue_id not in current_user.player_filter
        ):
            msg = f"{current_user.username} does not have access to player {queue_id}"
            raise InsufficientPermissions(msg)

    @handle_play_action
    async def _handle_play_media(
        self,
        queue_id: str,
        media: MediaItemType | ItemMapping | str | list[MediaItemType | ItemMapping | str],
        option: QueueOption | None = None,
        radio_mode: bool = False,
        start_item: PlayableMediaItemType | str | None = None,
        sort_by: str | None = None,
    ) -> None:
        """Handle play media without acquiring the queue lock."""
        # cancel any pending play_index calls for this queue to prevent conflicts
        self.mass.cancel_timer(f"queue_play_index_{queue_id}")
        self._set_transitioning(queue_id, False)
        # ruff: noqa: PLR0915
        # we use a contextvar to bypass the throttler for this asyncio task/context
        # this makes sure that playback has priority over other requests that may be
        # happening in the background
        BYPASS_THROTTLER.set(True)
        if not (queue := self.get(queue_id)):
            raise PlayerUnavailableError(f"Queue {queue_id} is not available")
        # always fetch the underlying player so we can raise early if its not available
        queue_player = self.mass.players.get_player(queue_id, True)
        assert queue_player is not None  # for type checking
        if queue_player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS):
            self.logger.warning("Ignore queue command: An announcement is in progress")
            return

        # save the user requesting the playback (clear it for anonymous playback)
        playback_user = get_current_user()
        queue.userid = playback_user.user_id if playback_user else None
        if playback_user:
            self.logger.debug(
                "User %s requested playback.", playback_user.display_name or playback_user.username
            )

        # a single item or list of items may be provided
        media_list = media if isinstance(media, list) else [media]

        if radio_mode:
            # radio_mode is deprecated: a "radio" is now a dynamic radio playlist. Translate each
            # seed into the radio_playlist provider's URI and enqueue those (resolved to dynamic
            # playlists that self-manage their refills).
            self.logger.warning(
                "radio_mode is deprecated; enqueue a radio_playlist:// dynamic playlist instead"
            )
            media_list = [
                seed_uri
                if (seed_uri := item if isinstance(item, str) else str(item.uri)).startswith(
                    "radio_playlist://"
                )
                else f"radio_playlist://playlist/{seed_uri}"
                for item in media_list
            ]
            radio_mode = False

        # clear queue if needed
        if option == QueueOption.REPLACE:
            self.clear(queue_id, skip_stop=True)
        # Clear the 'enqueued media item' list when a new queue is requested
        if option not in (QueueOption.ADD, QueueOption.NEXT):
            queue.enqueued_media_items.clear()

        # An ADD/NEXT onto a queue that already has dynamic sources keeps feeding its bounded pool;
        # any other enqueue is not managed here (a REPLACE clears the sources above, and a dynamic
        # playlist self-manages its own refills).
        already_managed = bool(queue.sources) and option in (QueueOption.ADD, QueueOption.NEXT)
        managed_pool = already_managed

        media_items: list[MediaItemType] = []
        source_items: list[MediaItemType] = []
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

                if (
                    isinstance(media_item, ItemMapping)
                    and media_item.media_type == MediaType.PLAYLIST
                ):
                    # Resolve ItemMapping for a playlist so the full Playlist object
                    # so we have access to details such as 'is_dynamic'
                    with suppress(MusicAssistantError):
                        media_item = await self.mass.music.playlists.get(
                            media_item.item_id,
                            media_item.provider,
                        )

                # Save requested media item to play on the queue so we can use it as a source
                # for Autoplay. Use FIFO list to keep track of the last 10 played items
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
                    if isinstance(media_item, Playlist) and media_item.is_dynamic:
                        # a dynamic playlist/station is always a self-managing dynamic source
                        source_items.append(media_item)

                # handle default enqueue option if needed
                if option is None:
                    # Radio + AudioSource share a single "live_sources" enqueue default —
                    # both are live infinite streams where REPLACE is almost always the
                    # right semantic. Other media types use their per-type config key.
                    if media_item.media_type in (MediaType.RADIO, MediaType.AUDIO_SOURCE):
                        config_key = CONF_DEFAULT_ENQUEUE_OPTION_LIVE_SOURCES
                    else:
                        config_key = f"default_enqueue_option_{media_item.media_type.value}"
                    config_value = await self.mass.config.get_core_config_value(
                        self.domain,
                        config_key,
                        return_type=str,
                    )
                    option = QueueOption(config_value)
                    if option == QueueOption.REPLACE:
                        self.clear(queue_id, skip_stop=True)

                # collect media_items to play
                if isinstance(media_item, Playlist) and media_item.is_dynamic:
                    # Dynamic playlists/stations supply their own tracks on demand and keep their
                    # self-managing refill path even inside a managed pool; fetch first batch now.
                    self.mass.create_task(
                        self.mass.music.mark_item_played(
                            media_item, userid=queue.userid, queue_id=queue_id, user_initiated=True
                        )
                    )
                    initial_tracks = await self._media_resolver.get_playlist_tracks(
                        media_item, start_item=None
                    )
                    media_items += initial_tracks
                elif managed_pool:
                    # Managed-pool mode: keep the item as a dynamic source; the bounded pool is
                    # built from these sources below (a finite source rotates its own tracks).
                    if not isinstance(media_item, (ItemMapping, BrowseFolder)):
                        source_items.append(media_item)
                else:
                    # Convert start_item to string URI if needed
                    start_item_uri: str | None = None
                    if isinstance(start_item, str):
                        start_item_uri = start_item
                    elif start_item is not None:
                        start_item_uri = start_item.uri
                    media_items += await self._media_resolver._resolve_media_items(
                        media_item,
                        start_item_uri,
                        userid=queue.userid,
                        queue_id=queue_id,
                        sort_by=sort_by,
                    )

            except MusicAssistantError as err:
                # invalid MA uri or item not found error
                self.logger.warning("Skipping %s: %s", item, str(err))

        # overwrite or append the queue's source items
        replace_sources = option not in (QueueOption.ADD, QueueOption.NEXT)
        if replace_sources:
            self._store_sources(queue, source_items)
        else:
            self._store_sources(queue, self._queue_data[queue_id].source_items + source_items)
        source_items = self._queue_data[queue_id].source_items
        queue.is_dynamic = has_dynamic_source(source_items)

        if already_managed and not media_items:
            # new sources were added to an already-active pool: leave the playing pool intact and
            # let the next top-up incorporate them
            self.signal_update(queue_id)
            if queue.current_index is not None and (queue.items - queue.current_index) < 5:
                task_id = f"fill_dynamic_tracks_{queue_id}"
                self.mass.call_later(
                    5, self._queue_loader._fill_dynamic_tracks, queue_id, task_id=task_id
                )
            return

        # only add valid/available items
        queue_items: list[QueueItem] = [
            QueueItem.from_media_item(queue_id, cast("PlayableMediaItemType", x))
            for x in media_items
            if x and x.available
        ]

        if not queue_items:
            raise MediaNotFoundError("No playable items found")

        # load the items into the queue (managed-pool tracks are already ordered, so don't reshuffle)
        await self._queue_loader._enqueue_with_option(queue_id, queue_items, option, managed_pool)

    @handle_play_action
    async def _handle_play(self, queue_id: str) -> None:
        """Handle play without acquiring the queue lock."""
        queue_player = self.mass.players.get_player(queue_id, True)
        if queue_player is None:
            raise PlayerUnavailableError(f"Player {queue_id} is not available")
        if (queue := self.get(queue_id)) and queue.active and queue.state == PlaybackState.PAUSED:
            # forward the actual play/unpause command to the player
            await queue_player.play()
            return
        # player is not paused, perform resume instead
        await self.resume(queue_id)

    def _set_transitioning(self, queue_id: str, value: bool) -> None:
        """Mark (or clear) whether a queue is mid-transition (no-op if it is not registered)."""
        if (data := self._queue_data.get(queue_id)) is not None:
            data.transitioning = value

    def _store_sources(self, queue: PlayerQueue, items: list[MediaItemType]) -> None:
        """
        Hold the queue's full dynamic-source items server-side and project them onto `sources`.

        :param queue: The queue whose sources are being set.
        :param items: The full source media items; an empty list clears the queue's sources.
        """
        self._queue_data[queue.queue_id].source_items = items
        queue.sources = [ItemMapping.from_item(item) for item in items]
        # release any materialized finite-source state whose source is no longer present
        self._managed_pool.retain(
            queue.queue_id, {item.uri for item in items if item.uri is not None}
        )

    async def _get_similar_tracks(
        self,
        queue_id: str,
        is_initial: bool = False,
        seed_items: list[MediaItemType] | None = None,
    ) -> list[Track]:
        """
        Fetch tracks similar to the given seeds (autoplay's similar/continuation mode).

        :param queue_id: The queue to fetch tracks for.
        :param is_initial: True to interleave the base/seed tracks into the result, False to
            return only similar tracks.
        :param seed_items: Explicit seed items to base the tracks on. Defaults to the queue's
            sources; autoplay passes the enqueued media items instead.
        """
        queue = self._queue_data[queue_id].queue
        queue_track_items: list[Track] = [
            q.media_item
            for q in self._queue_data[queue_id].items
            if q.media_item and isinstance(q.media_item, Track)
        ]
        source_items = (
            seed_items if seed_items is not None else self._queue_data[queue_id].source_items
        )
        if not source_items:
            # this may happen during race conditions as this method is called delayed
            return []
        self.logger.info(
            "Fetching similar tracks for queue %s based on: %s",
            queue.display_name,
            ", ".join([x.name for x in source_items]),
        )

        # Get user's preferred provider instances for steering provider selection
        preferred_provider_instances: list[str] | None = None
        if (
            queue.userid
            and (playback_user := await self.mass.webserver.auth.get_user(queue.userid))
            and playback_user.provider_filter
        ):
            preferred_provider_instances = playback_user.provider_filter

        # Some providers have very deterministic similar-track algorithms for a single track
        # seed. When continuing from a single track on a refill, seed from the play history
        # instead so the result keeps varying.
        if (
            len(source_items) == 1
            and source_items[0].media_type == MediaType.TRACK
            and not is_initial
            and queue_track_items
        ):
            # Helper samples 5 internally; bound the input.
            seeds: list[MediaItemType] = random.sample(
                queue_track_items, min(len(queue_track_items), 10)
            )
        else:
            seeds = list(source_items)

        radio_prov = self.mass.get_provider("radio_playlist")
        if radio_prov is None:
            return []
        dynamic_tracks = await cast("RadioPlaylistProvider", radio_prov).get_dynamic_tracks(
            seeds,
            include_base_tracks=is_initial,
            target_size=25,
            preferred_provider_instances=preferred_provider_instances,
        )
        # Drop anything already queued/played
        queued_set = set(queue_track_items)
        return [track for track in dynamic_tracks if track not in queued_set]
