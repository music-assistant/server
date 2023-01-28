"""Main Music Assistant class."""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from functools import partial
from time import time
from types import TracebackType
from typing import (
    Any,
    Callable,
    Coroutine,
    Deque,
    List,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
)
from uuid import uuid4

from aiohttp import ClientSession, TCPConnector, web

from music_assistant.common.models.enums import EventType, JobStatus
from music_assistant.common.models.event import MassEvent
from music_assistant.constants import (
    CONF_WEB_HOST,
    CONF_WEB_PORT,
    DEFAULT_PORT,
    DEFAULT_HOST,
    ROOT_LOGGER_NAME,
)
from music_assistant.server.controllers.cache import CacheController
from music_assistant.server.controllers.database import DatabaseController
from music_assistant.server.controllers.metadata.metadata import MetaDataController
from music_assistant.server.controllers.music import MusicController
from music_assistant.server.controllers.players import PlayerController
from music_assistant.server.controllers.config import ConfigController
from music_assistant.server.controllers.streams import StreamsController
from music_assistant.server.helpers.api import APICommandHandler, mount_websocket

EventCallBackType = Callable[[EventType, Any], None]
EventSubscriptionType = Tuple[
    EventCallBackType, Optional[Tuple[EventType]], Optional[Tuple[str]]
]

LOGGER = logging.getLogger(ROOT_LOGGER_NAME)


class MusicAssistant:
    """Main MusicAssistant (Server) object."""

    loop: asyncio.AbstractEventLoop | None = None
    http_session: ClientSession | None = None
    _web_apprunner: web.AppRunner | None = None
    _web_tcp: web.TCPSite | None = None

    def __init__(self, storage_path: str) -> None:
        """
        Create an instance of the MusicAssistant Server."""
        self.storage_path = storage_path
        self._subscribers: Set[EventCallBackType] = set()

        # init core controllers
        self.config = ConfigController(self)
        self.database = DatabaseController(self)
        self.cache = CacheController(self)
        self.metadata = MetaDataController(self)
        self.music = MusicController(self)
        self.players = PlayerController(self)
        self.streams = StreamsController(self)
        self._tracked_tasks: List[asyncio.Task] = []
        self.closed = False
        self.loop: asyncio.AbstractEventLoop | None = None
        # we dynamically register command handlers
        self.webapp = web.Application()
        self.command_handlers: dict[str, APICommandHandler] = {}
        self._register_api_commands()

    async def start(self) -> None:
        """Start running the Music Assistant server."""
        self.loop = asyncio.get_running_loop()
        # create shared aiohttp ClientSession
        self.http_session = ClientSession(
            loop=self.loop,
            connector=TCPConnector(ssl=False),
        )
        # setup core controllers
        await self.database.setup()
        await self.config.setup()
        await self.cache.setup()
        await self.music.setup()
        await self.metadata.setup()
        await self.players.setup()
        await self.streams.setup()
        # setup web server
        host = self.config.get(CONF_WEB_HOST, DEFAULT_HOST)
        port = self.config.get(CONF_WEB_PORT, DEFAULT_PORT)
        if host == "0.0.0.0":
            # set host to None to bind to all addresses on both IPv4 and IPv6
            host = None
        mount_websocket(self, "/ws")
        self._web_apprunner = web.AppRunner(self.webapp, access_log=None)
        await self._web_apprunner.setup()
        self._http = web.TCPSite(self._web_apprunner, host=host, port=port)
        await self._http.start()

    async def stop(self) -> None:
        """Stop running the music assistant server."""
        LOGGER.info("Stop called, cleaning up...")
        await self.players.cleanup()
        # cancel all running tasks
        for task in self._tracked_tasks:
            task.cancel()
        self.signal_event(EventType.SHUTDOWN)
        await self.database.close()
        self.closed = True
        if self.http_session:
            await self.http_session.close()

    def signal_event(
        self,
        event: EventType,
        object_id: str | None = None,
        data: Optional[Any] = None,
    ) -> None:
        """Signal event to subscribers."""
        if self.closed:
            return

        if LOGGER.isEnabledFor(logging.DEBUG):
            if event != EventType.QUEUE_TIME_UPDATED:
                # do not log queue time updated events because that is too chatty
                LOGGER.getChild("event").debug("%s %s", event.value, object_id or "")

        event_obj = MassEvent(event=event, object_id=object_id, data=data)
        for cb_func, event_filter, id_filter in self._subscribers:
            if not (event_filter is None or event in event_filter):
                continue
            if not (id_filter is None or object_id in id_filter):
                continue
            if asyncio.iscoroutinefunction(cb_func):
                asyncio.run_coroutine_threadsafe(cb_func(event_obj), self.loop)
            else:
                self.loop.call_soon_threadsafe(cb_func, event_obj)

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
        self._subscribers.add(listener)

        def remove_listener():
            self._subscribers.remove(listener)

        return remove_listener

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

    def register_api_command(
        self,
        command: str,
        handler: Callable,
    ) -> None:
        """Dynamically register a command on the API."""
        assert command not in self.command_handlers, "Command already registered"
        self.command_handlers[command] = APICommandHandler.parse(command, handler)

    def _register_api_commands(self) -> None:
        """Register all methods decorated as api_command within a class(instance)."""
        for cls in (self, self.config, self.metadata, self.music, self.players):
            for attr_name in dir(cls):
                if attr_name.startswith("__"):
                    continue
                obj = getattr(cls, attr_name)
                if hasattr(obj, "api_cmd"):
                    # method is decorated with our api decorator
                    self.register_api_command(obj.api_cmd, obj)

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
