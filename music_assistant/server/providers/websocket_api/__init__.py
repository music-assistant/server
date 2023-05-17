"""Default Music Assistant Websocket API."""
from __future__ import annotations

import asyncio
import inspect
import logging
import weakref
from concurrent import futures
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Final

from aiohttp import WSMsgType, web

from music_assistant.common.models.api import (
    ChunkedResultMessage,
    CommandMessage,
    ErrorResultMessage,
    MessageType,
    SuccessResultMessage,
)
from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.errors import InvalidCommand
from music_assistant.common.models.event import MassEvent
from music_assistant.constants import ROOT_LOGGER_NAME
from music_assistant.server.helpers.api import APICommandHandler, parse_arguments
from music_assistant.server.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


DEBUG = False  # Set to True to enable very verbose logging of all incoming/outgoing messages
MAX_PENDING_MSG = 512
CANCELLATION_ERRORS: Final = (asyncio.CancelledError, futures.CancelledError)
LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.websocket_api")


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = WebsocketAPI(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return tuple()  # we do not have any config entries (yet)


class WebsocketAPI(PluginProvider):
    """Default Music Assistant Websocket API."""

    clients: weakref.WeakSet[WebsocketClientHandler] = weakref.WeakSet()

    async def handle_setup(self) -> None:
        """Handle async initialization of the plugin."""
        self.mass.webserver.register_route("/ws", self._handle_ws_client)

    async def _handle_ws_client(self, request: web.Request) -> web.WebSocketResponse:
        connection = WebsocketClientHandler(self.mass, request)
        try:
            self.clients.add(connection)
            return await connection.handle_client()
        finally:
            self.clients.remove(connection)

    async def unload(self) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        self.mass.webserver.unregister_route("/ws")
        for client in set(self.clients):
            await client.disconnect()


class WebSocketLogAdapter(logging.LoggerAdapter):
    """Add connection id to websocket log messages."""

    def process(self, msg: str, kwargs: Any) -> tuple[str, Any]:
        """Add connid to websocket log messages."""
        return f'[{self.extra["connid"]}] {msg}', kwargs


class WebsocketClientHandler:
    """Handle an active websocket client connection."""

    def __init__(self, mass: MusicAssistant, request: web.Request) -> None:
        """Initialize an active connection."""
        self.mass = mass
        self.request = request
        self.wsock = web.WebSocketResponse(heartbeat=55)
        self._to_write: asyncio.Queue = asyncio.Queue(maxsize=MAX_PENDING_MSG)
        self._handle_task: asyncio.Task | None = None
        self._writer_task: asyncio.Task | None = None
        self._logger = WebSocketLogAdapter(LOGGER, {"connid": id(self)})

    async def disconnect(self) -> None:
        """Disconnect client."""
        self._cancel()
        if self._writer_task is not None:
            await self._writer_task

    async def handle_client(self) -> web.WebSocketResponse:
        """Handle a websocket response."""
        # ruff: noqa: PLR0915
        request = self.request
        wsock = self.wsock
        try:
            async with asyncio.timeout(10):
                await wsock.prepare(request)
        except asyncio.TimeoutError:
            self._logger.warning("Timeout preparing request from %s", request.remote)
            return wsock

        self._logger.debug("Connection from %s", request.remote)
        self._handle_task = asyncio.current_task()
        self._writer_task = asyncio.create_task(self._writer())

        # send server(version) info when client connects
        self._send_message(self.mass.get_server_info())

        # forward all events to clients
        def handle_event(event: MassEvent) -> None:
            self._send_message(event)

        unsub_callback = self.mass.subscribe(handle_event)

        disconnect_warn = None

        try:
            while not wsock.closed:
                msg = await wsock.receive()

                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING):
                    break

                if msg.type != WSMsgType.TEXT:
                    disconnect_warn = "Received non-Text message."
                    break

                if DEBUG:
                    self._logger.debug("Received: %s", msg.data)

                try:
                    command_msg = CommandMessage.from_json(msg.data)
                except ValueError:
                    disconnect_warn = f"Received invalid JSON: {msg.data}"
                    break

                self._handle_command(command_msg)

        except asyncio.CancelledError:
            self._logger.debug("Connection closed by client")

        except Exception:  # pylint: disable=broad-except
            self._logger.exception("Unexpected error inside websocket API")

        finally:
            # Handle connection shutting down.
            unsub_callback()
            self._logger.debug("Unsubscribed from events")

            try:
                self._to_write.put_nowait(None)
                # Make sure all error messages are written before closing
                await self._writer_task
                await wsock.close()
            except asyncio.QueueFull:  # can be raised by put_nowait
                self._writer_task.cancel()

            finally:
                if disconnect_warn is None:
                    self._logger.debug("Disconnected")
                else:
                    self._logger.warning("Disconnected: %s", disconnect_warn)

        return wsock

    def _handle_command(self, msg: CommandMessage) -> None:
        """Handle an incoming command from the client."""
        self._logger.debug("Handling command %s", msg.command)

        # work out handler for the given path/command
        handler = self.mass.command_handlers.get(msg.command)

        if handler is None:
            self._send_message(
                ErrorResultMessage(
                    msg.message_id,
                    InvalidCommand.error_code,
                    f"Invalid command: {msg.command}",
                )
            )
            self._logger.warning("Invalid command: %s", msg.command)
            return

        # schedule task to handle the command
        asyncio.create_task(self._run_handler(handler, msg))

    async def _run_handler(self, handler: APICommandHandler, msg: CommandMessage) -> None:
        try:
            args = parse_arguments(handler.signature, handler.type_hints, msg.args)
            result = handler.target(**args)
            if inspect.isasyncgen(result):
                # async generator = send chunked response
                chunk_size = 100
                batch: list[Any] = []
                async for item in result:
                    batch.append(item)
                    if len(batch) == chunk_size:
                        self._send_message(ChunkedResultMessage(msg.message_id, batch))
                        batch = []
                # send last chunk
                self._send_message(ChunkedResultMessage(msg.message_id, batch, True))
                del batch
                return
            if asyncio.iscoroutine(result):
                result = await result
            self._send_message(SuccessResultMessage(msg.message_id, result))
        except Exception as err:  # pylint: disable=broad-except
            self._logger.exception("Error handling message: %s", msg)
            self._send_message(
                ErrorResultMessage(msg.message_id, getattr(err, "error_code", 999), str(err))
            )

    async def _writer(self) -> None:
        """Write outgoing messages."""
        # Exceptions if Socket disconnected or cancelled by connection handler
        with suppress(RuntimeError, ConnectionResetError, *CANCELLATION_ERRORS):
            while not self.wsock.closed:
                if (process := await self._to_write.get()) is None:
                    break

                if not isinstance(process, str):
                    message: str = process()
                else:
                    message = process
                if DEBUG:
                    self._logger.debug("Writing: %s", message)
                await self.wsock.send_str(message)

    def _send_message(self, message: MessageType) -> None:
        """Send a message to the client.

        Closes connection if the client is not reading the messages.

        Async friendly.
        """
        _message = message.to_json()

        try:
            self._to_write.put_nowait(_message)
        except asyncio.QueueFull:
            self._logger.error("Client exceeded max pending messages: %s", MAX_PENDING_MSG)

            self._cancel()

    def _cancel(self) -> None:
        """Cancel the connection."""
        if self._handle_task is not None:
            self._handle_task.cancel()
        if self._writer_task is not None:
            self._writer_task.cancel()
