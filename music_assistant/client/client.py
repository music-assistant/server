"""Music Assistant Client: Manage a Music Assistant server remotely."""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from music_assistant.client.exceptions import (
    ConnectionClosed,
    InvalidServerVersion,
    InvalidState,
)
from music_assistant.common.models.api import (
    ChunkedResultMessage,
    CommandMessage,
    ErrorResultMessage,
    EventMessage,
    ResultMessageBase,
    ServerInfoMessage,
    SuccessResultMessage,
    parse_message,
)
from music_assistant.common.models.enums import EventType
from music_assistant.common.models.errors import ERROR_MAP
from music_assistant.common.models.event import MassEvent
from music_assistant.constants import API_SCHEMA_VERSION

from .connection import WebsocketsConnection
from .music import Music
from .players import Players

if TYPE_CHECKING:
    from types import TracebackType

    from aiohttp import ClientSession

    from music_assistant.common.models.media_items import MediaItemImage

EventCallBackType = Callable[[MassEvent], None]
EventSubscriptionType = tuple[
    EventCallBackType, tuple[EventType, ...] | None, tuple[str, ...] | None
]


class MusicAssistantClient:
    """Manage a Music Assistant server remotely."""

    def __init__(self, server_url: str, aiohttp_session: ClientSession | None) -> None:
        """Initialize the Music Assistant client."""
        self.server_url = server_url
        self.connection = WebsocketsConnection(server_url, aiohttp_session)
        self.logger = logging.getLogger(__package__)
        self._result_futures: dict[str, asyncio.Future] = {}
        self._subscribers: list[EventSubscriptionType] = []
        self._stop_called: bool = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._players = Players(self)
        self._music = Music(self)
        # below items are retrieved after connect
        self._server_info: ServerInfoMessage | None = None

    @property
    def server_info(self) -> ServerInfoMessage | None:
        """Return info of the server we're currently connected to."""
        return self._server_info

    @property
    def players(self) -> Players:
        """Return Players handler."""
        return self._players

    @property
    def music(self) -> Music:
        """Return Music handler."""
        return self._music

    def get_image_url(self, image: MediaItemImage) -> str:
        """Get (proxied) URL for MediaItemImage."""
        if image.provider != "url":
            # return imageproxy url for images that need to be resolved
            # the original path is double encoded
            encoded_url = urllib.parse.quote(urllib.parse.quote(image.path))
            return f"{self.server_info.base_url}/imageproxy?path={encoded_url}&provider={image.provider}"  # noqa: E501
        return image.path

    def subscribe(
        self,
        cb_func: EventCallBackType,
        event_filter: EventType | tuple[EventType] | None = None,
        id_filter: str | tuple[str] | None = None,
    ) -> Callable:
        """Add callback to event listeners.

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
        self._subscribers.append(listener)

        def remove_listener() -> None:
            self._subscribers.remove(listener)

        return remove_listener

    async def connect(self) -> None:
        """Connect to the remote Music Assistant Server."""
        self._loop = asyncio.get_running_loop()
        if self.connection.connected:
            # already connected
            return
        # NOTE: connect will raise when connecting failed
        result = await self.connection.connect()
        info = ServerInfoMessage.from_dict(result)

        # basic check for server schema version compatibility
        if info.min_supported_schema_version > API_SCHEMA_VERSION:
            # our schema version is too low and can't be handled by the server anymore.
            await self.connection.disconnect()
            msg = (
                f"Schema version is incompatible: {info.schema_version}, "
                f"the server requires at least {info.min_supported_schema_version} "
                " - update the Music Assistant client to a more "
                "recent version or downgrade the server."
            )
            raise InvalidServerVersion(msg)

        self._server_info = info

        self.logger.info(
            "Connected to Music Assistant Server %s using %s, Version %s, Schema Version %s",
            info.server_id,
            self.connection.__class__.__name__,
            info.server_version,
            info.schema_version,
        )

    async def send_command(
        self,
        command: str,
        require_schema: int | None = None,
        **kwargs: Any,
    ) -> Any:
        """Send a command and get a response."""
        if not self.connection.connected or not self._loop:
            msg = "Not connected"
            raise InvalidState(msg)

        if (
            require_schema is not None
            and self.server_info is not None
            and require_schema > self.server_info.schema_version
        ):
            msg = (
                "Command not available due to incompatible server version. Update the Music "
                f"Assistant Server to a version that supports at least api schema {require_schema}."
            )
            raise InvalidServerVersion(msg)

        command_message = CommandMessage(
            message_id=uuid.uuid4().hex,
            command=command,
            args=kwargs,
        )
        future: asyncio.Future[Any] = self._loop.create_future()
        self._result_futures[command_message.message_id] = future
        await self.connection.send_message(command_message.to_dict())
        try:
            return await future
        finally:
            self._result_futures.pop(command_message.message_id)

    async def send_command_no_wait(
        self,
        command: str,
        require_schema: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Send a command without waiting for the response."""
        if not self.server_info:
            msg = "Not connected"
            raise InvalidState(msg)

        if require_schema is not None and require_schema > self.server_info.schema_version:
            msg = (
                "Command not available due to incompatible server version. Update the Music "
                f"Assistant Server to a version that supports at least api schema {require_schema}."
            )
            raise InvalidServerVersion(msg)
        command_message = CommandMessage(
            message_id=uuid.uuid4().hex,
            command=command,
            args=kwargs,
        )
        await self.connection.send_message(command_message.to_dict())

    async def start_listening(self, init_ready: asyncio.Event | None = None) -> None:
        """Connect (if needed) and start listening to incoming messages from the server."""
        await self.connect()

        # fetch initial state
        # we do this in a separate task to not block reading messages
        async def fetch_initial_state() -> None:
            await self._players.fetch_state()

            if init_ready is not None:
                init_ready.set()

        asyncio.create_task(fetch_initial_state())

        try:
            # keep reading incoming messages
            while not self._stop_called:
                msg = await self.connection.receive_message()
                self._handle_incoming_message(msg)
        except ConnectionClosed:
            pass
        finally:
            await self.disconnect()

    async def disconnect(self) -> None:
        """Disconnect the client and cleanup."""
        self._stop_called = True
        # cancel all command-tasks awaiting a result
        for future in self._result_futures.values():
            future.cancel()
        await self.connection.disconnect()

    def _handle_incoming_message(self, raw: dict[str, Any]) -> None:
        """
        Handle incoming message.

        Run all async tasks in a wrapper to log appropriately.
        """
        msg = parse_message(raw)
        # handle result message
        if isinstance(msg, ResultMessageBase):
            future = self._result_futures.get(msg.message_id)

            if future is None:
                # no listener for this result
                return
            if isinstance(msg, ChunkedResultMessage):
                # handle chunked response (for very large objects)
                if not hasattr(future, "intermediate_result"):
                    future.intermediate_result = []
                future.intermediate_result += msg.result
                if msg.is_last_chunk:
                    future.set_result(future.intermediate_result)
                return
            if isinstance(msg, SuccessResultMessage):
                future.set_result(msg.result)
                return
            if isinstance(msg, ErrorResultMessage):
                exc = ERROR_MAP[msg.error_code]
                future.set_exception(exc(msg.details))
                return

        # handle EventMessage
        if isinstance(msg, EventMessage):
            self.logger.debug("Received event: %s", msg)
            self._handle_event(msg)
            return

        # Log anything we can't handle here
        self.logger.debug(
            "Received message with unknown type '%s': %s",
            type(msg),
            msg,
        )

    def _handle_event(self, event: MassEvent) -> None:
        """Forward event to subscribers."""
        if self._stop_called:
            return

        for cb_func, event_filter, id_filter in self._subscribers:
            if not (event_filter is None or event.event in event_filter):
                continue
            if not (id_filter is None or event.object_id in id_filter):
                continue
            if asyncio.iscoroutinefunction(cb_func):
                asyncio.run_coroutine_threadsafe(cb_func(event), self._loop)
            else:
                self._loop.call_soon_threadsafe(cb_func, event)

    async def __aenter__(self) -> MusicAssistantClient:
        """Initialize and connect the connection to the Music Assistant Server."""
        await self.connect()
        return self

    async def __aexit__(
        self, exc_type: Exception, exc_value: str, traceback: TracebackType
    ) -> None:
        """Disconnect from the server and exit."""
        await self.disconnect()

    def __repr__(self) -> str:
        """Return the representation."""
        conn_type = self.connection.__class__.__name__
        prefix = "" if self.connection.connected else "not "
        return f"{type(self).__name__}(connection={conn_type}, {prefix}connected)"
