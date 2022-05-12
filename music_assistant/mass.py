"""Main Music Assistant class."""
from __future__ import annotations

import asyncio
import functools
import logging
import threading
from collections import deque
from functools import partial
from time import time
from types import TracebackType
from typing import Any, Callable, Coroutine, Deque, List, Optional, Tuple, Type, Union
from uuid import uuid4

import aiohttp

from music_assistant.controllers.metadata import MetaDataController
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.stream import StreamController
from music_assistant.helpers.cache import Cache
from music_assistant.helpers.database import Database
from music_assistant.models.background_job import BackgroundJob
from music_assistant.models.config import MassConfig
from music_assistant.models.enums import EventType, JobStatus
from music_assistant.models.event import MassEvent

EventCallBackType = Callable[[MassEvent], None]
EventSubscriptionType = Tuple[
    EventCallBackType, Optional[Tuple[EventType]], Optional[Tuple[str]]
]


class MusicAssistant:
    """Main MusicAssistant object."""

    def __init__(
        self,
        config: MassConfig,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        """
        Create an instance of MusicAssistant.

            conf: Music Assistant runtimestartup Config
            stream_port: TCP port used for streaming audio.
            session: Optionally provide an aiohttp clientsession
        """

        self.config = config
        self.loop: asyncio.AbstractEventLoop = None
        self.http_session: aiohttp.ClientSession = session
        self.http_session_provided = session is not None
        self.logger = logging.getLogger(__name__)

        self._listeners = []
        self._jobs: Deque[BackgroundJob] = deque()
        self._jobs_event = asyncio.Event()

        # init core controllers
        self.database = Database(self)
        self.cache = Cache(self)
        self.metadata = MetaDataController(self)
        self.music = MusicController(self)
        self.players = PlayerController(self)
        self.streams = StreamController(self)
        self._tracked_tasks: List[asyncio.Task] = []
        self.closed = False

    async def setup(self) -> None:
        """Async setup of music assistant."""
        # initialize loop
        self.loop = asyncio.get_event_loop()
        # create shared aiohttp ClientSession
        if not self.http_session:
            self.http_session = aiohttp.ClientSession(
                loop=self.loop,
                connector=aiohttp.TCPConnector(ssl=False),
            )
        # setup core controllers
        await self.database.setup()
        await self.cache.setup()
        await self.music.setup()
        await self.metadata.setup()
        await self.players.setup()
        await self.streams.setup()
        self.create_task(self.__process_jobs())

    async def stop(self) -> None:
        """Stop running the music assistant server."""
        self.logger.info("Stop called, cleaning up...")
        await self.players.cleanup()
        # cancel all running tasks
        for task in self._tracked_tasks:
            task.cancel()
        self.signal_event(MassEvent(EventType.SHUTDOWN))
        self.closed = True
        if self.http_session and not self.http_session_provided:
            await self.http_session.close()

    def signal_event(self, event: MassEvent) -> None:
        """Signal event to subscribers."""
        if self.closed:
            return
        for cb_func, event_filter, id_filter in self._listeners:
            if not (event_filter is None or event.type in event_filter):
                continue
            if not (id_filter is None or event.object_id in id_filter):
                continue
            self.create_task(cb_func, event)

    def subscribe(
        self,
        cb_func: EventCallBackType,
        event_filter: Union[EventType, Tuple[EventType], None] = None,
        id_filter: Union[str, Tuple[str], None] = None,
    ) -> Callable:
        """
        Add callback to event listeners.

        Returns function to remove the listener.
            :param cb_func: callback function or coroutine
            :param event_filter: Optionally only listen for these events
            :param id_filter: Optionally only listen for these id's (player_id, queue_id, uri)
        """
        if isinstance(event_filter, EventType):
            event_filter = (event_filter,)
        if isinstance(id_filter, str):
            id_filter = (id_filter,)
        listener = (cb_func, event_filter, id_filter)
        self._listeners.append(listener)

        def remove_listener():
            self._listeners.remove(listener)

        return remove_listener

    def add_job(
        self, coro: Coroutine, name: Optional[str] = None, allow_duplicate=False
    ) -> None:
        """Add job to be (slowly) processed in the background."""
        if not allow_duplicate:
            # pylint: disable=protected-access
            if any(x for x in self._jobs if x.name == name):
                self.logger.debug("Ignored duplicate job: %s", name)
                coro.close()
                return
        if not name:
            name = coro.__qualname__ or coro.__name__
        job = BackgroundJob(str(uuid4()), name=name, coro=coro)
        self._jobs.append(job)
        self._jobs_event.set()
        self.signal_event(MassEvent(EventType.BACKGROUND_JOB_UPDATED, data=job))

    def create_task(
        self,
        target: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Union[asyncio.Task, asyncio.Future]:
        """
        Create Task on (main) event loop from Callable or awaitable.

        Tasks created by this helper will be properly cancelled on stop.
        """
        if self.closed:
            return

        # Check for partials to properly determine if coroutine function
        check_target = target
        while isinstance(check_target, functools.partial):
            check_target = check_target.func

        async def executor_wrapper(_target: Callable, *_args, **_kwargs):
            return await self.loop.run_in_executor(None, _target, *_args, **_kwargs)

        # called from other thread
        if threading.current_thread() is not threading.main_thread():
            if asyncio.iscoroutine(check_target):
                task = asyncio.run_coroutine_threadsafe(target, self.loop)
            elif asyncio.iscoroutinefunction(check_target):
                task = asyncio.run_coroutine_threadsafe(target(*args), self.loop)
            else:
                task = asyncio.run_coroutine_threadsafe(
                    executor_wrapper(target, *args, **kwargs), self.loop
                )
        else:
            if asyncio.iscoroutine(check_target):
                task = self.loop.create_task(target)
            elif asyncio.iscoroutinefunction(check_target):
                task = self.loop.create_task(target(*args))
            else:
                task = self.loop.create_task(executor_wrapper(target, *args, **kwargs))

        def task_done_callback(*args, **kwargs):
            self._tracked_tasks.remove(task)

        self._tracked_tasks.append(task)
        task.add_done_callback(task_done_callback)
        return task

    @property
    def jobs(self) -> List[BackgroundJob]:
        """Return the pending/running background jobs."""
        return list(self._jobs)

    async def __process_jobs(self):
        """Process jobs in the background."""
        while True:
            await self._jobs_event.wait()
            self._jobs_event.clear()
            # make sure we're not running more jobs than allowed
            running_jobs = tuple(x for x in self._jobs if x.status == JobStatus.RUNNING)
            slots_available = self.config.max_simultaneous_jobs - len(running_jobs)
            count = 0
            while count <= slots_available:
                count += 1
                next_job = next(
                    (x for x in self._jobs if x.status == JobStatus.PENDING), None
                )
                if next_job is None:
                    break
                # create task from coroutine and attach task_done callback
                next_job.timestamp = time()
                next_job.status = JobStatus.RUNNING
                self.logger.debug("Start processing job [%s].", next_job.name)
                task = self.create_task(next_job.coro)
                task.set_name(next_job.name)
                task.add_done_callback(partial(self.__job_done_cb, job=next_job))
                self.signal_event(
                    MassEvent(EventType.BACKGROUND_JOB_UPDATED, data=next_job)
                )

    def __job_done_cb(self, task: asyncio.Task, job: BackgroundJob):
        """Call when background job finishes."""
        execution_time = round(time() - job.timestamp, 2)
        job.timestamp = execution_time
        if task.cancelled():
            job.status = JobStatus.CANCELLED
            self.logger.debug("Job [%s] is cancelled.", job.name)
        elif err := task.exception():
            job.status = JobStatus.ERROR
            self.logger.error(
                "Job [%s] failed with error %s.",
                job.name,
                str(err),
                exc_info=err,
            )
        else:
            job.status = JobStatus.FINISHED
            self.logger.info(
                "Finished job [%s] in %s seconds.", job.name, execution_time
            )
        self._jobs.remove(job)
        self._jobs_event.set()
        self.signal_event(MassEvent(EventType.BACKGROUND_JOB_UPDATED, data=job))

    async def __aenter__(self) -> "MusicAssistant":
        """Return Context manager."""
        await self.setup()
        return self

    async def __aexit__(
        self,
        exc_type: Type[BaseException],
        exc_val: BaseException,
        exc_tb: TracebackType,
    ) -> Optional[bool]:
        """Exit context manager."""
        await self.stop()
        if exc_val:
            raise exc_val
        return exc_type
