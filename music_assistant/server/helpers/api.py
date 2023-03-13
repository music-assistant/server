"""Several helpers for the WebSockets API."""
from __future__ import annotations

import asyncio
import inspect
import logging
import weakref
from collections.abc import Callable, Coroutine
from concurrent import futures
from contextlib import suppress
from dataclasses import MISSING, dataclass
from datetime import datetime
from enum import Enum
from types import NoneType, UnionType
from typing import TYPE_CHECKING, Any, Final, TypeVar, Union, get_args, get_origin, get_type_hints

from aiohttp import WSMsgType, web

from music_assistant.common.models.api import (
    CommandMessage,
    ErrorResultMessage,
    MessageType,
    ServerInfoMessage,
    SuccessResultMessage,
)
from music_assistant.common.models.errors import InvalidCommand
from music_assistant.common.models.event import MassEvent
from music_assistant.constants import __version__

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant

MAX_PENDING_MSG = 512
CANCELLATION_ERRORS: Final = (asyncio.CancelledError, futures.CancelledError)
API_SCHEMA_VERSION = 1

LOGGER = logging.getLogger(__name__)
DEBUG = False  # Set to True to enable very verbose logging of all incoming/outgoing messages

_F = TypeVar("_F", bound=Callable[..., Any])


@dataclass
class APICommandHandler:
    """Model for an API command handler."""

    command: str
    signature: inspect.Signature
    type_hints: dict[str, Any]
    target: Callable[..., Coroutine[Any, Any, Any]]

    @classmethod
    def parse(
        cls, command: str, func: Callable[..., Coroutine[Any, Any, Any]]
    ) -> APICommandHandler:
        """Parse APICommandHandler by providing a function."""
        return APICommandHandler(
            command=command,
            signature=inspect.signature(func),
            type_hints=get_type_hints(func),
            target=func,
        )


def api_command(command: str) -> Callable[[_F], _F]:
    """Decorate a function as API route/command."""

    def decorate(func: _F) -> _F:
        func.api_cmd = command  # type: ignore[attr-defined]
        return func

    return decorate


def parse_arguments(
    func_sig: inspect.Signature,
    func_types: dict[str, Any],
    args: dict | None,
    strict: bool = False,
) -> dict[str, Any]:
    """Parse (and convert) incoming arguments to correct types."""
    if args is None:
        args = {}
    final_args = {}
    # ignore extra args if not strict
    if strict:
        for key, value in args.items():
            if key not in func_sig.parameters:
                raise KeyError("Invalid parameter: '%s'" % key)
    # parse arguments to correct type
    for name, param in func_sig.parameters.items():
        value = args.get(name)
        default = MISSING if param.default is inspect.Parameter.empty else param.default
        final_args[name] = parse_value(name, value, func_types[name], default)
    return final_args


def mount_websocket_api(mass: MusicAssistant, path: str) -> None:
    """Mount the websocket endpoint."""
    clients: weakref.WeakSet[WebsocketClientHandler] = weakref.WeakSet()

    async def _handle_ws(request: web.Request) -> web.WebSocketResponse:
        connection = WebsocketClientHandler(mass, request)
        try:
            clients.add(connection)
            return await connection.handle_client()
        finally:
            clients.remove(connection)

    async def _handle_shutdown(app: web.Application) -> None:  # noqa: ARG001
        for client in set(clients):
            await client.disconnect()

    mass.webapp.on_shutdown.append(_handle_shutdown)
    mass.webapp.router.add_route("GET", path, _handle_ws)


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
        self._send_message(
            ServerInfoMessage(server_version=__version__, schema_version=API_SCHEMA_VERSION)
        )

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


def parse_utc_timestamp(datetime_string: str) -> datetime:
    """Parse datetime from string."""
    return datetime.fromisoformat(datetime_string.replace("Z", "+00:00"))


def parse_value(name: str, value: Any, value_type: Any, default: Any = MISSING) -> Any:
    """Try to parse a value from raw (json) data and type annotations."""
    if isinstance(value, dict) and hasattr(value_type, "from_dict"):
        if "media_type" in value and value["media_type"] != value_type.media_type:
            raise ValueError("Invalid MediaType")
        return value_type.from_dict(value)

    if value is None and not isinstance(default, type(MISSING)):
        return default
    if value is None and value_type is NoneType:
        return None
    origin = get_origin(value_type)
    if origin is list:
        return [
            parse_value(name, subvalue, get_args(value_type)[0])
            for subvalue in value
            if subvalue is not None
        ]
    elif origin is dict:
        subkey_type = get_args(value_type)[0]
        subvalue_type = get_args(value_type)[1]
        return {
            parse_value(subkey, subkey, subkey_type): parse_value(
                f"{subkey}.value", subvalue, subvalue_type
            )
            for subkey, subvalue in value.items()
        }
    elif origin is Union or origin is UnionType:
        # try all possible types
        sub_value_types = get_args(value_type)
        for sub_arg_type in sub_value_types:
            if value is NoneType and sub_arg_type is NoneType:
                return value
            # try them all until one succeeds
            try:
                return parse_value(name, value, sub_arg_type)
            except (KeyError, TypeError, ValueError):
                pass
        # if we get to this point, all possibilities failed
        # find out if we should raise or log this
        err = (
            f"Value {value} of type {type(value)} is invalid for {name}, "
            f"expected value of type {value_type}"
        )
        if NoneType not in sub_value_types:
            # raise exception, we have no idea how to handle this value
            raise TypeError(err)
        # failed to parse the (sub) value but None allowed, log only
        logging.getLogger(__name__).warn(err)
        return None
    elif origin is type:
        return eval(value)
    if value_type is Any:
        return value
    if value is None and value_type is not NoneType:
        raise KeyError(f"`{name}` of type `{value_type}` is required.")

    try:
        if issubclass(value_type, Enum):  # type: ignore[arg-type]
            return value_type(value)  # type: ignore[operator]
        if issubclass(value_type, datetime):  # type: ignore[arg-type]
            return parse_utc_timestamp(value)
    except TypeError:
        # happens if value_type is not a class
        pass

    if value_type is float and isinstance(value, int):
        return float(value)
    if value_type is int and isinstance(value, str) and value.isnumeric():
        return int(value)
    if not isinstance(value, value_type):  # type: ignore[arg-type]
        raise TypeError(
            f"Value {value} of type {type(value)} is invalid for {name}, "
            f"expected value of type {value_type}"
        )
    return value
