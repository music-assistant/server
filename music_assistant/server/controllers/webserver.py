"""
Controller that manages the builtin webserver that hosts the api and frontend.

Unlike the streamserver (which is as simple and unprotected as possible),
this webserver allows for more fine grained configuration to better secure it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import urllib.parse
from concurrent import futures
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from aiohttp import WSMsgType, web
from music_assistant_frontend import where as locate_frontend

from music_assistant.common.helpers.util import get_ip, select_free_port
from music_assistant.common.models.api import (
    ChunkedResultMessage,
    CommandMessage,
    ErrorResultMessage,
    MessageType,
    SuccessResultMessage,
)
from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant.common.models.enums import ConfigEntryType
from music_assistant.common.models.errors import InvalidCommand
from music_assistant.constants import CONF_BIND_IP, CONF_BIND_PORT
from music_assistant.server.helpers.api import APICommandHandler, parse_arguments
from music_assistant.server.helpers.audio import get_preview_stream
from music_assistant.server.helpers.util import get_ips
from music_assistant.server.helpers.webserver import Webserver
from music_assistant.server.models.core_controller import CoreController

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from music_assistant.common.models.config_entries import ConfigValueType, CoreConfig
    from music_assistant.common.models.event import MassEvent

DEFAULT_SERVER_PORT = 8095
CONF_BASE_URL = "base_url"
CONF_EXPOSE_SERVER = "expose_server"
DEBUG = False  # Set to True to enable very verbose logging of all incoming/outgoing messages
MAX_PENDING_MSG = 512
CANCELLATION_ERRORS: Final = (asyncio.CancelledError, futures.CancelledError)


class WebserverController(CoreController):
    """Core Controller that manages the builtin webserver that hosts the api and frontend."""

    domain: str = "webserver"

    def __init__(self, *args, **kwargs) -> None:
        """Initialize instance."""
        super().__init__(*args, **kwargs)
        self._server = Webserver(self.logger, enable_dynamic_routes=False)
        self.clients: set[WebsocketClientHandler] = set()
        self.manifest.name = "Web Server (frontend and api)"
        self.manifest.description = (
            "The built-in webserver that hosts the Music Assistant Websockets API and frontend"
        )
        self.manifest.icon = "web-box"

    @property
    def base_url(self) -> str:
        """Return the base_url for the streamserver."""
        return self._server.base_url

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        default_publish_ip = await get_ip()
        if self.mass.running_as_hass_addon:
            return (
                ConfigEntry(
                    key=CONF_EXPOSE_SERVER,
                    type=ConfigEntryType.BOOLEAN,
                    # hardcoded/static value
                    default_value=False,
                    label="Expose the webserver (port 8095)",
                    description="By default the Music Assistant webserver "
                    "(serving the API and frontend), runs on a protected internal network only "
                    "and you can securely access the webinterface using "
                    "Home Assistant's ingress service from the sidebar menu.\n\n"
                    "By enabling this option you also allow direct access to the webserver "
                    "from your local network, meaning you can navigate to "
                    f"http://{default_publish_ip}:8095 to access the webinterface. \n\n"
                    "Use this option on your own risk and never expose this port "
                    "directly to the internet.",
                ),
            )

        # HA supervisor not present: user is responsible for securing the webserver
        # we give the tools to do so by presenting config options
        all_ips = await get_ips()
        default_port = await select_free_port(8095, 9200)
        default_base_url = f"http://{default_publish_ip}:{default_port}"
        return (
            ConfigEntry(
                key=CONF_BASE_URL,
                type=ConfigEntryType.STRING,
                default_value=default_base_url,
                label="Base URL",
                description="The (base) URL to reach this webserver in the network. \n"
                "Override this in advanced scenarios where for example you're running "
                "the webserver behind a reverse proxy.",
            ),
            ConfigEntry(
                key=CONF_BIND_PORT,
                type=ConfigEntryType.INTEGER,
                default_value=default_port,
                label="TCP Port",
                description="The TCP port to run the webserver.",
            ),
            ConfigEntry(
                key=CONF_BIND_IP,
                type=ConfigEntryType.STRING,
                default_value="0.0.0.0",
                options=(ConfigValueOption(x, x) for x in {"0.0.0.0", *all_ips}),
                label="Bind to IP/interface",
                description="Start the (web)server on this specific interface. \n"
                "Use 0.0.0.0 to bind to all interfaces. \n"
                "Set this address for example to a docker-internal network, "
                "to enhance security and protect outside access to the webinterface and API. \n\n"
                "This is an advanced setting that should normally "
                "not be adjusted in regular setups.",
                advanced=True,
            ),
        )

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        # work out all routes
        routes: list[tuple[str, str, Awaitable]] = []
        # frontend routes
        frontend_dir = locate_frontend()
        for filename in next(os.walk(frontend_dir))[2]:
            if filename.endswith(".py"):
                continue
            filepath = os.path.join(frontend_dir, filename)
            handler = partial(self._server.serve_static, filepath)
            routes.append(("GET", f"/{filename}", handler))
        # add index
        index_path = os.path.join(frontend_dir, "index.html")
        handler = partial(self._server.serve_static, index_path)
        routes.append(("GET", "/", handler))
        # add info
        routes.append(("GET", "/info", self._handle_server_info))
        # add logging
        routes.append(("GET", "/log", self._handle_application_log))
        # add websocket api
        routes.append(("GET", "/ws", self._handle_ws_client))
        # also host the image proxy on the webserver
        routes.append(("GET", "/imageproxy", self.mass.metadata.handle_imageproxy))
        # also host the audio preview service
        routes.append(("GET", "/preview", self.serve_preview_stream))
        # start the webserver
        if self.mass.running_as_hass_addon:
            # if we're running on the HA supervisor the webserver is secured by HA ingress
            # we only start the webserver on the internal docker network and ingress connects
            # to that internally and exposes the webUI securely
            # if a user also wants to expose a the webserver non securely on his internal
            # network he/she should explicitly do so (and know the risks)
            default_publish_ip = await get_ip()
            self.publish_port = DEFAULT_SERVER_PORT
            if config.get_value(CONF_EXPOSE_SERVER):
                bind_ip = "0.0.0.0"
                self.publish_ip = default_publish_ip
            else:
                # use internal ("172.30.32.) IP
                self.publish_ip = bind_ip = next(
                    (x for x in await get_ips() if x.startswith("172.30.32.")), default_publish_ip
                )
            base_url = f"http://{self.publish_ip}:{self.publish_port}"
        else:
            base_url = config.get_value(CONF_BASE_URL)
            self.publish_port = config.get_value(CONF_BIND_PORT)
            self.publish_ip = bind_ip = config.get_value(CONF_BIND_IP)
        await self._server.setup(
            bind_ip=bind_ip,
            bind_port=self.publish_port,
            base_url=base_url,
            static_routes=routes,
            # add assets subdir as static_content
            static_content=("/assets", os.path.join(frontend_dir, "assets"), "assets"),
        )

    async def close(self) -> None:
        """Cleanup on exit."""
        for client in set(self.clients):
            await client.disconnect()
        await self._server.close()

    async def serve_preview_stream(self, request: web.Request):
        """Serve short preview sample."""
        provider_instance_id_or_domain = request.query["provider"]
        item_id = urllib.parse.unquote(request.query["item_id"])
        resp = web.StreamResponse(status=200, reason="OK", headers={"Content-Type": "audio/mp3"})
        await resp.prepare(request)
        async for chunk in get_preview_stream(self.mass, provider_instance_id_or_domain, item_id):
            await resp.write(chunk)
        return resp

    async def _handle_server_info(self, request: web.Request) -> web.Response:
        """Handle request for server info."""
        return web.json_response(self.mass.get_server_info().to_dict())

    async def _handle_ws_client(self, request: web.Request) -> web.WebSocketResponse:
        connection = WebsocketClientHandler(self, request)
        try:
            self.clients.add(connection)
            return await connection.handle_client()
        finally:
            self.clients.remove(connection)

    async def _handle_application_log(self, request: web.Request) -> web.Response:
        """Handle request to get the application log."""
        log_data = await self.mass.get_application_log()
        return web.Response(text=log_data, content_type="text/text")


class WebSocketLogAdapter(logging.LoggerAdapter):
    """Add connection id to websocket log messages."""

    def process(self, msg: str, kwargs: Any) -> tuple[str, Any]:
        """Add connid to websocket log messages."""
        return f'[{self.extra["connid"]}] {msg}', kwargs


class WebsocketClientHandler:
    """Handle an active websocket client connection."""

    def __init__(self, webserver: WebserverController, request: web.Request) -> None:
        """Initialize an active connection."""
        self.mass = webserver.mass
        self.request = request
        self.wsock = web.WebSocketResponse(heartbeat=55)
        self._to_write: asyncio.Queue = asyncio.Queue(maxsize=MAX_PENDING_MSG)
        self._handle_task: asyncio.Task | None = None
        self._writer_task: asyncio.Task | None = None
        self.log_level = webserver.log_level
        self._logger = WebSocketLogAdapter(webserver.logger, {"connid": id(self)})

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
        except TimeoutError:
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

                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
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
            if self.log_level == "VERBOSE":
                self._logger.exception("Error handling message: %s", msg)
            else:
                self._logger.error("Error handling message: %s: %s", msg.command, str(err))
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
