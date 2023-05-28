"""Controller that manages the builtin webserver(s) needed for the music Assistant server."""
from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING

from aiohttp import web
from music_assistant_frontend import where as locate_frontend

from music_assistant.common.helpers.util import select_free_port
from music_assistant.constants import ROOT_LOGGER_NAME

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant


LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.web")


class WebserverController:
    """Controller to stream audio to players."""

    port: int
    webapp: web.Application

    def __init__(self, mass: MusicAssistant):
        """Initialize instance."""
        self.mass = mass
        self._apprunner: web.AppRunner
        self._tcp: web.TCPSite
        self._route_handlers: dict[str, Callable] = {}

    @property
    def base_url(self) -> str:
        """Return the (web)server's base url."""
        return f"http://{self.mass.base_ip}:{self.port}"

    async def setup(self) -> None:
        """Async initialize of module."""
        self.webapp = web.Application()
        self.port = await select_free_port(8095, 9200)
        LOGGER.info("Starting webserver on port %s", self.port)
        self._apprunner = web.AppRunner(self.webapp, access_log=None)
        # setup stream paths
        self.webapp.router.add_get("/stream/preview", self.mass.streams.serve_preview)
        self.webapp.router.add_get(
            "/stream/{player_id}/{queue_item_id}/{stream_id}.{fmt}",
            self.mass.streams.serve_queue_stream,
        )

        # setup frontend
        frontend_dir = locate_frontend()
        for filename in next(os.walk(frontend_dir))[2]:
            if filename.endswith(".py"):
                continue
            filepath = os.path.join(frontend_dir, filename)
            handler = partial(self.serve_static, filepath)
            self.webapp.router.add_get(f"/{filename}", handler)
        # add assets subdir as static
        self.webapp.router.add_static(
            "/assets", os.path.join(frontend_dir, "assets"), name="assets"
        )
        # add index
        index_path = os.path.join(frontend_dir, "index.html")
        handler = partial(self.serve_static, index_path)
        self.webapp.router.add_get("/", handler)
        # add info
        self.webapp.router.add_get("/info", self._handle_server_info)
        # register catch-all route to handle our custom paths
        self.webapp.router.add_route("*", "/{tail:.*}", self._handle_catch_all)
        await self._apprunner.setup()
        # set host to None to bind to all addresses on both IPv4 and IPv6
        host = None
        self._tcp_site = web.TCPSite(self._apprunner, host=host, port=self.port)
        await self._tcp_site.start()

    async def close(self) -> None:
        """Cleanup on exit."""
        # stop/clean webserver
        await self._tcp_site.stop()
        await self._apprunner.cleanup()
        await self.webapp.shutdown()
        await self.webapp.cleanup()

    def register_route(self, path: str, handler: Awaitable, method: str = "*") -> Callable:
        """Register a route on the (main) webserver, returns handler to unregister."""
        key = f"{method}.{path}"
        if key in self._route_handlers:
            raise RuntimeError(f"Route {path} already registered.")
        self._route_handlers[key] = handler

        def _remove():
            return self._route_handlers.pop(key)

        return _remove

    def unregister_route(self, path: str, method: str = "*") -> None:
        """Unregister a route from the (main) webserver."""
        key = f"{method}.{path}"
        self._route_handlers.pop(key)

    async def serve_static(self, file_path: str, _request: web.Request) -> web.FileResponse:
        """Serve file response."""
        headers = {"Cache-Control": "no-cache"}
        return web.FileResponse(file_path, headers=headers)

    async def _handle_catch_all(self, request: web.Request) -> web.Response:
        """Redirect request to correct destination."""
        # find handler for the request
        for key in (f"{request.method}.{request.path}", f"*.{request.path}"):
            if handler := self._route_handlers.get(key):
                return await handler(request)
        # deny all other requests
        LOGGER.debug(
            "Received unhandled %s request to %s from %s\nheaders: %s\n",
            request.method,
            request.path,
            request.remote,
            request.headers,
        )
        return web.Response(status=404)

    async def _handle_server_info(self, request: web.Request) -> web.Response:  # noqa: ARG002
        """Handle request for server info."""
        return web.json_response(self.mass.get_server_info().to_dict())
