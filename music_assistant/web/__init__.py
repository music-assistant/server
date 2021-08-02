"""
The web module handles serving the custom websocket api on a custom port (default is 8095).

All MusicAssistant clients communicate locally with this websockets api.
The server is intended to be used locally only and not exposed outside,
so it is HTTP only. Secure remote connections will be offered by a remote connect broker.
"""
import logging
import os
import uuid
from json.decoder import JSONDecodeError
from typing import Callable, List, Tuple

import aiofiles
import aiohttp_cors
import jwt
import music_assistant.web.api as api
from aiohttp import web
from aiohttp.web_exceptions import HTTPNotFound, HTTPUnauthorized
from music_assistant.constants import (
    CONF_KEY_SECURITY_LOGIN,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from music_assistant.constants import __version__ as MASS_VERSION
from music_assistant.helpers.datetime import future_timestamp
from music_assistant.helpers.encryption import decrypt_string
from music_assistant.helpers.errors import AuthenticationError
from music_assistant.helpers.images import get_thumb_file
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import get_hostname, get_ip
from music_assistant.helpers.web import APIRoute, create_api_route

from .json_rpc import json_rpc_endpoint
from .stream import routes as stream_routes

LOGGER = logging.getLogger("webserver")


class WebServer:
    """Webserver and json/websocket api."""

    def __init__(self, mass: MusicAssistant, port: int) -> None:
        """Initialize class."""
        self.jwt_key = None
        self.app = None
        self.mass = mass
        self._port = port
        # load/create/update config
        self._hostname = get_hostname().lower()
        self._ip_address = get_ip()
        self.config = mass.config.base["web"]
        self._runner = None
        self.api_routes: List[APIRoute] = []

    async def setup(self) -> None:
        """Perform async setup."""
        self.jwt_key = await decrypt_string(self.mass.config.stored_config["jwt_key"])
        self.app = web.Application()
        self.app["mass"] = self.mass
        self.app["ws_clients"] = []
        # add all routes
        self.app.add_routes(stream_routes)
        self.app.router.add_route("*", "/jsonrpc.js", json_rpc_endpoint)
        self.app.router.add_view("/ws", api.WebSocketApi)

        # Add server discovery on info including CORS support
        cors = aiohttp_cors.setup(
            self.app,
            defaults={
                "*": aiohttp_cors.ResourceOptions(
                    allow_credentials=True,
                    allow_headers="*",
                )
            },
        )
        cors.add(self.app.router.add_get("/info", self.info))
        cors.add(self.app.router.add_post("/login", self.login))
        cors.add(self.app.router.add_post("/setup", self.first_setup))
        cors.add(self.app.router.add_get("/thumb", self.image_thumb))
        self.app.router.add_route("*", "/api/{tail:.+}", api.handle_api_request)
        # Host the frontend app
        webdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static/")
        if os.path.isdir(webdir):
            self.app.router.add_get("/", self.index)
            self.app.router.add_static("/", webdir, append_version=True)

        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        # set host to None to bind to all addresses on both IPv4 and IPv6
        http_site = web.TCPSite(self._runner, host=None, port=self.port)
        await http_site.start()
        self.add_api_routes()
        LOGGER.info("Started Music Assistant server on port %s", self.port)

    async def stop(self) -> None:
        """Stop the webserver."""
        for ws_client in self.app["ws_clients"]:
            await ws_client.close(message=b"server shutdown")

    def add_api_routes(self) -> None:
        """Register all methods decorated as api_route."""
        for cls in [
            api,
            self.mass.music,
            self.mass.players,
            self.mass.config,
            self.mass.library,
            self.mass.tasks,
        ]:
            for item in dir(cls):
                func = getattr(cls, item)
                if not hasattr(func, "api_path"):
                    continue
                # method is decorated with our api decorator
                self.register_api_route(func.api_path, func, func.api_method)

    def register_api_route(
        self,
        path: str,
        handler: Callable,
        method: str = "GET",
    ) -> None:
        """Dynamically register a path/route on the API."""
        route = create_api_route(path, handler, method)
        # TODO: swagger generation
        self.api_routes.append(route)

    @property
    def hostname(self) -> str:
        """Return the hostname for this Music Assistant instance."""
        if not self._hostname.endswith(".local"):
            # probably running in docker, use mdns name instead
            return f"mass_{self.server_id}.local"
        return self._hostname

    @property
    def ip_address(self) -> str:
        """Return the local IP(v4) address for this Music Assistant instance."""
        return self._ip_address

    @property
    def port(self) -> int:
        """Return the port for this Music Assistant instance."""
        return self._port

    @property
    def stream_url(self) -> str:
        """Return the base stream URL for this Music Assistant instance."""
        # dns resolving often fails on stream devices so use IP-address
        return f"http://{self.ip_address}:{self.port}/stream"

    @property
    def address(self) -> str:
        """Return the base HTTP address for this Music Assistant instance."""
        return f"http://{self.hostname}:{self.port}"

    @property
    def server_id(self) -> str:
        """Return the device ID for this Music Assistant Server."""
        return self.mass.config.stored_config["server_id"]

    @property
    def discovery_info(self) -> dict:
        """Return discovery info for this Music Assistant server."""
        return {
            "id": self.server_id,
            "address": self.address,
            "hostname": self.hostname,
            "ip_address": self.ip_address,
            "port": self.port,
            "version": MASS_VERSION,
            "friendly_name": self.mass.config.stored_config["friendly_name"],
            "initialized": self.mass.config.stored_config["initialized"],
        }

    async def index(self, request: web.Request) -> web.FileResponse:
        """Get the index page."""
        # pylint: disable=unused-argument
        html_app = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "static/index.html"
        )
        return web.FileResponse(html_app)

    async def info(self, request: web.Request) -> web.Response:
        """Return server discovery info."""
        return web.json_response(self.discovery_info)

    async def login(self, request: web.Request) -> web.Response:
        """
        Validate given credentials and return JWT token.

        If app_id is provided, a long lived token will be issued which can be withdrawn by the user.
        """
        try:
            data = await request.post()
            if not data:
                data = await request.json()
        except JSONDecodeError:
            data = await request.json()
        username = data["username"]
        password = data["password"]
        app_id = data.get("app_id", "")
        verified = self.mass.config.security.validate_credentials(username, password)
        if verified:
            client_id = str(uuid.uuid4())
            token_info = {
                "username": username,
                "server_id": self.server_id,
                "client_id": client_id,
                "app_id": app_id,
            }
            if app_id:
                token_info["enabled"] = True
                token_info["exp"] = future_timestamp(days=365 * 10)
            else:
                token_info["exp"] = future_timestamp(hours=8)
            token = jwt.encode(token_info, self.jwt_key, algorithm="HS256")
            if app_id:
                self.mass.config.security.add_app_token(token_info)
            token_info["token"] = token
            return web.json_response(token_info)
        raise HTTPUnauthorized(reason="Invalid credentials")

    async def first_setup(self, request: web.Request) -> web.Response:
        """Handle first-time server setup through onboarding wizard."""
        try:
            data = await request.post()
            if not data:
                data = await request.json()
        except JSONDecodeError:
            data = await request.json()
        username = data["username"]
        password = data["password"]
        if self.mass.config.stored_config["initialized"]:
            raise AuthenticationError("Already initialized")
        # save credentials in config
        self.mass.config.security[CONF_KEY_SECURITY_LOGIN][CONF_USERNAME] = username
        self.mass.config.security[CONF_KEY_SECURITY_LOGIN][CONF_PASSWORD] = password
        self.mass.config.stored_config["initialized"] = True
        self.mass.config.save()
        # fix discovery info
        await self.mass.setup_discovery()
        return web.json_response(self.discovery_info)

    async def image_thumb(self, request: web.Request) -> web.Response:
        """Get (resized) thumb image for given URL."""
        url = request.query.get("url")
        size = int(request.query.get("size", 150))

        img_file = await get_thumb_file(self.mass, url, size)
        if img_file:
            async with aiofiles.open(img_file, "rb") as _file:
                img_data = await _file.read()
                headers = {
                    "Content-Type": "image/png",
                    "Cache-Control": "public, max-age=604800",
                }
                return web.Response(body=img_data, headers=headers)
        raise KeyError("Invalid url!")

    def get_api_handler(self, path: str, method: str) -> Tuple[APIRoute, dict]:
        """Find API route match for given path."""
        matchpath = path.replace("/api/", "")
        for route in self.api_routes:
            match = route.match(matchpath, method)
            if match:
                return match[0], match[1]
        raise HTTPNotFound(reason="Invalid path: %s" % path)
