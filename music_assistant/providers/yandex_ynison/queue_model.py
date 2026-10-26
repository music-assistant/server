"""Pure Ynison queue interpretation and queue-edit construction."""

from __future__ import annotations

import random
import time
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from music_assistant_models.enums import RepeatMode

# The official client publishes at most this many playables around the
# current item; RADIO appends keep the same window.
QUEUE_WINDOW = 200

# Entity types whose queue end is replenished by the active device.
RADIO_ENTITY_TYPES = frozenset({"RADIO"})

QueueOutcome = Literal["change", "restart", "stop", "radio", "failed"]

_REPEAT_TO_YNISON = {RepeatMode.OFF: "NONE", RepeatMode.ONE: "ONE", RepeatMode.ALL: "ALL"}
_REPEAT_FROM_YNISON = {wire: mode for mode, wire in _REPEAT_TO_YNISON.items()}


def make_version_block(device_id: str) -> dict[str, Any]:
    """
    Build a version sub-object authored by the given device.

    Ynison expects string types for version and timestamp fields;
    passing integers triggers 500 responses that terminate the WebSocket.
    """
    return {
        "device_id": device_id,
        "version": str(time.time_ns()),
        "timestamp_ms": "0",
    }


def insert_shuffle_indices(
    order: list[int], position: int, count: int, *, after_current: int | None = None
) -> list[int]:
    """Return a shuffle permutation after inserting original-list positions."""
    shifted = [index + count if index >= position else index for index in order]
    inserted = list(range(position, position + count))
    if after_current is None:
        return [*shifted, *inserted]
    shifted_current = after_current + count if after_current >= position else after_current
    logical_position = shifted.index(shifted_current) + 1
    return [*shifted[:logical_position], *inserted, *shifted[logical_position:]]


class YnisonQueueView:
    """Expose validated logical navigation over an Ynison player queue."""

    def __init__(self, queue: Mapping[str, Any]) -> None:
        """Initialize a queue view from one complete player-queue object."""
        self._queue = queue
        playable_list = queue.get("playable_list")
        self._size = len(playable_list) if isinstance(playable_list, list) else 0
        raw_current = queue.get("current_playable_index", -1)
        self.current_index = (
            raw_current
            if isinstance(raw_current, int) and not isinstance(raw_current, bool)
            else -1
        )
        raw_shuffle = queue.get("shuffle_optional")
        raw_order = (
            raw_shuffle.get("playable_indices") if isinstance(raw_shuffle, Mapping) else None
        )
        sequential = tuple(range(self._size))
        if (
            isinstance(raw_order, list)
            and len(raw_order) == self._size
            and all(isinstance(index, int) and not isinstance(index, bool) for index in raw_order)
            and set(raw_order) == set(sequential)
        ):
            self.order: tuple[int, ...] = tuple(raw_order)
            self.shuffle_enabled = True
        else:
            self.order = sequential
            self.shuffle_enabled = False

    def next_index(self, *, wrap: bool = False) -> int | None:
        """Return the next original-list index in logical playback order."""
        return self._relative_index(1, wrap=wrap)

    def previous_index(self, *, wrap: bool = False) -> int | None:
        """Return the previous original-list index in logical playback order."""
        return self._relative_index(-1, wrap=wrap)

    def _relative_index(self, offset: int, *, wrap: bool) -> int | None:
        if self.current_index not in self.order:
            return None
        logical_position = self.order.index(self.current_index) + offset
        if wrap and self.order:
            logical_position %= len(self.order)
        if 0 <= logical_position < len(self.order):
            return self.order[logical_position]
        return None


def repeat_from_ynison(queue: Mapping[str, Any]) -> RepeatMode:
    """Return the Music Assistant repeat mode of a Ynison player queue."""
    options = queue.get("options")
    value = options.get("repeat_mode", "NONE") if isinstance(options, Mapping) else "NONE"
    return _REPEAT_FROM_YNISON.get(value, RepeatMode.UNKNOWN)


def plan_navigation(
    player_state: dict[str, Any], *, natural: bool, duration_ms: int, device_id: str
) -> QueueOutcome:
    """
    Advance a player state to the next logical item, honouring repeat.

    :param player_state: Complete player state to edit in place.
    :param natural: Whether the current item finished rather than being skipped.
    :param duration_ms: Duration of the finished item, published on a terminal stop.
    :param device_id: This device, the author of the new status.
    :return: ``radio`` without editing when a RADIO queue needs more items,
        otherwise ``change``, ``restart`` or ``stop``.
    """
    queue = player_state["player_queue"]
    view = YnisonQueueView(queue)
    repeat = repeat_from_ynison(queue)
    radio = queue.get("entity_type") in RADIO_ENTITY_TYPES
    if natural and repeat == RepeatMode.ONE:
        next_index: int | None = view.current_index
    else:
        next_index = view.next_index(wrap=repeat == RepeatMode.ALL and not radio)
    if next_index is None and radio:
        return "radio"
    status = dict(player_state.get("status", {}))
    if next_index is None:
        status.update(progress_ms=str(duration_ms), duration_ms=str(duration_ms), paused=True)
        outcome: QueueOutcome = "stop"
    else:
        queue["current_playable_index"] = next_index
        status.update(progress_ms="0", duration_ms="0", paused=False)
        outcome = "restart" if next_index == view.current_index else "change"
    status["version"] = make_version_block(device_id)
    player_state["status"] = status
    return outcome


def set_index(
    player_state: dict[str, Any],
    index: int,
    *,
    device_id: str,
    items: list[dict[str, Any]] | None = None,
) -> None:
    """
    Move a player state to ``index`` and restart its status.

    :param player_state: Complete player state to edit in place.
    :param index: Original-list index of the new current item, counted in ``items``
        when they are supplied.
    :param device_id: This device, the author of the new status.
    :param items: Optional replacement playable list that appends to the current one.
    """
    if items is not None:
        index -= replace_items(player_state, items)
    player_state["player_queue"]["current_playable_index"] = index
    status = dict(player_state.get("status", {}))
    status.update(progress_ms="0", duration_ms="0", paused=False)
    status["version"] = make_version_block(device_id)
    player_state["status"] = status


def replace_items(player_state: dict[str, Any], items: list[dict[str, Any]]) -> int:
    """
    Replace the playable list with one that appends to it.

    Appended items join an existing shuffle permutation. An unshuffled list is
    trimmed from the front to :data:`QUEUE_WINDOW` without dropping the current
    item.

    :param player_state: Complete player state to edit in place.
    :param items: The current playables followed by the appended ones.
    :return: Number of leading items dropped by the window.
    """
    queue = dict(player_state["player_queue"])
    old_count = len(queue.get("playable_list", []))
    queue["playable_list"] = list(items)
    shuffle = queue.get("shuffle_optional")
    dropped = 0
    if isinstance(shuffle, dict) and isinstance(shuffle.get("playable_indices"), list):
        queue["shuffle_optional"] = {
            **shuffle,
            "playable_indices": insert_shuffle_indices(
                shuffle["playable_indices"], old_count, len(items) - old_count
            ),
        }
    else:
        current = queue.get("current_playable_index", -1)
        current = current if isinstance(current, int) and current > 0 else 0
        dropped = min(max(0, len(items) - QUEUE_WINDOW), current)
        if dropped:
            queue["playable_list"] = queue["playable_list"][dropped:]
            queue["current_playable_index"] = current - dropped
    player_state["player_queue"] = queue
    return dropped


def set_repeat(player_state: dict[str, Any], mode: RepeatMode) -> None:
    """
    Publish a Music Assistant repeat mode in a player state.

    :param player_state: Complete player state to edit in place.
    :param mode: Repeat mode other than ``UNKNOWN``.
    """
    queue = dict(player_state["player_queue"])
    queue["options"] = {**queue.get("options", {}), "repeat_mode": _REPEAT_TO_YNISON[mode]}
    player_state["player_queue"] = queue


def set_shuffle(
    player_state: dict[str, Any], *, enabled: bool, rng: random.Random | None = None
) -> None:
    """
    Publish or remove a shuffle permutation that starts at the current item.

    :param player_state: Complete player state to edit in place.
    :param enabled: Whether shuffle is on.
    :param rng: Random source for the permutation.
    """
    queue = dict(player_state["player_queue"])
    count = len(queue.get("playable_list", []))
    current = queue.get("current_playable_index", -1)
    if enabled and isinstance(current, int) and 0 <= current < count:
        remaining = [index for index in range(count) if index != current]
        sample = (rng or random).sample(remaining, len(remaining))
        queue["shuffle_optional"] = {"playable_indices": [current, *sample]}
    else:
        queue.pop("shuffle_optional", None)
    player_state["player_queue"] = queue


def radio_items(tracks: Sequence[Any], origin: str) -> list[dict[str, Any]]:
    """
    Map rotor tracks to Ynison playables.

    :param tracks: Track objects returned by the rotor API.
    :param origin: ``from`` value of the queue the items join.
    """
    items: list[dict[str, Any]] = []
    for track in tracks:
        albums = getattr(track, "albums", None)
        album_id = str(albums[0].id) if albums and albums[0].id else ""
        items.append(
            {
                "playable_id": str(track.id),
                "album_id_optional": album_id,
                "playable_type": "TRACK",
                "from": origin,
                "title": track.title or "",
                "cover_url_optional": getattr(track, "cover_uri", None) or "",
            }
        )
    return items
