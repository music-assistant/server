"""Main Music Assistant class."""
from __future__ import annotations

import asyncio
import logging
from time import time
from typing import Any, Callable, Coroutine, Optional, Tuple, Union

import aiohttp
from databases import DatabaseURL
from music_assistant.constants import EventType
from music_assistant.controllers.metadata import MetaDataController
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.players import PlayerController
from music_assistant.helpers import util
from music_assistant.helpers.cache import Cache
from music_assistant.helpers.database import Database
from music_assistant.helpers.util import create_task

EventCallBackType = Callable[[EventType, Any], None]
EventSubscriptionType = Tuple[EventCallBackType, Optional[Tuple[EventType]]]


class MusicAssistant:
    """Main MusicAssistant object."""

    def __init__(
        self,
        db_url: DatabaseURL,
        stream_port: int = 8095,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        """
        Create an instance of MusicAssistant.

            db_url: Database connection string/url.
            stream_port: TCP port used for streaming audio.
            session: Optionally provide an aiohttp clientsession
        """

        self.loop: asyncio.AbstractEventLoop = None
        self.http_session: aiohttp.ClientSession = session
        self.http_session_provided = session is not None
        self.logger = logging.getLogger(__name__)

        self._listeners = []
        self._jobs = asyncio.Queue()

        # init core controllers
        self.database = Database(self, db_url)
        self.cache = Cache(self)
        self.metadata = MetaDataController(self)
        self.music = MusicController(self)
        self.players = PlayerController(self, stream_port)
        self._jobs_task: asyncio.Task = None

    async def setup(self) -> None:
        """Async setup of music assistant."""
        # initialize loop
        self.loop = asyncio.get_event_loop()
        util.DEFAULT_LOOP = self.loop
        # create shared aiohttp ClientSession
        if not self.http_session:
            self.http_session = aiohttp.ClientSession(
                loop=self.loop,
                connector=aiohttp.TCPConnector(enable_cleanup_closed=True, ssl=False),
            )
        # setup core controllers
        await self.cache.setup()
        await self.music.setup()
        await self.metadata.setup()
        await self.players.setup()
        self._jobs_task = create_task(self.__process_jobs())

    async def stop(self) -> None:
        """Stop running the music assistant server."""
        self.logger.info("Application shutdown")
        self.signal_event(EventType.SHUTDOWN)
        if self._jobs_task is not None:
            self._jobs_task.cancel()
        if self.http_session and not self.http_session_provided:
            await self.http_session.connector.close()
        self.http_session.detach()

    def signal_event(self, event_type: EventType, event_details: Any = None) -> None:
        """
        Signal (systemwide) event.

            :param event_msg: the eventmessage to signal
            :param event_details: optional details to send with the event.
        """
        for cb_func, event_filter in self._listeners:
            if not event_filter or event_type in event_filter:
                create_task(cb_func, event_type, event_details)

    def subscribe(
        self,
        cb_func: EventCallBackType,
        event_filter: Union[EventType, Tuple[EventType], None] = None,
    ) -> Callable:
        """
        Add callback to event listeners.

        Returns function to remove the listener.
            :param cb_func: callback function or coroutine
            :param event_filter: Optionally only listen for these events
        """
        if isinstance(event_filter, EventType):
            event_filter = (event_filter,)
        elif event_filter is None:
            event_filter = tuple()
        listener = (cb_func, event_filter)
        self._listeners.append(listener)

        def remove_listener():
            self._listeners.remove(listener)

        return remove_listener

    def add_job(self, job: Coroutine, name: Optional[str] = None) -> None:
        """Add job to be (slowly) processed in the background (one by one)."""
        if not name:
            name = job.__qualname__ or job.__name__
        self._jobs.put_nowait((name, job))

    async def __process_jobs(self):
        """Process jobs in the background."""
        while True:
            name, job = await self._jobs.get()
            time_start = time()
            self.logger.debug("Start processing job [%s].", name)
            try:
                # await job
                task = asyncio.create_task(job, name=name)
                await task
            except Exception as err:  # pylint: disable=broad-except
                self.logger.error(
                    "Job [%s] failed with error %s.", name, str(err), exc_info=err
                )
            else:
                duration = round(time() - time_start, 2)
                self.logger.info("Finished job [%s] in %s seconds.", name, duration)
