"""The web module handles serving the frontend and the rest/websocket api's."""
import asyncio
import datetime
import logging
import os
import uuid
from base64 import b64encode
from typing import Awaitable, Optional, Union

import aiohttp_cors
import jwt
import ujson
from aiohttp import web
from aiohttp.web_request import Request
from aiohttp_jwt import JWTMiddleware, login_required
from music_assistant.constants import __version__ as MASS_VERSION
from music_assistant.helpers import repath
from music_assistant.helpers.images import async_get_image_url, async_get_thumb_file
from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.helpers.util import get_hostname, get_ip
from music_assistant.helpers.web import (
    api_route,
    async_json_response,
    json_serializer,
    parse_arguments,
)
from music_assistant.models.media_types import ItemMapping, MediaItem

from .json_rpc import json_rpc_endpoint
from .streams import routes as stream_routes
from .websocket import WebSocketHandler

LOGGER = logging.getLogger("webserver")


class WebServer:
    """Webserver and json/websocket api."""

    def __init__(self, mass: MusicAssistantType, port: int):
        """Initialize class."""
        self.app = None
        self.mass = mass
        self._port = port
        # load/create/update config
        self._host = get_ip()
        self.config = mass.config.base["web"]
        self._runner = None
        self.api_routes = {}

    async def async_setup(self):
        """Perform async setup."""

        jwt_middleware = JWTMiddleware(
            self.device_id, request_property="user", credentials_required=False
        )
        self.app = web.Application(middlewares=[jwt_middleware])
        self.app["mass"] = self.mass
        self.app["websockets"] = []
        # add routes
        self.app.add_routes(stream_routes)
        self.app.router.add_post("/login", self.login)
        self.app.router.add_get("/jsonrpc.js", json_rpc_endpoint)
        self.app.router.add_post("/jsonrpc.js", json_rpc_endpoint)
        self.app.router.add_get("/ws", WebSocketHandler)
        self.app.router.add_get("/", self.index)
        self.app.router.add_put("/api/library/{tail:.*}/add", self.handle_api_request)
        self.app.router.add_delete(
            "/api/library/{tail:.*}/remove", self.handle_api_request
        )
        self.app.router.add_put(
            "/api/players/{tail:.*}/play_media", self.handle_api_request
        )
        self.app.router.add_put(
            "/api/players/{tail:.*}/play_uri", self.handle_api_request
        )
        # catch-all for all api routes is handled by our special method
        self.app.router.add_get("/api/{tail:.*}", self.handle_api_request)

        # register all methods decorated as api_route
        for cls in [
            self,
            self.mass.music,
            self.mass.players,
            self.mass.config,
            self.mass.library,
        ]:
            self.register_api_routes(cls)

        webdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static/")
        if os.path.isdir(webdir):
            self.app.router.add_static("/", webdir, append_version=True)
        else:
            # The (minified) build of the frontend(app) is included in the pypi releases
            LOGGER.warning("Loaded without frontend support.")

        # Add CORS support to all routes
        cors = aiohttp_cors.setup(
            self.app,
            defaults={
                "*": aiohttp_cors.ResourceOptions(
                    allow_credentials=True,
                    allow_headers="*",
                )
            },
        )
        for route in list(self.app.router.routes()):
            cors.add(route)

        # set custom server header
        async def on_prepare(request, response):
            response.headers[
                "Server"
            ] = f'MusicAssistant/{MASS_VERSION} {response.headers["Server"]}'

        self.app.on_response_prepare.append(on_prepare)
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        http_site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await http_site.start()
        LOGGER.info("Started HTTP webserver on port %s", self.port)
        self.mass.add_event_listener(self.__async_handle_mass_events)

    async def async_stop(self):
        """Stop the webserver."""
        for ws_client in self.app["websockets"]:
            await ws_client.close("server shutdown")

    def register_api_route(self, cmd: str, func: Awaitable):
        """Register a command(handler) to the websocket api."""
        pattern = repath.pattern(cmd)
        self.api_routes[pattern] = func

    def register_api_routes(self, cls):
        """Register all methods of a class (instance) that are decorated with api_route."""
        for item in dir(cls):
            func = getattr(cls, item)
            if not hasattr(func, "ws_cmd_path"):
                continue
            # method is decorated with our websocket decorator
            self.register_api_route(func.ws_cmd_path, func)

    @property
    def host(self):
        """Return the local IP address/host for this Music Assistant instance."""
        return self._host

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
        return self.mass.config.stored_config["server_id"]

    @api_route("info")
    async def discovery_info(self):
        """Return (discovery) info about this instance."""
        return {
            "id": self.device_id,
            "url": self.url,
            "host": self.host,
            "port": self.port,
            "version": MASS_VERSION,
            "friendly_name": get_hostname(),
        }

    async def login(self, request: Request):
        """Handle user login by form post."""
        form = await request.post()
        try:
            username = form["username"]
            password = form["password"]
        except KeyError:
            data = await request.json()
            username = data["username"]
            password = data["password"]
        token_info = await self.get_token(username, password)
        if token_info:
            return web.Response(
                body=json_serializer(token_info), content_type="application/json"
            )
        return web.HTTPUnauthorized(body="Invalid username and/or password provided!")

    async def get_token(self, username: str, password: str, appname: str = "") -> dict:
        """Validate given credentials and return JWT token."""
        verified = self.mass.config.validate_credentials(username, password)
        if verified:
            if appname:
                token_expires = datetime.datetime.utcnow() + datetime.timedelta(
                    days=365 * 10
                )
            else:
                token_expires = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
            client_id = str(uuid.uuid4())
            token = jwt.encode(
                {"username": username, "client_id": client_id, "exp": token_expires},
                self.device_id,
            )
            return {
                "user": username,
                "token": token.decode(),
                "expires": token_expires.isoformat(),
                "appname": appname,
                "client_id": client_id,
            }
        return None

    @login_required
    async def handle_api_request(self, request: Request):
        """Handle API route/command."""
        api_path = request.path.replace("/api/", "")
        LOGGER.debug("Handling %s - %s", api_path, request.get("user"))
        try:
            # TODO: parse mediaitems from body if needed
            data = await request.json(loads=ujson.loads)
        except Exception:  # pylint: disable=broad-except
            data = {}
        # work out handler for the given path/command
        for key in self.api_routes:
            match = repath.match(key, api_path)
            if match:
                try:
                    params = match.groupdict()
                    handler = self.mass.web.api_routes[key]
                    params = parse_arguments(handler, {**params, **data})
                    res = handler(**params)
                    if asyncio.iscoroutine(res):
                        res = await res
                    # return result of command to client
                    return await async_json_response(res)
                except Exception as exc:  # pylint: disable=broad-except
                    return web.Response(status=500, text=str(exc))
        return web.Response(status=404)

    async def index(self, request: web.Request):
        """Get the index page, redirect if we do not have a web directory."""
        # pylint: disable=unused-argument
        html_app = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "static/index.html"
        )
        if not os.path.isfile(html_app):
            raise web.HTTPFound("https://music-assistant.github.io/self.app")
        return web.FileResponse(html_app)

    async def __async_handle_mass_events(self, event, event_data):
        """Broadcast events to connected websocket clients."""
        for ws_client in self.app["websockets"]:
            if not ws_client.authenticated:
                continue
            await ws_client.send(event=event, data=event_data)

    @api_route("images/thumb")
    async def async_get_image_thumb(
        self,
        size: int,
        url: Optional[str] = "",
        item: Union[None, ItemMapping, MediaItem] = None,
    ):
        """Get (resized) thumb image for given URL or media item as base64 encoded string."""
        if not url and item:
            url = await async_get_image_url(
                self.mass, item.item_id, item.provider, item.media_type
            )
        img_file = await async_get_thumb_file(self.mass, url, size)
        if img_file:
            with open(img_file, "rb") as _file:
                icon_data = _file.read()
                icon_data = b64encode(icon_data)
                return "data:image/png;base64," + icon_data.decode()
        raise KeyError("Invalid item or url")

    @api_route("images/provider-icons/:provider_id?")
    async def async_get_provider_icon(self, provider_id: Optional[str]):
        """Get Provider icon as base64 encoded string."""
        if not provider_id:
            return {
                prov.id: await self.async_get_provider_icon(prov.id)
                for prov in self.mass.get_providers(include_unavailable=True)
            }
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        icon_path = os.path.join(base_dir, "providers", provider_id, "icon.png")
        if os.path.isfile(icon_path):
            with open(icon_path, "rb") as _file:
                icon_data = _file.read()
                icon_data = b64encode(icon_data)
                return "data:image/png;base64," + icon_data.decode()
        raise KeyError("Invalid provider: %s" % provider_id)
