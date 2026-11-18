"""Pure Ynison queue-order interpretation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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
