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
from typing import TYPE_CHECKING, Any, Final, cast

import shortuuid
from music_assistant_models.auth import Scope
from music_assistant_models.enums import (
    EventType,
    MediaType,
    PlaybackState,
    PlayerType,
    QueueOption,
    RepeatMode,
)
from music_assistant_models.errors import (
    AudioError,
    InsufficientPermissions,
    InvalidCommand,
    InvalidDataError,
    MediaNotFoundError,
    PlayerUnavailableError,
    QueueEmpty,
)
from music_assistant_models.media_items import (
    Audiobook,
    ItemMapping,
    MediaItemType,
    PlayableMediaItemType,
    Playlist,
    PodcastEpisode,
    SoundEffect,
    Track,
)
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.constants import (
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    MASS_LOGO_ONLINE,
    PLAYLIST_MEDIA_TYPES,
)
from music_assistant.controllers.player_queues.autoplay import Autoplay
from music_assistant.controllers.player_queues.config import (
    core_config_entries,
    queue_config_entries,
)
from music_assistant.controllers.player_queues.constants import (
    CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
    CACHE_CATEGORY_PLAYER_QUEUE_STATE,
    PLAYBACK_START_TIMEOUT,
    QUEUE_CACHE_SAVE_DELAY,
    SHUFFLE_INTENT_WINDOW,
)
from music_assistant.controllers.player_queues.helpers import (
    get_current_playback_speed,
    handle_play_action,
    is_dynamic_source,
)
from music_assistant.controllers.player_queues.managed_pool import ManagedPool
from music_assistant.controllers.player_queues.media_resolver import MediaResolver
from music_assistant.controllers.player_queues.playback_tracker import PlaybackTrackerMixin
from music_assistant.controllers.player_queues.queue_loader import QueueLoaderMixin
from music_assistant.controllers.player_queues.smart_shuffle import SmartShuffle
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.player_queues.stream_feeder import StreamFeederMixin
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.api import api_command
from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant_models import BackgroundTask
    from music_assistant_models.config_entries import (
        ConfigEntry,
        ConfigValueOption,
        CoreConfig,
    )
    from music_assistant_models.queue_item import QueueItem

    from music_assistant import MusicAssistant
    from music_assistant.constants import PlaylistPlayableItem
    from music_assistant.controllers.music.recency import RecencyWindows
    from music_assistant.helpers.json import SerializableType
    from music_assistant.models.player import Player


# the container media types worth surfacing as a queue "source" for clients to display. Individual
# items (single tracks, radio streams, podcast episodes, live audio sources, ...) carry no grouping
# and only clutter the "playing from" representation, so they are omitted from the wire `sources`.
_WIRE_SOURCE_MEDIA_TYPES: Final = frozenset(
    {
        MediaType.ARTIST,
        MediaType.ALBUM,
        MediaType.PLAYLIST,
        MediaType.PODCAST,
        MediaType.AUDIOBOOK,
    }
)


class PlayerQueuesController(QueueLoaderMixin, PlaybackTrackerMixin, StreamFeederMixin):
    """
    Controller holding all logic to enqueue music for players.

    The loading, playback-tracking and stream-feeding logic lives in mixins (over the shared base);
    this class owns the public API surface, the per-queue records and the stateful helper services.
    """

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize core controller."""
        super().__init__(mass)
        # server-side per-queue records, keyed by queue_id; each bundles the wire PlayerQueue with
        # its items, dynamic-source items and runtime-only state (see PlayerQueueData)
        self._queue_data: dict[str, PlayerQueueData] = {}
        # stateful helper services (own per-queue state + lifecycle), constructed with self
        self._autoplay = Autoplay(self)
        self._smart_shuffle = SmartShuffle(self)
        self._managed_pool = ManagedPool(self)
        self._media_resolver = MediaResolver(self)
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
        # flush any pending (debounced) state writes so the latest queue survives shutdown/update
        for queue in self.all():
            self.mass.cancel_timer(f"save_queue_cache_{queue.queue_id}")
            await self._save_queue_to_cache(queue.queue_id)

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this controller to include in diagnostics reports."""
        queues = [queue_data.queue for queue_data in self._queue_data.values()]
        by_state: dict[str, int] = {}
        for queue in queues:
            by_state[queue.state.value] = by_state.get(queue.state.value, 0) + 1
        return {
            "total": len(queues),
            "active": sum(queue.active for queue in queues),
            "by_state": by_state,
            "flow_mode_active": sum(queue.flow_mode for queue in queues),
            "dynamic_mode_active": sum(queue.is_dynamic for queue in queues),
            "total_items": sum(queue.items for queue in queues),
        }

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the core-module (global) config entries: the queue-controller defaults."""
        # kept cheap (no library lookup): the config controller populates the global autoplay
        # playlist dropdown for the UI, so this stays fast on the config value/parse path
        return core_config_entries(self.mass)

    async def update_config(self, config: CoreConfig, changed_keys: set[str]) -> None:
        """Apply a global queue-settings change: refresh derived per-queue state and notify clients."""
        await super().update_config(config, changed_keys)
        if not any(key.startswith("values/") for key in changed_keys):
            return
        # queues that follow a changed global value may flip their derived indicators, so refresh
        # and signal them (mirrors what save_player_queue_config does for a single queue)
        for queue in self.all():
            queue.smart_fades_active = self.mass.streams.is_smart_fades_active(queue)
            queue.smart_shuffle_active = self.is_smart_shuffle_active(queue)
            self.signal_update(queue.queue_id)

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
        return queue_config_entries(self.mass, playlist_options)

    def __iter__(self) -> Iterator[PlayerQueue]:
        """Iterate over (available) players."""
        return iter(queue_data.queue for queue_data in self._queue_data.values())

    @api_command("player_queues/all", required_scope=Scope.QUEUES_READ)
    def all(self) -> tuple[PlayerQueue, ...]:
        """Return all registered PlayerQueues."""
        return tuple(queue_data.queue for queue_data in self._queue_data.values())

    @api_command("player_queues/get", required_scope=Scope.QUEUES_READ)
    def get(self, queue_id: str) -> PlayerQueue | None:
        """Return PlayerQueue by queue_id or None if not found."""
        queue_data = self._queue_data.get(queue_id)
        return queue_data.queue if queue_data else None

    def queue_data(self, queue_id: str) -> PlayerQueueData:
        """
        Return the server-side record for a queue (raises if the queue is unknown).

        Internal accessor for the stateful helper services so they reach per-queue state through
        the controller rather than its private store.
        """
        return self._queue_data[queue_id]

    def queue_data_or_none(self, queue_id: str) -> PlayerQueueData | None:
        """Return the server-side record for a queue, or None if it is not registered."""
        return self._queue_data.get(queue_id)

    @api_command("player_queues/items", required_scope=Scope.QUEUES_READ)
    def items(self, queue_id: str, limit: int = 500, offset: int = 0) -> list[QueueItem]:
        """Return all QueueItems for given PlayerQueue."""
        if (queue_data := self._queue_data.get(queue_id)) is None:
            return []
        return queue_data.items[offset : offset + limit]

    @api_command("player_queues/get_active_queue", required_scope=Scope.QUEUES_READ)
    def get_active_queue(self, player_id: str) -> PlayerQueue | None:
        """Return the current active/synced queue for a player."""
        if player := self.mass.players.get_player(player_id):
            return self.mass.players.get_active_queue(player)
        return None

    # Queue commands

    @api_command("player_queues/shuffle", required_scope=Scope.QUEUES_CONTROL)
    async def set_shuffle(self, queue_id: str, shuffle_enabled: bool) -> None:
        """Configure shuffle setting on the the queue."""
        queue = self._queue_data[queue_id].queue
        if queue.is_dynamic:
            # a dynamic queue is an always-on, recency-orchestrated smart mix; manual shuffle
            # (and plain linear order) have no meaning here so the toggle is locked
            raise InvalidCommand("Cannot change shuffle while the queue is in dynamic mode")
        if queue.shuffle_enabled == shuffle_enabled:
            return  # no change
        queue.shuffle_enabled = shuffle_enabled
        # remember the moment the user asked for shuffle, so media started right after this
        # keeps it instead of being reset by the "fresh queue plays in order" default. Monotonic
        # because only the elapsed interval matters, and a host whose clock is corrected while
        # MA runs (a Pi without an RTC syncing after boot) would otherwise age it wrongly.
        self._queue_data[queue_id].shuffle_set_at = time.monotonic() if shuffle_enabled else None
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

        A dynamic queue is always an orchestrated smart mix (the managed pool), so it always counts
        as active; otherwise smart shuffle is active when shuffle is on and the per-queue
        smart-shuffle setting is enabled.

        :param queue: The queue to evaluate.
        """
        if queue.is_dynamic:
            return True
        return queue.shuffle_enabled and self._smart_shuffle.is_enabled(queue.queue_id)

    @api_command("player_queues/autoplay", required_scope=Scope.QUEUES_CONTROL)
    def set_autoplay(self, queue_id: str, autoplay_enabled: bool) -> None:
        """Configure Autoplay setting on the queue."""
        queue_data = self._queue_data[queue_id]
        queue = queue_data.queue
        queue.autoplay_enabled = autoplay_enabled
        # if we're already at/near the end of the queue, kick off a refill right away
        # (an active dynamic source manages its own refills, so leave it be)
        if (
            queue.autoplay_enabled
            and not queue.is_dynamic
            and queue.current_index is not None
            and (queue.items - queue.current_index) < 5
        ):
            task_id = f"fill_autoplay_tracks_{queue_id}"
            self.mass.call_later(5, self._fill_autoplay_tracks, queue_id, task_id=task_id)
        self.signal_update(queue_id=queue_id)

    @api_command(
        "player_queues/dont_stop_the_music", required_scope=Scope.QUEUES_CONTROL, alias=True
    )
    def set_dont_stop_the_music(self, queue_id: str, dont_stop_the_music_enabled: bool) -> None:
        """Backwards-compatible alias for the autoplay command, used by older clients."""
        self.set_autoplay(queue_id, dont_stop_the_music_enabled)

    @api_command("player_queues/repeat", required_scope=Scope.QUEUES_CONTROL)
    def set_repeat(self, queue_id: str, repeat_mode: RepeatMode) -> None:
        """Configure repeat setting on the the queue."""
        queue = self._queue_data[queue_id].queue
        if queue.is_dynamic:
            # a dynamic queue is an always-on flowing mix of its sources; repeat has no meaning here
            raise InvalidCommand("Cannot change repeat while the queue is in dynamic mode")
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

    @api_command("player_queues/crossfade", required_scope=Scope.QUEUES_CONTROL)
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
                self._enqueue_next_item(queue_id, next_item)

    @api_command("player_queues/overlay", required_scope=Scope.QUEUES_CONTROL)
    async def set_overlay(
        self,
        queue_id: str,
        enabled: bool | None = None,
        source: str | None = None,
        volume: int | None = None,
    ) -> None:
        """
        Configure the audio overlay for the given queue.

        The audio overlay mixes a looping sound effect (e.g. rain or white noise)
        into the queue's audio stream. Changes take effect immediately: if the
        queue is playing, playback is restarted from the current position.

        :param queue_id: queue_id of the queue to configure.
        :param enabled: Enable or disable the audio overlay. Omit to leave unchanged.
        :param source: URI of the sound effect item to mix in. Omit to leave unchanged.
        :param volume: Overlay loudness relative to the music in percent
            (0-200, 100 = equally loud). Omit to leave unchanged.
        """
        queue = self._queue_data[queue_id].queue
        changed = audible_change = False
        if source is not None:
            item = await self.mass.music.get_item_by_uri(source)
            if item.media_type != MediaType.SOUND_EFFECT:
                raise InvalidDataError("Audio overlay source must be a sound effect item")
            mapping = ItemMapping.from_item(cast("SoundEffect", item))
            if queue.overlay_source != mapping:
                queue.overlay_source = mapping
                changed = True
                audible_change = queue.overlay_enabled
        if volume is not None:
            if not (0 <= volume <= 200):
                raise InvalidDataError(f"Overlay volume must be between 0 and 200, got {volume}")
            if queue.overlay_volume != volume:
                queue.overlay_volume = volume
                changed = True
                audible_change |= queue.overlay_enabled
        if enabled is not None and queue.overlay_enabled != enabled:
            if enabled and queue.overlay_source is None:
                raise InvalidCommand("Can not enable audio overlay: no overlay source selected")
            queue.overlay_enabled = enabled
            changed = audible_change = True
        if not changed:
            return
        self.signal_update(queue_id)
        if audible_change and queue.state == PlaybackState.PLAYING:
            # restart playback from the current position so the change is heard
            # immediately instead of after the player's audio buffer drains
            await self.resume(queue_id)

    # Two timebases are used in this controller when variable playback speed is in
    # effect (atempo applied server-side):
    #   "stream-time"  — seconds of audio the player has played (post-atempo).
    #   "media-time"   — seconds of the original content the listener has heard.
    #                    What the user expects to see on the progress bar and what
    #                    we use for resume positions.
    # Conversion: media-time = stream-time x playback_speed.
    @api_command("player_queues/set_playback_speed", required_scope=Scope.QUEUES_CONTROL)
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

    @api_command(
        "player_queues/play_media", required_scope=Scope.QUEUES_CONTROL, allow_impersonation=True
    )
    async def play_media(
        self,
        queue_id: str,
        media: MediaItemType | ItemMapping | str | list[MediaItemType | ItemMapping | str],
        option: QueueOption | None = None,
        radio_mode: bool = False,
        start_item: PlayableMediaItemType | str | None = None,
        sort_by: str | None = None,
        start_from_beginning: bool = False,
        shuffle: bool | None = None,
    ) -> None:
        """
        Play media item(s) on the given queue.

        :param queue_id: The queue_id of the queue to play media on.
        :param media: Media that should be played (MediaItem(s) and/or uri's).
        :param option: Which enqueue mode to use.
        :param radio_mode: Deprecated — translated to a radio_playlist:// dynamic playlist;
            prefer enqueuing that URI directly.
        :param start_item: Optional item to start the playlist or album from.
        :param sort_by: Optional sort key to order tracks before applying start_item.
        :param start_from_beginning: Start a podcast episode at position 0, ignoring any
            saved resume position. The stored progress itself is left untouched.
        :param shuffle: Play the media shuffled (or explicitly in order). Only applies to the
            options that start playing right away (play/replace), and never to a dynamic source
            (an always-on smart mix). Omit to let the queue decide: it keeps shuffle when the user
            just switched it on, and plays the media in order otherwise.
        """
        self._check_player_permission(queue_id)
        if not self.get(queue_id):
            raise PlayerUnavailableError(f"Queue {queue_id} is not available")
        # Lock is acquired by the @handle_play_action decorator on the internal handler
        await self._handle_play_media(
            queue_id, media, option, radio_mode, start_item, sort_by, start_from_beginning, shuffle
        )

    @api_command("player_queues/move_item", required_scope=Scope.QUEUES_CONTROL)
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

    @api_command("player_queues/move_item_end", required_scope=Scope.QUEUES_CONTROL)
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

    @api_command("player_queues/delete_item", required_scope=Scope.QUEUES_CONTROL)
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

    @api_command("player_queues/clear", required_scope=Scope.QUEUES_CONTROL)
    def clear(self, queue_id: str, skip_stop: bool = False) -> None:
        """Clear all items in the queue, switching shuffle off with them."""
        self._clear(queue_id, skip_stop)
        # clearing is an explicit "start over" gesture by the user, so a shuffle that belonged to
        # the discarded content must not carry over into whatever is played next
        self._reset_shuffle(queue_id)

    def mark_ended(self, queue_id: str) -> None:
        """
        Mark a queue as played to its end, keeping its items so it can be replayed.

        The playback position is parked on the last item rather than cleared: a null index is
        indistinguishable from a queue that was loaded but never started, and an index past the
        end is silently misread by everything that does arithmetic on it. `ended` is what tells
        clients the queue finished, and pressing play starts it over from the first item.

        :param queue_id: The queue_id of the queue that reached its end.
        """
        queue_data = self._queue_data[queue_id]
        queue = queue_data.queue
        if not queue_data.items:
            # nothing to replay, so there is nothing to advertise as finished either
            self._clear(queue_id)
            return
        self.mass.streams.audio_processing.clear(queue_id)
        queue.ended = True
        queue.current_index = len(queue_data.items) - 1
        queue.current_item = queue_data.items[-1]
        queue.next_item = None
        queue.elapsed_time = 0
        queue.elapsed_time_last_updated = time.time()
        queue.index_in_buffer = None
        queue.resume_pos = 0
        self.mass.create_task(self._cleanup_queue_audio_data(queue_id))
        self.signal_update(queue_id)

    @api_command("player_queues/save_as_playlist", required_scope=Scope.LIBRARY_WRITE)
    async def save_as_playlist(self, queue_id: str, name: str) -> BackgroundTask:
        """
        Save the current queue items as a new playlist.

        :param queue_id: The queue_id of the queue to save.
        :param name: The name for the new playlist.
        """
        if not self.get(queue_id):
            raise PlayerUnavailableError(f"Queue {queue_id} is not available")
        queue_items = queue_data.items if (queue_data := self._queue_data.get(queue_id)) else []
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

    @api_command("player_queues/stop", required_scope=Scope.QUEUES_CONTROL)
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
        queue_data = self._queue_data[queue_id]
        session_id = queue_data.session_id
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
        if queue_data.session_id == session_id:
            queue_data.session_id = None
        self.mass.streams.audio_processing.clear(queue_id, session_id)
        self.mass.create_task(self._cleanup_queue_audio_data(queue_id))

    @api_command("player_queues/play", required_scope=Scope.QUEUES_CONTROL)
    async def play(self, queue_id: str) -> None:
        """
        Handle PLAY command for given queue.

        :param queue_id: queue_id of the playerqueue to handle the command.
        """
        self._check_player_permission(queue_id)
        if not self.get(queue_id):
            raise PlayerUnavailableError(f"Queue {queue_id} is not available")
        await self._handle_play(queue_id)

    @api_command("player_queues/pause", required_scope=Scope.QUEUES_CONTROL)
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

    @api_command("player_queues/play_pause", required_scope=Scope.QUEUES_CONTROL)
    async def play_pause(self, queue_id: str) -> None:
        """
        Toggle play/pause on given playerqueue.

        - queue_id: queue_id of the queue to handle the command.
        """
        if (queue := self.get(queue_id)) and queue.state == PlaybackState.PLAYING:
            await self.pause(queue_id)
            return
        await self.play(queue_id)

    @api_command("player_queues/next", required_scope=Scope.QUEUES_CONTROL)
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
        next_index = self._get_next_index(queue_id, idx, True)
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

    @api_command("player_queues/previous", required_scope=Scope.QUEUES_CONTROL)
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

    @api_command("player_queues/skip", required_scope=Scope.QUEUES_CONTROL)
    async def skip(self, queue_id: str, seconds: int = 10) -> None:
        """
        Handle SKIP command for given queue.

        - queue_id: queue_id of the queue to handle the command.
        - seconds: number of seconds to skip in track. Use negative value to skip back.
        """
        if (queue := self.get(queue_id)) is None or not queue.active:
            raise InvalidCommand(f"Queue {queue_id} is not active")
        await self.seek(queue_id, int(self._queue_data[queue_id].queue.elapsed_time + seconds))

    @api_command("player_queues/seek", required_scope=Scope.QUEUES_CONTROL)
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
        # Publish the seek target before rebuilding the stream to prevent progress snapback.
        queue.elapsed_time = position
        queue.elapsed_time_last_updated = time.time()
        self.signal_update(queue_id)
        await self.play_index(queue_id, queue.current_index, seek_position=position)

    @api_command("player_queues/resume", required_scope=Scope.QUEUES_CONTROL)
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

        if queue.ended and len(queue_items) > 0:
            # the queue played to its end and is parked on its last item,
            # so pressing play starts it over from the beginning
            resume_item = queue_items[0]
            resume_pos = 0
        elif not resume_item and queue.current_index is not None and len(queue_items) > 0:
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
            msg = f"Resume queue requested but queue {queue.display_name} is empty"
            raise QueueEmpty(msg)

    @api_command("player_queues/play_index", required_scope=Scope.QUEUES_CONTROL)
    @handle_play_action
    async def play_index(  # noqa: PLR0915
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
            queue_data = self._queue_data[queue_id]
            queue = queue_data.queue
            queue.resume_pos = 0
            # A queue picked up from its end plays its items over from the start, so a resume point
            # left on an audiobook/episode must not pull it back to where it was left off. The flag
            # itself is only cleared once an item actually loaded below, so a start that never got
            # off the ground leaves the queue finished instead of stranding it without a position.
            restarting_ended_queue = queue.ended
            if isinstance(index, str):
                temp_index = self.index_by_id(queue_id, index)
                if temp_index is None:
                    raise InvalidDataError(f"Item {index} not found in queue")
                index = temp_index
            # At this point index is guaranteed to be int
            queue.index_in_buffer = index
            queue_data.flow_mode_stream_log = []
            queue_data.flow_buffer_completed = None
            queue_data.flow_queue_exhausted = None
            target_player = self.mass.players.get_player(queue_id)
            if target_player is None:
                raise PlayerUnavailableError(f"Player {queue_id} is not available")
            queue_data.next_item_id_enqueued = None
            # always update session id when we start a new playback session
            queue_data.session_id = shortuuid.random(length=8)
            self.mass.streams.audio_processing.start_session(
                queue_id,
                queue_data.session_id,
            )
            # handle resume point of audiobook(chapter) or podcast(episode)
            if (
                not seek_position
                and not restarting_ended_queue
                and (queue_item := self.get_item(queue_id, index))
                and (resume_position_ms := getattr(queue_item.media_item, "resume_position_ms", 0))
            ):
                # the client may have fetched the item before its duration was known
                await self._restore_probed_duration(queue_item)
                if queue_item.duration or getattr(queue_item.media_item, "duration", 0):
                    seek_position = max(0, int((resume_position_ms - 500) / 1000))
                else:
                    # seeking needs a duration, which is determined while streaming
                    self.logger.debug(
                        "Can not resume %s at %ss: its duration is not known (yet)",
                        queue_item.name,
                        int(resume_position_ms / 1000),
                    )

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
                    userid=queue_data.userid,
                )
                if stored_speed != 1.0:
                    queue_item.extra_attributes["playback_speed"] = stored_speed

            # try to load the item, retry with next item if it fails
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
                    # playback is under way, so the queue is no longer sitting at its end
                    queue.ended = False
                    # reset the elapsed clock together with the item switch (like
                    # next/previous do), so queue updates signaled before the player
                    # reports position don't carry the previous item's elapsed_time
                    queue.elapsed_time = seek_position if attempt == 0 else 0
                    queue.elapsed_time_last_updated = time.time()
                    break
                except (MediaNotFoundError, AudioError) as err:
                    item_name = queue_item.name if queue_item else "unknown"
                    # Only MediaNotFoundError (item unreachable) is persistent;
                    # keep AudioError items available so a retry can resurface
                    # the same actionable error.
                    if queue_item and isinstance(err, MediaNotFoundError):
                        queue_item.available = False
                    next_index = self._get_next_index(queue_id, index, allow_repeat=False)
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
            player_media = await self.player_media_from_queue_item(queue_item)
            # Hold the play action until the player confirms playback so the UI keeps
            # showing the command as in progress instead of falling back to a play button
            # for the time the player still needs to connect and start. The queue update
            # for the new item goes out first, so the item shows while it is starting.
            async with self.mass.players.wait_for_player_update(
                queue_id,
                attribute_name="playback_state",
                attribute_value=PlaybackState.PLAYING,
                timeout=PLAYBACK_START_TIMEOUT,
            ):
                await self.mass.players.play_media(queue_id, player_media)
                queue.current_index = index
                queue.current_item = queue_item
                self.signal_update(queue_id)
        finally:
            self._set_transitioning(queue_id, False)

    @api_command("player_queues/transfer", required_scope=Scope.QUEUES_CONTROL)
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
        # The shuffle intent moves with the flag it was recorded for, or the target would judge the
        # transferred shuffle against a stamp left over from its own previous content. It is good
        # for one play, so it is taken off the source rather than copied: the gesture followed the
        # queue to its new player and must not shuffle whatever is started here next.
        source_data = self._queue_data[source_queue_id]
        self._queue_data[target_queue_id].shuffle_set_at = source_data.shuffle_set_at
        source_data.shuffle_set_at = None
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
        self._queue_data[target_queue_id].enqueued_media_items = list(
            self._queue_data[source_queue_id].enqueued_media_items
        )
        target_queue.resume_pos = source_resume_pos
        target_queue.current_index = source_current_index
        if source_current_item:
            target_queue.current_item = source_current_item
            target_queue.current_item.queue_id = target_queue_id
        self._clear(source_queue_id, skip_stop=True)

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
        queue_data: PlayerQueueData | None = None
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
                queue_data = PlayerQueueData.from_cache(prev_state, prev_items)
            except Exception as err:
                self.logger.warning(
                    "Failed to restore the queue(items) for %s - %s",
                    player.state.name,
                    str(err),
                )
                # Reset to clean state on failure
                queue_data = None
        if queue_data is None:
            queue_data = PlayerQueueData(
                queue=PlayerQueue(
                    queue_id=queue_id,
                    active=False,
                    display_name=player.state.name,
                    available=player.state.available,
                    # Autoplay starts out on for a brand new queue; the player's own Autoplay
                    # switch owns it from here on (and is restored above for a queue we know)
                    autoplay_enabled=True,
                    items=0,
                )
            )

        self._queue_data[queue_id] = queue_data
        # always call update to calculate state etc
        self.on_player_update(player, {})
        self.mass.signal_event(EventType.QUEUE_ADDED, object_id=queue_id, data=queue_data.queue)

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
        self._update_queue_from_player(player)

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
            _, elapsed_time = self._get_flow_queue_stream_index(queue, player)
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
        self.mass.streams.audio_processing.clear(player_id)
        # cancel any pending play_index calls for this queue to prevent conflicts
        self.mass.cancel_timer(f"queue_play_index_{player_id}")
        # cancel a pending debounced cache write AND an already-started one, so neither can
        # recreate a deleted entry after the player is gone (the timer becomes a task once it fires)
        self.mass.cancel_timer(f"save_queue_cache_{player_id}")
        self.mass.cancel_task(f"save_queue_cache_{player_id}")
        self._set_transitioning(player_id, False)
        if permanent:
            self.purge_saved_queue(player_id)
        self._queue_data.pop(player_id, None)
        self._managed_pool.forget(player_id)

    def purge_saved_queue(self, queue_id: str) -> None:
        """Delete the persisted state and items of the given queue."""
        for category in (CACHE_CATEGORY_PLAYER_QUEUE_STATE, CACHE_CATEGORY_PLAYER_QUEUE_ITEMS):
            # a removal runs both the player teardown and the config cleanup, so keep the
            # delete to one task per category instead of one per caller
            self.mass.create_task(
                self.mass.cache.delete(
                    key=queue_id,
                    provider=self.domain,
                    category=category,
                ),
                task_id=f"purge_saved_queue_{queue_id}_{category}",
            )

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
        self._preload_next_item(queue_id, item_id)
        # clean up stale audio buffers for old queue items to prevent memory leaks
        if current_index is not None:
            self.mass.create_task(self._cleanup_stale_queue_buffers(queue_id, current_index))

    def queue_buffer_completed(self, queue_id: str, queue_exhausted: bool) -> None:
        """
        Call when the flow stream has finished generating all audio data for a queue.

        At this point all audio data for the queue has been passed to the encoding pipeline.
        The player will go idle once it finishes playing the remaining buffered audio.

        We start a background task that waits for the player to go idle and checks if new
        items have been added to the queue in the meantime, resuming playback if so.

        :param queue_id: The queue ID.
        :param queue_exhausted: Whether the flow ended because the queue ran out of items,
            as opposed to ending early to restart on a format change or a live item.
        """
        queue = self.get(queue_id)
        if not queue:
            return
        self.logger.debug("Queue flow buffer completed for %s", queue.display_name)

        # capture session_id so we can bail out if playback restarts
        queue_data = self._queue_data[queue_id]
        original_session_id = queue_data.session_id
        # record so player providers can detect flow EOF without an idle report
        if original_session_id is not None:
            queue_data.flow_buffer_completed = original_session_id
            if queue_exhausted:
                queue_data.flow_queue_exhausted = original_session_id

        async def _resume_on_idle() -> None:
            # wait for the player to finish playing the buffered audio and go idle
            idle_detected = False
            for _ in range(60):
                await asyncio.sleep(1)
                if not queue.active or queue_data.session_id != original_session_id:
                    return
                if queue.state == PlaybackState.IDLE:
                    idle_detected = True
                    break
            if not idle_detected:
                return
            # player went idle, give it a brief moment to settle
            await asyncio.sleep(1)
            if queue.state != PlaybackState.IDLE or queue_data.session_id != original_session_id:
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
        queue_data = self.queue_data_or_none(queue_id)
        if queue_data is None or queue_data.session_id is None:
            return False
        return queue_data.flow_buffer_completed == queue_data.session_id

    def flow_queue_exhausted(self, queue_id: str, session_id: str) -> bool:
        """
        Return whether the given flow stream session played the queue to its end.

        False while a session is still streaming, and for a flow stream that ended early
        to be restarted (a format change or a live item), where the player is expected to
        pick up the next stream right away.

        :param queue_id: The queue ID.
        :param session_id: The stream session to check.
        """
        queue_data = self.queue_data_or_none(queue_id)
        if queue_data is None or queue_data.session_id != session_id:
            return False
        return queue_data.flow_queue_exhausted == session_id

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
        if (queue_data := self._queue_data.get(queue_id)) is None:
            return None
        queue_items = queue_data.items
        if isinstance(item_id_or_index, int) and len(queue_items) > item_id_or_index:
            return queue_items[item_id_or_index]
        if isinstance(item_id_or_index, str):
            return next((x for x in queue_items if x.queue_item_id == item_id_or_index), None)
        return None

    def signal_update(self, queue_id: str, items_changed: bool = False) -> None:
        """Signal state changed of given queue."""
        if (queue_data := self._queue_data.get(queue_id)) is None:
            return
        queue = queue_data.queue
        if items_changed:
            queue_data.items_cache_dirty = True
            self.mass.signal_event(EventType.QUEUE_ITEMS_UPDATED, object_id=queue_id, data=queue)
        self.mass.streams.audio_processing.prune(queue_id)
        # always send the base event
        self.mass.signal_event(EventType.QUEUE_UPDATED, object_id=queue_id, data=queue)
        # also signal update to the player itself so it can update its current_media
        self.mass.players.trigger_player_update(queue_id)
        # persist the (settings-bearing) queue state, debounced so a burst of updates or the
        # per-track updates during playback collapse into a single cache write
        self.mass.call_later(
            QUEUE_CACHE_SAVE_DELAY,
            self._save_queue_to_cache,
            queue_id,
            task_id=f"save_queue_cache_{queue_id}",
        )

    def index_by_id(self, queue_id: str, queue_item_id: str) -> int | None:
        """Get index by queue_item_id."""
        if (queue_data := self._queue_data.get(queue_id)) is None:
            return None
        for index, item in enumerate(queue_data.items):
            if item.queue_item_id == queue_item_id:
                return index
        return None

    async def get_tracks_for_playback(self, media_item: MediaItemType) -> list[Track]:
        """
        Return the playable tracks a media item resolves to, honoring the user's selection prefs.

        :param media_item: The media item to resolve to playable tracks.
        """
        return await self._media_resolver.get_tracks_for_playback(media_item)

    async def get_playlist_tracks(
        self, playlist: Playlist, start_item: str | None = None, sort_by: str | None = None
    ) -> list[PlaylistPlayableItem]:
        """
        Return the playable tracks for a playlist, honoring the user's selection prefs.

        :param playlist: The playlist to resolve.
        :param start_item: Optional item URI to start the playlist from.
        :param sort_by: Optional sort key for the returned tracks.
        """
        return await self._media_resolver.get_playlist_tracks(playlist, start_item, sort_by)

    async def get_dynamic_source_tracks(self, item: MediaItemType) -> list[Track]:
        """
        Return a fresh batch of tracks for a dynamic source (a dynamic playlist or radio station).

        :param item: The dynamic playlist or radio station to fetch the next batch for.
        """
        return await self._media_resolver.get_dynamic_source_tracks(item)

    def recency_windows(self) -> RecencyWindows:
        """Return the configured recency windows (a global setting; used for recency-aware gating)."""
        return self._smart_shuffle.windows()

    async def player_media_from_queue_item(self, queue_item: QueueItem) -> PlayerMedia:
        """
        Parse PlayerMedia from QueueItem.

        :param queue_item: The queue item to create media from.
        """
        queue_data = self._queue_data[queue_item.queue_id]
        stream_duration: int | None = None
        if queue_item.streamdetails:
            # prefer netto duration
            duration = queue_item.streamdetails.duration or queue_item.duration
            if duration and queue_item.streamdetails.seek_position:
                # the audio handed to the player starts at the seek position, so it is
                # shorter than the media item itself. seeking to (or past) the end
                # leaves no stream to describe, so the full length is kept instead.
                remaining = int(duration - queue_item.streamdetails.seek_position)
                stream_duration = remaining if remaining > 0 else None
        else:
            duration = queue_item.duration
        if queue_data.session_id is None:
            raise InvalidDataError("Queue session_id is None")
        media = PlayerMedia(
            uri=queue_item.uri,
            media_type=queue_item.media_type,
            title=queue_item.name,
            image_url=MASS_LOGO_ONLINE,
            duration=duration,
            stream_duration=stream_duration,
            source_id=queue_item.queue_id,
            queue_item_id=queue_item.queue_item_id,
            custom_data={
                "session_id": queue_data.session_id,
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

    def store_sources(self, queue: PlayerQueue, items: list[MediaItemType]) -> None:
        """
        Hold the queue's full dynamic-source items server-side and project them onto `sources`.

        :param queue: The queue whose sources are being set.
        :param items: The full source media items; an empty list clears the queue's sources.
        """
        self._queue_data[queue.queue_id].source_items = items
        # keep every occurrence server-side (a source added more than once weights it up in the
        # managed pool), but expose only the distinct container sources on the wire for clients to
        # show. Individual items (tracks, live radio streams, podcast episodes, ...) are omitted; see
        # `_WIRE_SOURCE_MEDIA_TYPES`. Autoplay/pool refill reads the full `source_items` above, not
        # this projected list, so it is unaffected.
        seen: set[str] = set()
        sources: list[ItemMapping] = []
        for item in items:
            if item.media_type not in _WIRE_SOURCE_MEDIA_TYPES and not is_dynamic_source(item):
                continue
            mapping = ItemMapping.from_item(item)
            if mapping.uri and mapping.uri in seen:
                continue
            if mapping.uri:
                seen.add(mapping.uri)
            sources.append(mapping)
        queue.sources = sources
        # release any materialized finite-source state whose source is no longer present
        self._managed_pool.retain(
            queue.queue_id, {item.uri for item in items if item.uri is not None}
        )

    async def _save_queue_to_cache(self, queue_id: str) -> None:
        """Persist the queue's state (and its items when changed) to the cache."""
        if (queue_data := self._queue_data.get(queue_id)) is None:
            return
        try:
            # persistent so a cache clear/reset does not wipe the user's queues; the default
            # expiration still applies but is refreshed on every write. Skip the state write when its
            # persist-worthy content is unchanged (i.e. only playback progress advanced).
            state = queue_data.to_cache()
            significant = queue_data.cache_significant(state)
            if significant != queue_data.last_saved_state:
                await self.mass.cache.set(
                    key=queue_id,
                    data=state,
                    provider=self.domain,
                    category=CACHE_CATEGORY_PLAYER_QUEUE_STATE,
                    persistent=True,
                )
                queue_data.last_saved_state = significant
            if queue_data.items_cache_dirty:
                # only cache items with a valid media_item
                await self.mass.cache.set(
                    key=queue_id,
                    data=queue_data.items_to_cache(),
                    provider=self.domain,
                    category=CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
                    persistent=True,
                )
                queue_data.items_cache_dirty = False
        except Exception as err:
            self.logger.warning("Failed to persist the queue for %s - %s", queue_id, err)

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
    async def _handle_play(self, queue_id: str) -> None:
        """Handle play without acquiring the queue lock."""
        queue_player = self.mass.players.get_player(queue_id, True)
        if queue_player is None:
            raise PlayerUnavailableError(f"Player {queue_id} is not available")
        if (queue := self.get(queue_id)) and queue.active and queue.state == PlaybackState.PAUSED:
            # forward the actual play/unpause command to the player,
            # holding the action until the player confirms it resumed playback
            async with self.mass.players.wait_for_player_update(
                queue_id,
                attribute_name="playback_state",
                attribute_value=PlaybackState.PLAYING,
                timeout=PLAYBACK_START_TIMEOUT,
            ):
                await queue_player.play()
            return
        # player is not paused, perform resume instead
        await self.resume(queue_id)

    def _set_transitioning(self, queue_id: str, value: bool) -> None:
        """Mark (or clear) whether a queue is mid-transition (no-op if it is not registered)."""
        if (queue_data := self._queue_data.get(queue_id)) is not None:
            queue_data.transitioning = value

    def _clear(self, queue_id: str, skip_stop: bool = False) -> None:
        """Drop the queue's items and playback position, leaving its settings untouched."""
        queue = self._queue_data[queue_id].queue
        self.mass.streams.audio_processing.clear(queue_id)
        self.store_sources(queue, [])
        queue.is_dynamic = False
        # dropping the dynamic source changes what smart shuffle resolves to, so the derived
        # flag has to follow or clients keep showing a smart mix on a plain queue
        queue.smart_shuffle_active = self.is_smart_shuffle_active(queue)
        queue.ended = False
        if queue.state != PlaybackState.IDLE and not skip_stop:
            self.mass.create_task(self.stop(queue_id))
        queue.current_index = None
        queue.current_item = None
        queue.elapsed_time = 0
        queue.elapsed_time_last_updated = time.time()
        queue.index_in_buffer = None
        self.mass.create_task(self._cleanup_queue_audio_data(queue_id))
        self.update_items(queue_id, [])

    def _reset_shuffle(self, queue_id: str) -> None:
        """Switch shuffle off and drop any pending shuffle intent."""
        queue_data = self._queue_data[queue_id]
        queue_data.shuffle_set_at = None
        queue = queue_data.queue
        if not queue.shuffle_enabled:
            return
        queue.shuffle_enabled = False
        queue.smart_shuffle_active = self.is_smart_shuffle_active(queue)
        self.signal_update(queue_id)

    async def _apply_shuffle_intent(
        self, queue_id: str, option: QueueOption, shuffle: bool | None
    ) -> None:
        """
        Settle the queue's shuffle state for a play command before its items are resolved.

        :param queue_id: The queue the media is played on.
        :param option: The enqueue option this command resolved to.
        :param shuffle: Explicit shuffle request from the caller; None to derive it.
        """
        queue_data = self._queue_data[queue_id]
        queue = queue_data.queue
        if option == QueueOption.REPLACE_NEXT and queue.is_dynamic:
            # The one staging option that replaces the queue's sources, so the smart mix may be on
            # its way out - and its shuffle is never the user's own (a dynamic queue's toggle is
            # locked), so it must not outlive the source that imposed it. Recorded directly, as in
            # the still-dynamic case below: the state is provisional until the sources are known,
            # and `_enter_dynamic_mode` forces shuffle back on if the queue stays dynamic.
            queue.shuffle_enabled = False
            return
        if option not in (QueueOption.PLAY, QueueOption.REPLACE):
            # only the options that start playing the media right away are a new listening
            # session; staging items for later leaves the queue's shuffle state alone
            return
        if shuffle is None:
            # Without an explicit request, shuffle only carries over when the user asked for it
            # moments ago: starting an album is otherwise expected to play it in track order,
            # regardless of what the previous listening session left switched on.
            shuffle = (
                queue_data.shuffle_set_at is not None
                and (time.monotonic() - queue_data.shuffle_set_at) < SHUFFLE_INTENT_WINDOW
            )
        if queue.shuffle_enabled != shuffle:
            if queue.is_dynamic:
                # set_shuffle refuses a queue that is still a smart mix, but the media being
                # started may well replace that dynamic source - and the items are resolved
                # against this flag. Record the requested state directly: should the queue stay
                # dynamic, `_enter_dynamic_mode` forces shuffle back on further down.
                queue.shuffle_enabled = shuffle
            else:
                # routed through set_shuffle so switching shuffle off also restores the order of
                # the items that stay in the queue: a play keeps them, and a tail left in shuffled
                # order behind a queue that now reads unshuffled would contradict its own flag
                await self.set_shuffle(queue_id, shuffle)
        # the intent belongs to this play command alone, whether or not it changed anything
        queue_data.shuffle_set_at = None
