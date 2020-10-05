"""The web module handles serving the frontend and the rest/websocket api's."""
import logging
import os
import ssl
import uuid

import aiohttp_cors
from aiohttp import web
from aiohttp_jwt import JWTMiddleware
from music_assistant.constants import __version__ as MASS_VERSION
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

    def __init__(self, mass):
        """Initialize class."""
        self.mass = mass
        # load/create/update config
        self._local_ip = get_ip()
        self._device_id = f"{uuid.getnode()}_{get_hostname()}"
        self.config = mass.config.base["web"]
        self._runner = None

        enable_ssl = self.config["ssl_certificate"] and self.config["ssl_key"]
        if self.config["ssl_certificate"] and not os.path.isfile(
            self.config["ssl_certificate"]
        ):
            enable_ssl = False
            LOGGER.warning(
                "SSL certificate file not found: %s", self.config["ssl_certificate"]
            )
        if self.config["ssl_key"] and not os.path.isfile(self.config["ssl_key"]):
            enable_ssl = False
            LOGGER.warning(
                "SSL certificate key file not found: %s", self.config["ssl_key"]
            )
        if not self.config.get("external_url"):
            enable_ssl = False
        self._enable_ssl = enable_ssl

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
        http_site = web.TCPSite(self._runner, "0.0.0.0", self.http_port)
        await http_site.start()
        LOGGER.info("Started HTTP webserver on port %s", self.http_port)
        if self._enable_ssl:
            ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(
                self.config["ssl_certificate"], self.config["ssl_key"]
            )
            https_site = web.TCPSite(
                self._runner, "0.0.0.0", self.https_port, ssl_context=ssl_context
            )
            await https_site.start()
            LOGGER.info(
                "Started HTTPS webserver on port %s - serving at FQDN %s",
                self.https_port,
                self.external_url,
            )

    async def async_stop(self):
        """Stop the webserver."""
        # if self._runner:
        #     await self._runner.cleanup()

    @property
    def internal_ip(self):
        """Return the local IP address for this Music Assistant instance."""
        return self._local_ip

    @property
    def http_port(self):
        """Return the HTTP port for this Music Assistant instance."""
        return self.config.get("http_port", 8095)

    @property
    def https_port(self):
        """Return the HTTPS port for this Music Assistant instance."""
        return self.config.get("https_port", 8096)

    @property
    def internal_url(self):
        """Return the internal URL for this Music Assistant instance."""
        return f"http://{self._local_ip}:{self.http_port}"

    @property
    def external_url(self):
        """Return the internal URL for this Music Assistant instance."""
        if self._enable_ssl and self.config.get("external_url"):
            return self.config["external_url"]
        return self.internal_url

    @property
    def device_id(self):
        """Return the device ID for this Music Assistant Server."""
        return self._device_id

    @property
    def discovery_info(self):
        """Return (discovery) info about this instance."""
        return {
            "id": self._device_id,
            "external_url": self.external_url,
            "internal_url": self.internal_url,
            "host": self.internal_ip,
            "http_port": self.http_port,
            "https_port": self.https_port,
            "ssl_enabled": self._enable_ssl,
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
