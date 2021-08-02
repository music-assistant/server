"""Logic to process tasks on the event loop."""

import asyncio
import logging
from asyncio.futures import Future
from enum import IntEnum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union
from uuid import uuid4

from music_assistant.constants import EVENT_TASK_UPDATED
from music_assistant.helpers.datetime import now
from music_assistant.helpers.muli_state_queue import MultiStateQueue
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import create_task
from music_assistant.helpers.web import api_route

LOGGER = logging.getLogger("task_manager")

MAX_SIMULTANEOUS_TASKS = 2


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
        self.updated_at = now()
        self.execution_time = 0  # time in seconds it took to process
        self.id = str(uuid4())

    def __str__(self):
        """Return string representation, used for logging."""
        return f"{self.name} ({self.id})"

    def to_dict(self) -> Dict[str, Any]:
        """Return serializable dict."""
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "error_details": self.error_details,
            "updated_at": self.updated_at.isoformat(),
            "execution_time": self.execution_time,
        }

    @property
    def dupe_hash(self):
        """Return simple hash to identify duplicate tasks."""
        return f"{self.name}.{self.target.__qualname__}.{self.args}"


class TaskManager:
    """Task manager that executes tasks from a queue in the background."""

    def __init__(self, mass: MusicAssistant):
        """Initialize TaskManager instance."""
        self.mass = mass
        self._queue = None

    async def setup(self):
        """Async initialize of module."""
        # queue can only be initialized when the loop is running
        MultiStateQueue.QUEUE_ITEM_TYPE = TaskInfo
        self._queue = MultiStateQueue()
        create_task(self.__process_tasks())

    def add(
        self,
        name: str,
        target: Union[Callable, Awaitable],
        *args: Any,
        periodic: Optional[int] = None,
        prevent_duplicate: bool = True,
        **kwargs: Any,
    ) -> TaskInfo:
        """Add a job/task to the task manager.

        name: A name to identify this task in the task queue.
        target: target to call (coroutine function or callable).
        periodic: [optional] run this task every X seconds.
        prevent_duplicate: [default true] prevent same task running at same time
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

        task_info = TaskInfo(
            name, periodic=periodic, target=target, args=args, kwargs=kwargs
        )
        if prevent_duplicate:
            for task in self._queue.progress_items + self._queue.pending_items:
                if task.dupe_hash == task_info.dupe_hash:
                    LOGGER.debug(
                        "Ignoring task %s as it is already running....", task_info.name
                    )
                    return task
        self._add_task(task_info)
        return task_info

    @api_route("tasks")
    def get_all_tasks(self) -> List[TaskInfo]:
        """Return all tasks in the TaskManager."""
        return self._queue.all_items

    def _add_task(self, task_info: TaskInfo) -> None:
        """Handle adding a task to the task queue."""
        LOGGER.debug("Adding task [%s] to Task Queue...", task_info.name)
        self._queue.put_nowait(task_info)
        self.mass.eventbus.signal(EVENT_TASK_UPDATED, task_info)

    def __task_done_callback(self, future: Future):
        task_info: TaskInfo = future.task_info
        self._queue.mark_finished(task_info)
        prev_timestamp = task_info.updated_at.timestamp()
        task_info.updated_at = now()
        task_info.execution_time = round(
            task_info.updated_at.timestamp() - prev_timestamp, 2
        )
        if future.cancelled():
            future.task_info.status = TaskStatus.CANCELLED
        elif future.exception():
            exc = future.exception()
            task_info.status = TaskStatus.ERROR
            task_info.error_details = repr(exc)
            LOGGER.debug(
                "Error while running task [%s]",
                task_info.name,
                exc_info=exc,
            )
        else:
            task_info.status = TaskStatus.FINISHED
            LOGGER.debug(
                "Task finished: [%s] in %s seconds",
                task_info.name,
                task_info.execution_time,
            )
        self.mass.eventbus.signal(EVENT_TASK_UPDATED, task_info)
        # reschedule if the task is periodic
        if task_info.periodic:
            self.mass.loop.call_later(task_info.periodic, self._add_task, task_info)

    async def __process_tasks(self):
        """Process handling of tasks in the queue."""
        while not self.mass.exit:
            while len(self._queue.progress_items) >= MAX_SIMULTANEOUS_TASKS:
                await asyncio.sleep(1)
            next_task = await self._queue.get()
            next_task.status = TaskStatus.PROGRESS
            next_task.updated_at = now()
            task = create_task(next_task.target, *next_task.args, **next_task.kwargs)
            setattr(task, "task_info", next_task)
            task.add_done_callback(self.__task_done_callback)
            self.mass.eventbus.signal(EVENT_TASK_UPDATED, next_task)
