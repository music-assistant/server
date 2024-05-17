"""Base Webserver logic for an HTTPServer that can handle dynamic routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from aiohttp import web

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable

MAX_CLIENT_SIZE: Final = 1024**2 * 16
MAX_LINE_SIZE: Final = 24570


class Webserver:
    """Base Webserver logic for an HTTPServer that can handle dynamic routes."""

    def __init__(
        self,
        logger: logging.Logger,
        enable_dynamic_routes: bool = False,
    ) -> None:
        """Initialize instance."""
        self.logger = logger
        # the below gets initialized in async setup
        self._apprunner: web.AppRunner | None = None
        self._webapp: web.Application | None = None
        self._tcp_site: web.TCPSite | None = None
        self._static_routes: list[tuple[str, str, Awaitable]] | None = None
        self._dynamic_routes: dict[str, Callable] | None = {} if enable_dynamic_routes else None
        self._bind_port: int | None = None

    async def setup(
        self,
        bind_ip: str | None,
        bind_port: int,
        base_url: str,
        static_routes: list[tuple[str, str, Awaitable]] | None = None,
        static_content: tuple[str, str, str] | None = None,
    ) -> None:
        """Async initialize of module."""
        self._base_url = base_url[:-1] if base_url.endswith("/") else base_url
        self._bind_port = bind_port
        self._static_routes = static_routes
        self._webapp = web.Application(
            logger=self.logger,
            client_max_size=MAX_CLIENT_SIZE,
            handler_args={
                "max_line_size": MAX_LINE_SIZE,
                "max_field_size": MAX_LINE_SIZE,
            },
        )
        self.logger.info("Starting server on  %s:%s - base url: %s", bind_ip, bind_port, base_url)
        self._apprunner = web.AppRunner(self._webapp, access_log=None, shutdown_timeout=10)
        # add static routes
        if self._static_routes:
            for method, path, handler in self._static_routes:
                self._webapp.router.add_route(method, path, handler)
        if static_content:
            self._webapp.router.add_static(
                static_content[0], static_content[1], name=static_content[2]
            )
        # register catch-all route to handle dynamic routes (if enabled)
        if self._dynamic_routes is not None:
            self._webapp.router.add_route("*", "/{tail:.*}", self._handle_catch_all)
        await self._apprunner.setup()
        # set host to None to bind to all addresses on both IPv4 and IPv6
        host = None if bind_ip == "0.0.0.0" else bind_ip
        try:
            self._tcp_site = web.TCPSite(self._apprunner, host=host, port=bind_port)
            await self._tcp_site.start()
        except OSError:
            if host is None:
                raise
            # the configured interface is not available, retry on all interfaces
            self.logger.error(
                "Could not bind to %s, will start on all interfaces as fallback!", host
            )
            self._tcp_site = web.TCPSite(self._apprunner, host=None, port=bind_port)
            await self._tcp_site.start()

    async def close(self) -> None:
        """Cleanup on exit."""
        # stop/clean webserver
        await self._tcp_site.stop()
        await self._apprunner.cleanup()
        await self._webapp.shutdown()
        await self._webapp.cleanup()

    @property
    def base_url(self):
        """Return the base URL of this webserver."""
        return self._base_url

    @property
    def port(self):
        """Return the port of this webserver."""
        return self._bind_port

    def register_dynamic_route(self, path: str, handler: Awaitable, method: str = "*") -> Callable:
        """Register a dynamic route on the webserver, returns handler to unregister."""
        if self._dynamic_routes is None:
            msg = "Dynamic routes are not enabled"
            raise RuntimeError(msg)
        key = f"{method}.{path}"
        if key in self._dynamic_routes:  # pylint: disable=unsupported-membership-test
            msg = f"Route {path} already registered."
            raise RuntimeError(msg)
        self._dynamic_routes[key] = handler

        def _remove():
            return self._dynamic_routes.pop(key)

        return _remove

    def unregister_dynamic_route(self, path: str, method: str = "*") -> None:
        """Unregister a dynamic route from the webserver."""
        if self._dynamic_routes is None:
            msg = "Dynamic routes are not enabled"
            raise RuntimeError(msg)
        key = f"{method}.{path}"
        self._dynamic_routes.pop(key)

    async def serve_static(self, file_path: str, request: web.Request) -> web.FileResponse:
        """Serve file response."""
        headers = {"Cache-Control": "no-cache"}
        return web.FileResponse(file_path, headers=headers)

    async def _handle_catch_all(self, request: web.Request) -> web.Response:
        """Redirect request to correct destination."""
        # find handler for the request
        for key in (f"{request.method}.{request.path}", f"*.{request.path}"):
            if handler := self._dynamic_routes.get(key):
                return await handler(request)
        # deny all other requests
        self.logger.warning(
            "Received unhandled %s request to %s from %s\nheaders: %s\n",
            request.method,
            request.path,
            request.remote,
            request.headers,
        )
        return web.Response(status=404)
