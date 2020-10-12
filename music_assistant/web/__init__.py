"""The web module handles serving the frontend and the rest/websocket api's."""
import logging
import os
import uuid

import aiohttp_cors
from aiohttp import web
from aiohttp_jwt import JWTMiddleware
from music_assistant.constants import __version__ as MASS_VERSION
from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.helpers.util import get_hostname, get_ip, json_serializer

from .endpoints import (
    albums,
    artists,
    config,
    images,
    json_rpc,
    library,
    login,
    players,
    playlists,
    radios,
    search,
    streams,
    tracks,
    websocket,
)

LOGGER = logging.getLogger("webserver")


routes = web.RouteTableDef()


class WebServer:
    """Webserver and json/websocket api."""

    def __init__(self, mass: MusicAssistantType, port: int):
        """Initialize class."""
        self.mass = mass
        self._port = port
        # load/create/update config
        self._local_ip = get_ip()
        self._device_id = f"{uuid.getnode()}_{get_hostname()}"
        self.config = mass.config.base["web"]
        self._runner = None

    async def async_setup(self):
        """Perform async setup."""

        jwt_middleware = JWTMiddleware(
            self.device_id, request_property="user", credentials_required=False
        )
        app = web.Application(middlewares=[jwt_middleware])
        app["mass"] = self.mass
        # add routes
        app.add_routes(albums.routes)
        app.add_routes(artists.routes)
        app.add_routes(config.routes)
        app.add_routes(images.routes)
        app.add_routes(json_rpc.routes)
        app.add_routes(library.routes)
        app.add_routes(login.routes)
        app.add_routes(players.routes)
        app.add_routes(playlists.routes)
        app.add_routes(radios.routes)
        app.add_routes(search.routes)
        app.add_routes(streams.routes)
        app.add_routes(tracks.routes)
        app.add_routes(websocket.routes)
        app.add_routes(routes)

        webdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static/")
        if os.path.isdir(webdir):
            app.router.add_static("/", webdir, append_version=True)
        else:
            # The (minified) build of the frontend(app) is included in the pypi releases
            LOGGER.warning("Loaded without frontend support.")

        # Add CORS support to all routes
        cors = aiohttp_cors.setup(
            app,
            defaults={
                "*": aiohttp_cors.ResourceOptions(
                    allow_credentials=True,
                    expose_headers="*",
                    allow_headers="*",
                    allow_methods=["POST", "PUT", "DELETE", "GET"],
                )
            },
        )
        for route in list(app.router.routes()):
            cors.add(route)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        http_site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await http_site.start()
        LOGGER.info("Started HTTP webserver on port %s", self.port)

    async def async_stop(self):
        """Stop the webserver."""
        # if self._runner:
        #     await self._runner.cleanup()

    @property
    def host(self):
        """Return the local IP address/host for this Music Assistant instance."""
        return self._local_ip

    @property
    def port(self):
        """Return the port for this Music Assistant instance."""
        return self._port

    @property
    def url(self):
        """Return the URL for this Music Assistant instance."""
        return f"http://{self.host}:{self.port}"

    @property
    def device_id(self):
        """Return the device ID for this Music Assistant Server."""
        return self._device_id

    @property
    def discovery_info(self):
        """Return (discovery) info about this instance."""
        return {
            "id": self._device_id,
            "url": self.url,
            "host": self.host,
            "port": self.port,
            "version": MASS_VERSION,
        }


@routes.get("/api/info")
async def async_discovery_info(request: web.Request):
    # pylint: disable=unused-argument
    """Return (discovery) info about this instance."""
    return web.Response(
        body=json_serializer(request.app["mass"].web.discovery_info),
        content_type="application/json",
    )


@routes.get("/")
async def async_index(request: web.Request):
    """Get the index page, redirect if we do not have a web directory."""
    # pylint: disable=unused-argument
    html_app = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "static/index.html"
    )
    if not os.path.isfile(html_app):
        raise web.HTTPFound("https://music-assistant.github.io/app")
    return web.FileResponse(html_app)
