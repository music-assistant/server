"""Logic to process tasks on the event loop."""

import asyncio
import logging
import time
from asyncio.futures import Future
from collections import deque
from enum import IntEnum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union
from uuid import uuid4

from music_assistant.constants import EVENT_TASK_UPDATED
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import create_task
from music_assistant.helpers.web import api_route

LOGGER = logging.getLogger("task_manager")


class TaskStatus(IntEnum):
    """Enum for Task status."""

    PENDING = 0
    PROGRESS = 1
    FINISHED = 2
    ERROR = 3
    CANCELLED = 4


class TaskInfo:
    """Model for a background task."""

    def __init__(
        self,
        name: str,
        target: Union[Callable, Awaitable],
        args: Any,
        kwargs: Any,
        periodic: Optional[int] = None,
    ) -> None:
        """Initialize instance."""
        self.name = name
        self.target = target
        self.args = args
        self.kwargs = kwargs
        self.periodic = periodic
        self.status = TaskStatus.PENDING
        self.error_details = ""
        self.timestamp = int(time.time())
        self.id = str(uuid4())

    def to_dict(self) -> Dict[str, Any]:
        """Return serializable dict."""
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status.name,
            "error_details": self.error_details,
            "timestamp": self.timestamp,
        }


class TaskManager:
    """Task manager that executes tasks from a queue in the background."""

    def __init__(self, mass: MusicAssistant):
        """Initialize TaskManager instance."""
        self.mass = mass
        self._queue = None

    async def setup(self):
        """Async initialize of module."""
        # queue can only be initialized when the loop is running
        self._queue = TasksQueue()
        create_task(self.__process_tasks())

    def add(
        self,
        target: Union[Callable, Awaitable],
        *args: Any,
        name: str = None,
        periodic: Optional[int] = None,
        **kwargs: Any
    ) -> Optional[asyncio.Task]:
        """Add a job/task to the task manager.

        target: target to call (coroutine function or callable).
        name: A name to identify this task in the task queue.
        periodic: [optional] run this task every X seconds,
        args: [optional] parameters for method to call.
        kwargs: [optional] parameters for method to call.
        """

        if self.mass.exit:
            return
        if self._queue is None:
            raise RuntimeError("Not yet initialized")

        if periodic and asyncio.iscoroutine(target):
            raise RuntimeError(
                "Provide a coroutine function and not a coroutine itself"
            )

        if not name:
            name = target.__qualname__.replace(".", ": ").replace("_", " ").strip()
        task_info = TaskInfo(
            name, periodic=periodic, target=target, args=args, kwargs=kwargs
        )
        self._add_task(task_info)

    @api_route("tasks")
    def get_all_tasks(self) -> List[TaskInfo]:
        """Return all tasks in the TaskManager."""
        return self._queue.all_items

    def _add_task(self, task_info: TaskInfo) -> None:
        """Handle adding a task to the task queue."""
        LOGGER.debug("Adding task %s to Task Queue...", task_info.name)
        self._queue.put_nowait(task_info)
        self.mass.eventbus.signal(EVENT_TASK_UPDATED, task_info)

    def __task_done_callback(self, future: Future):
        task_info: TaskInfo = future.task_info
        self._queue.mark_finished(task_info)
        if future.cancelled():
            future.task_info.status = TaskStatus.CANCELLED
        elif future.exception():
            exc = future.exception()
            task_info.status = TaskStatus.ERROR
            task_info.error_details = repr(exc)
            LOGGER.debug(
                "Error while running task %s",
                task_info.name,
                exc_info=exc,
            )
        else:
            task_info.status = TaskStatus.FINISHED
            self.mass.eventbus.signal(EVENT_TASK_UPDATED, task_info)
            LOGGER.debug("Task finished: %s", task_info.name)
        # reschedule if the task is periodic
        if task_info.periodic:
            self.mass.loop.call_later(task_info.periodic, self._add_task, task_info)

    async def __process_tasks(self):
        """Process handling of tasks in the queue."""
        while True:
            next_task = await self._queue.get()
            next_task.status = TaskStatus.PROGRESS
            task = create_task(next_task.target, *next_task.args, **next_task.kwargs)
            setattr(task, "task_info", next_task)
            task.add_done_callback(self.__task_done_callback)
            self.mass.eventbus.signal(EVENT_TASK_UPDATED, next_task)
            await asyncio.sleep(1)


class TasksQueue:
    """Special queue-like to store tasks in different states."""

    def __init__(self, max_finished_items: int = 50) -> None:
        """Initialize class."""
        self._pending_items = asyncio.Queue()
        self._progress_items = deque()
        self._finished_items = deque(maxlen=max_finished_items)

    @property
    def pending_items(self) -> List[TaskInfo]:
        """Return all pending Queue items."""
        # pylint: disable=protected-access
        return list(self._pending_items._queue)

    @property
    def progress_items(self) -> List[TaskInfo]:
        """Return all in-progress Queue items."""
        return list(self._progress_items)

    @property
    def finished_items(self) -> List[TaskInfo]:
        """Return all finished Queue items."""
        return list(self._finished_items)

    @property
    def all_items(self) -> List[TaskInfo]:
        """Return all Queue items."""
        return list(self.pending_items + self.progress_items + self.finished_items)

    def put_nowait(self, item: TaskInfo) -> None:
        """Put item in the queue to progress."""
        return self._pending_items.put_nowait(item)

    async def put(self, item: TaskInfo) -> None:
        """Put item in the queue to progress."""
        return await self._pending_items.put(item)

    async def get_nowait(self) -> Optional[TaskInfo]:
        """Get next item in Queue, raises QueueEmpty if no items in Queue."""
        return self._pending_items.get_nowait()

    async def get(self) -> TaskInfo:
        """Get next item in Queue, waits until item is available."""
        next_item = await self._pending_items.get()
        self._progress_items.append(next_item)
        return next_item

    def mark_finished(self, item: TaskInfo) -> None:
        """Mark item as finished."""
        self._progress_items.remove(item)
        self._finished_items.append(item)
