"""Main Music Assistant class."""
from __future__ import annotations

import asyncio
import logging
from typing import  Callable, Tuple

import aiohttp
from databases import DatabaseURL
from zeroconf import InterfaceChoice, Zeroconf
from music_assistant.helpers.typing import EventCallBackType, EventDetails

import music_assistant.helpers.util as util
from music_assistant.config import ConfigManager
from music_assistant.constants import EventType
from music_assistant.database import DatabaseController
from music_assistant.helpers.cache import Cache
from music_assistant.helpers.util import callback, create_task
from music_assistant.metadata import MetaDataController
from music_assistant.music import MusicController
from music_assistant.players import PlayerController
from music_assistant.tasks import TaskManager


class MusicAssistant:
    """Main MusicAssistant object."""

    def __init__(self, db_url: DatabaseURL, stream_port: int = 8095) -> None:
        """
        Create an instance of MusicAssistant.

            :param db_url: Database connection string/url.
            :param stream_port: TCP port used for streaming audio.
        """

        self.loop: asyncio.AbstractEventLoop = None
        self.http_session: aiohttp.ClientSession = None
        self.zeroconf = Zeroconf(interfaces=InterfaceChoice.All)
        self.logger = logging.getLogger(__file__)

        self._listeners = []

        # init core controllers
        self.config = ConfigManager(self)
        self.tasks = TaskManager(self)
        self.database = DatabaseController(self, db_url)
        self.cache = Cache(self)
        self.metadata = MetaDataController(self)
        self.music = MusicController(self)
        self.players = PlayerController(self)

    async def setup(self) -> None:
        """Async setup of music assistant."""
        # initialize loop
        self.loop = asyncio.get_event_loop()
        util.DEFAULT_LOOP = self.loop
        # create shared aiohttp ClientSession
        self.http_session = aiohttp.ClientSession(
            loop=self.loop,
            connector=aiohttp.TCPConnector(enable_cleanup_closed=True, ssl=False),
        )
        # run migrations if needed
        await self.database.setup()
        await self.tasks.setup()
        await self.config.setup()
        await self.cache.setup()
        await self.music.setup()
        await self.players.setup()

    async def stop(self) -> None:
        """Stop running the music assistant server."""
        self.logger.info("Application shutdown")
        self.signal_event(EventType.SHUTDOWN)
        await self.http_session.connector.close()
        self.http_session.detach()

    @callback
    def signal_event(
        self, event_msg: EventType, event_details: EventDetails = None
    ) -> None:
        """
        Signal (systemwide) event.

            :param event_msg: the eventmessage to signal
            :param event_details: optional details to send with the event.
        """
        if self.loop.get_debug():
            self.logger.debug("%s: %s", event_msg, str(event_details))
        for cb_func, event_filter in self._listeners:
            if not event_filter or event_msg in event_filter:
                create_task(cb_func, event_msg, event_details)

    @callback
    def subscribe(
        self,
        cb_func: EventCallBackType,
        event_filter: EventType | Tuple[EventType] | None = None,
    ) -> Callable:
        """
        Add callback to event listeners.

        Returns function to remove the listener.
            :param cb_func: callback function or coroutine
            :param event_filter: Optionally only listen for these events
        """
        listener = (cb_func, event_filter)
        self._listeners.append(listener)

        def remove_listener():
            self._listeners.remove(listener)

        return remove_listener
