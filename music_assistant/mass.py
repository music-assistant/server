"""Main Music Assistant class."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from functools import partial
from time import time
from types import TracebackType
from typing import Any, Callable, Coroutine, Deque, List, Optional, Tuple, Type, Union
from uuid import uuid4

import aiohttp

from music_assistant.constants import ROOT_LOGGER_NAME
from music_assistant.controllers.cache import CacheController
from music_assistant.controllers.database import DatabaseController
from music_assistant.controllers.metadata.metadata import MetaDataController
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.streams import StreamsController
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

            config: Music Assistant runtimestartup Config
            session: Optionally provide an aiohttp clientsession
        """

        self.config = config
        self.loop: asyncio.AbstractEventLoop = None
        self.http_session: aiohttp.ClientSession = session
        self.http_session_provided = session is not None
        self.logger = logging.getLogger(ROOT_LOGGER_NAME)

        self._listeners = []
        self._jobs: Deque[BackgroundJob] = deque()
        self._jobs_event = asyncio.Event()

        # init core controllers
        self.database = DatabaseController(self)
        self.cache = CacheController(self)
        self.metadata = MetaDataController(self)
        self.music = MusicController(self)
        self.players = PlayerController(self)
        self.streams = StreamsController(self)
        self._tracked_tasks: List[asyncio.Task] = []
        self.closed = False

    async def setup(self) -> None:
        """Async setup of music assistant."""
        # initialize loop
        self.loop = asyncio.get_running_loop()
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
        await self.database.close()
        self.closed = True
        if self.http_session and not self.http_session_provided:
            await self.http_session.close()

    def signal_event(self, event: MassEvent) -> None:
        """Signal event to subscribers."""
        if self.closed:
            return
        if self.logger.isEnabledFor(logging.DEBUG):
            if event.type != EventType.QUEUE_TIME_UPDATED:
                # do not log queue time updated events because that is too chatty
                self.logger.getChild("event").debug(
                    "%s %s", event.type.value, event.object_id or ""
                )
        for cb_func, event_filter, id_filter in self._listeners:
            if not (event_filter is None or event.type in event_filter):
                continue
            if not (id_filter is None or event.object_id in id_filter):
                continue
            if asyncio.iscoroutinefunction(cb_func):
                asyncio.run_coroutine_threadsafe(cb_func(event), self.loop)
            else:
                self.loop.call_soon_threadsafe(cb_func, event)

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
    ) -> BackgroundJob:
        """Add job to be (slowly) processed in the background."""
        if not allow_duplicate:
            if existing := next((x for x in self._jobs if x.name == name), None):
                self.logger.debug("Ignored duplicate job: %s", name)
                coro.close()
                return existing
        if not name:
            name = coro.__qualname__ or coro.__name__
        job = BackgroundJob(str(uuid4()), name=name, coro=coro)
        self._jobs.append(job)
        self._jobs_event.set()
        self.signal_event(MassEvent(EventType.BACKGROUND_JOB_UPDATED, data=job))
        return job

    def create_task(
        self,
        target: Coroutine,
        *args: Any,
        **kwargs: Any,
    ) -> Union[asyncio.Task, asyncio.Future]:
        """
        Create Task on (main) event loop from Callable or awaitable.

        Tasks created by this helper will be properly cancelled on stop.
        """
        if self.closed:
            return

        if asyncio.iscoroutinefunction(target):
            task = self.loop.create_task(target(*args, **kwargs))
        else:
            task = self.loop.create_task(target)

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
                task = self.create_task(next_job.coro)
                task.set_name(next_job.name)
                task.add_done_callback(partial(self.__job_done_cb, job=next_job))
                self.signal_event(
                    MassEvent(
                        EventType.BACKGROUND_JOB_UPDATED, next_job.name, data=next_job
                    )
                )

    def __job_done_cb(self, task: asyncio.Task, job: BackgroundJob):
        """Call when background job finishes."""
        execution_time = round(time() - job.timestamp, 2)
        job.timestamp = execution_time
        if task.cancelled():
            job.status = JobStatus.CANCELLED
        elif err := task.exception():
            job.status = JobStatus.ERROR
            self.logger.error(
                "Job [%s] failed with error %s.",
                job.name,
                str(err),
                exc_info=err,
            )
        else:
            job.result = task.result()
            job.status = JobStatus.FINISHED
            self.logger.info(
                "Finished job [%s] in %s seconds.", job.name, execution_time
            )
        self._jobs.remove(job)
        self._jobs_event.set()
        # mark job as done
        job.done()
        self.signal_event(
            MassEvent(EventType.BACKGROUND_JOB_FINISHED, job.name, data=job)
        )

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
