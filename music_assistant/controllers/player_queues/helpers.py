"""Helper utilities for the player queues controller."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Concatenate, Protocol, TypedDict, TypeVar

from music_assistant_models.media_items import Playlist

from music_assistant.constants import ATTR_PLAY_ACTION_IN_PROGRESS, PlaylistPlayableItem
from music_assistant.controllers.players.constants import PlayerLockPurpose

if TYPE_CHECKING:
    from music_assistant_models.enums import ContentType, PlaybackState
    from music_assistant_models.media_items import MediaItemType
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem

    from music_assistant import MusicAssistant
    from music_assistant.controllers.player_queues.state import PlayerQueueData

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
    output_formats: list[str] | None


class _PlayActionHost(Protocol):
    """
    The minimal controller surface that :func:`handle_play_action` needs.

    Lets the decorator wrap actions defined either on the controller itself or on one
    of its mixins, since both expose this surface at runtime.
    """

    mass: MusicAssistant
    _queue_data: dict[str, PlayerQueueData]

    def signal_update(self, queue_id: str, items_changed: bool = False) -> None: ...


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
        data = self._queue_data.get(queue_id)
        if data is None:
            return await func(self, *args, **kwargs)
        queue = data.queue
        async with self.mass.players.get_player_lock(queue_id, PlayerLockPurpose.PLAYBACK):
            prev_in_progress = queue.extra_attributes.get(ATTR_PLAY_ACTION_IN_PROGRESS, False)
            try:
                data.play_action_refcount += 1
                queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] = True
                if not prev_in_progress:
                    self.signal_update(queue_id)
                return await func(self, *args, **kwargs)
            finally:
                data.play_action_refcount -= 1
                if data.play_action_refcount <= 0:
                    data.play_action_refcount = 0
                    queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] = False
                    self.signal_update(queue_id)

    return wrapper


def has_dynamic_source(source_items: list[MediaItemType]) -> bool:
    """Return True if any source is a dynamic playlist (the queue is in dynamic mode)."""
    return any(isinstance(item, Playlist) and item.is_dynamic for item in source_items)


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
