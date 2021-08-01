"""Special queue-like to process items in different states."""
import asyncio
from collections import deque
from typing import Any, List, Type


class MultiStateQueue:
    """Special queue-like to process items in different states."""

    QUEUE_ITEM_TYPE: Type = Any

    def __init__(self, max_finished_items: int = 50) -> None:
        """Initialize class."""
        self._pending_items = asyncio.Queue()
        self._progress_items = deque()
        self._finished_items = deque(maxlen=max_finished_items)

    @property
    def pending_items(self) -> List[QUEUE_ITEM_TYPE]:
        """Return all pending items."""
        # pylint: disable=protected-access
        return list(self._pending_items._queue)

    @property
    def progress_items(self) -> List[QUEUE_ITEM_TYPE]:
        """Return all in-progress items."""
        return list(self._progress_items)

    @property
    def finished_items(self) -> List[QUEUE_ITEM_TYPE]:
        """Return all finished items."""
        return list(self._finished_items)

    @property
    def all_items(self) -> List[QUEUE_ITEM_TYPE]:
        """Return all items."""
        return list(self.pending_items + self.progress_items + self.finished_items)

    def put_nowait(self, item: QUEUE_ITEM_TYPE) -> None:
        """Put item in the queue to progress."""
        if item in self._finished_items:
            self._finished_items.remove(item)
        return self._pending_items.put_nowait(item)

    async def put(self, item: QUEUE_ITEM_TYPE) -> None:
        """Put item on the queue to progress."""
        if item in self._finished_items:
            self._finished_items.remove(item)
        return await self._pending_items.put(item)

    async def get_nowait(self) -> QUEUE_ITEM_TYPE:
        """Get next item in Queue, raises QueueEmpty if no items in Queue."""
        next_item = self._pending_items.get_nowait()
        self._progress_items.append(next_item)
        return next_item

    async def get(self) -> QUEUE_ITEM_TYPE:
        """Get next item in Queue, waits until item is available."""
        next_item = await self._pending_items.get()
        self._progress_items.append(next_item)
        return next_item

    def mark_finished(self, item: QUEUE_ITEM_TYPE) -> None:
        """Mark item as finished."""
        self._progress_items.remove(item)
        self._finished_items.append(item)
