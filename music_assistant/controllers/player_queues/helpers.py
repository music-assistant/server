"""Helper utilities for the player queues controller."""

from __future__ import annotations

import functools
import random
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Concatenate, Protocol, TypedDict, TypeGuard, TypeVar

from music_assistant_models.media_items import MediaItemMetadata, Playlist, Radio, Track
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import ATTR_PLAY_ACTION_IN_PROGRESS, PlaylistPlayableItem
from music_assistant.controllers.players.constants import PlayerLockPurpose

if TYPE_CHECKING:
    from music_assistant_models.enums import ContentType, PlaybackState
    from music_assistant_models.media_items import (
        BrowseFolder,
        MediaItemType,
        PlayableMediaItemType,
    )
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant import MusicAssistant
    from music_assistant.controllers.player_queues.state import PlayerQueueData
    from music_assistant.models.player import Player

_SortableT = TypeVar("_SortableT", bound=PlaylistPlayableItem)


class CompareState(TypedDict):
    """
    Simple object where we store the (previous) state of a queue.

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
    output_player_ids: list[str] | None


class _PlayActionHost(Protocol):
    """
    The minimal controller surface that :func:`handle_play_action` needs.

    Lets the decorator wrap actions defined either on the controller itself or on one
    of its mixins, since both expose this surface at runtime.
    """

    mass: MusicAssistant
    _queue_data: dict[str, PlayerQueueData]

    def signal_update(self, queue_id: str, items_changed: bool = False) -> None: ...

    def on_player_update(
        self, player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None: ...


def handle_play_action[PlayActionHostT: _PlayActionHost, **P, R](
    func: Callable[Concatenate[PlayActionHostT, P], Awaitable[R]],
) -> Callable[Concatenate[PlayActionHostT, P], Coroutine[Any, Any, R]]:
    """
    Decorator for queue playback actions.

    Acquires the shared playback lock for the queue's player (re-entrant)
    and sets ATTR_PLAY_ACTION_IN_PROGRESS on the queue while the action runs.
    Uses an internal refcount so nested actions don't clear the flag prematurely.

    :param func: The function to wrap.
    """  # noqa: D401

    @functools.wraps(func)
    async def wrapper(self: PlayActionHostT, *args: P.args, **kwargs: P.kwargs) -> R:
        """Execute function with playback lock and play action flag set."""
        queue_id = kwargs.get("queue_id") or args[0]
        assert isinstance(queue_id, str)  # for type checking
        queue_data = self._queue_data.get(queue_id)
        if queue_data is None:
            return await func(self, *args, **kwargs)
        queue = queue_data.queue
        async with self.mass.players.get_player_lock(queue_id, PlayerLockPurpose.PLAYBACK):
            prev_in_progress = queue.extra_attributes.get(ATTR_PLAY_ACTION_IN_PROGRESS, False)
            try:
                queue_data.play_action_refcount += 1
                queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] = True
                if not prev_in_progress:
                    self.signal_update(queue_id)
                return await func(self, *args, **kwargs)
            finally:
                queue_data.play_action_refcount -= 1
                if queue_data.play_action_refcount <= 0:
                    queue_data.play_action_refcount = 0
                    queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] = False
                    # the queue follows the player through a debounced update, which is also
                    # suppressed while an action is transitioning; recalculate it here so the
                    # update that clears the flag already carries the action's resulting state
                    if (player := self.mass.players.get_player(queue_id)) is not None:
                        self.on_player_update(player, {})
                    self.signal_update(queue_id)

    return wrapper


def is_dynamic_source(item: MediaItemType | BrowseFolder) -> TypeGuard[Playlist | Radio]:
    """Return True if the item supplies its own on-demand track feed."""
    return isinstance(item, Playlist | Radio) and item.is_dynamic


def find_dynamic_source(queue_data: PlayerQueueData) -> MediaItemType | None:
    """
    Return the queue's most recently added dynamic source, if it has one.

    Prefers the queue's sources and falls back to what was enqueued on it.

    :param queue_data: The queue to inspect.
    """
    for items in (queue_data.source_items, queue_data.enqueued_media_items):
        for item in reversed(items):
            if is_dynamic_source(item):
                return item
    return None


def has_dynamic_source(source_items: list[MediaItemType]) -> bool:
    """Return True if any source supplies its own on-demand track feed (the queue is dynamic)."""
    return any(is_dynamic_source(item) for item in source_items)


def build_queue_item(queue_id: str, media_item: PlayableMediaItemType) -> QueueItem:
    """
    Build a QueueItem for enqueueing, keeping its media item slim.

    The returned item only carries the media details needed for the queue listing and stream
    resolution. For tracks the full metadata is dropped; it is restored from the library when
    the item becomes the queue's current or next item, so large queues stay light on memory
    and persisted-cache size.

    :param queue_id: The id of the queue the item is created for.
    :param media_item: The source media item to enqueue.
    """
    queue_item = QueueItem.from_media_item(queue_id, media_item)
    if isinstance(queue_item.media_item, Track):
        # the list-row artwork is already captured on QueueItem.image, so dropping the
        # track's metadata here does not lose anything the queue listing still needs
        queue_item.media_item.metadata = MediaItemMetadata()
    return queue_item


def sort_tracks(tracks: list[_SortableT], sort_by: str) -> list[_SortableT]:
    """Sort tracks by the given sort key."""
    key_map: dict[str, tuple[Any, bool]] = {
        "position_desc": (lambda t: getattr(t, "position", 0) or 0, True),
        "name": (lambda t: (t.sort_name or t.name or "").lower(), False),
        "artist": (
            lambda t: (
                (t.artists[0].sort_name or t.artists[0].name).lower()
                if hasattr(t, "artists") and t.artists
                else ""
            ),
            False,
        ),
        "album": (
            lambda t: (
                (t.album.sort_name or t.album.name).lower()
                if hasattr(t, "album") and t.album
                else ""
            ),
            False,
        ),
        "duration": (lambda t: getattr(t, "duration", 0) or 0, False),
        "duration_desc": (lambda t: getattr(t, "duration", 0) or 0, True),
        "track_number": (
            lambda t: (
                getattr(t, "disc_number", 0) or 0,
                getattr(t, "track_number", 0) or 0,
            ),
            False,
        ),
    }
    if sort_by in key_map:
        key_fn, reverse = key_map[sort_by]
        return sorted(tracks, key=key_fn, reverse=reverse)
    return list(tracks)


def get_current_playback_speed(queue: PlayerQueue) -> float:
    """Return the playback_speed of the queue's current item (1.0 if unset)."""
    if queue.current_item is None:
        return 1.0
    return float(queue.current_item.extra_attributes.get("playback_speed") or 1.0)


def committed_index(queue: PlayerQueue) -> int | None:
    """
    Return the last queue index the player has already committed to, or None if it has none.

    Everything up to and including this index is settled: the next track is handed to the player
    long before it starts, so changing the queue at or before this point leaves the player playing
    something the queue no longer lists. Insert behind it instead.

    :param queue: The queue to resolve the index for.
    """
    # repeat wraps the buffered index back to the front of the queue, which would otherwise put
    # the boundary before the playing track, so take whichever of the two is furthest along
    if queue.current_index is None:
        return queue.index_in_buffer
    if queue.index_in_buffer is None:
        return queue.current_index
    return max(queue.current_index, queue.index_in_buffer)


def interleave_groups[ItemT](groups: list[list[ItemT]]) -> list[ItemT]:
    """
    Randomly interleave groups while preserving the item order within each group.

    :param groups: The ordered item groups to spread across the result.
    """
    positioned: list[tuple[float, ItemT]] = []
    for items in groups:
        total = len(items)
        for offset, item in enumerate(items):
            positioned.append(((offset + random.random()) / total, item))
    positioned.sort(key=lambda entry: entry[0])
    return [item for _, item in positioned]


# how many bounded passes to make separating directly-adjacent same-artist items
ARTIST_REPAIR_PASSES = 4
# how far ahead to look for a non-clashing item to swap in (keeps moves local so any
# existing even spread is preserved)
ARTIST_SWAP_WINDOW = 6


def space_by_artist(artist_sets: list[set[str]], *, preceding: set[str] | None = None) -> list[int]:
    """
    Return an index order that best-effort keeps same-artist entries from sitting adjacent.

    :param artist_sets: The lowercased artist-name set for each item, in its current order.
    :param preceding: Artist names of the item that will sit directly before the first entry (the
        seam with the already-queued tail); the first entry is kept clear of it too. None ignores it.
    """
    count = len(artist_sets)
    order = list(range(count))
    sets = list(artist_sets)
    for _ in range(ARTIST_REPAIR_PASSES):
        changed = False
        # index -1 represents the preceding (seam) item, so the first entry is kept clear of it too
        for index in range(-1, count - 1):
            current = preceding if index == -1 else sets[index]
            if not current or not current & sets[index + 1]:
                continue
            for target in range(index + 2, min(index + 2 + ARTIST_SWAP_WINDOW, count)):
                if not current & sets[target]:
                    order[index + 1], order[target] = order[target], order[index + 1]
                    sets[index + 1], sets[target] = sets[target], sets[index + 1]
                    changed = True
                    break
        if not changed:
            break
    return order
