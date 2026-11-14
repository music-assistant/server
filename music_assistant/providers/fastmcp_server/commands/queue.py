"""Plain Music Assistant queue command handlers."""
# ruff: noqa: TID252 -- provider source is transplanted under the MA package.

from __future__ import annotations

from typing import Any

from music_assistant_models.errors import InvalidDataError

from ..models import RemoveFromQueueResult


async def remove_items_safe(mass: Any, queue_id: str, item_ids: list[str]) -> RemoveFromQueueResult:
    """Remove only queue items that are neither played nor player-buffered."""
    if not item_ids:
        raise InvalidDataError("Provide at least one queue item id")
    queue = mass.player_queues.get(queue_id)
    if queue is None:
        raise InvalidDataError(f"Queue {queue_id!r} not found")

    result = RemoveFromQueueResult()
    for item_id in item_ids:
        index = mass.player_queues.index_by_id(queue_id, item_id)
        if index is None:
            result.not_found.append(item_id)
        elif queue.current_index is not None and index <= queue.current_index:
            result.skipped_played.append(item_id)
        elif queue.index_in_buffer is not None and index <= queue.index_in_buffer:
            result.skipped_buffered.append(item_id)
        else:
            try:
                mass.player_queues.delete_item(queue_id, item_id)
            except KeyError, InvalidDataError:
                result.not_found.append(item_id)
                continue
            bucket = (
                result.removed
                if mass.player_queues.index_by_id(queue_id, item_id) is None
                else result.skipped_buffered
            )
            bucket.append(item_id)
    return result
