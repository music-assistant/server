"""
The web module handles serving the frontend and the rest/websocket api's.

API is available with both HTTP json rest endpoints AND WebSockets.
All MusicAssistant clients communicate with the websockets api.
For now, we do not yet support SSL/HTTPS directly, to prevent messing with certificates etc.
The server is intended to be used locally only and not exposed outside.
Users may use reverse proxy etc. to add ssl themselves.
"""
import asyncio
import datetime
import logging
import os
import uuid
from base64 import b64encode
from typing import Any, Awaitable, Optional, Union

import aiohttp_cors
import jwt
import ujson
from aiohttp import web
from aiohttp.web_request import Request
from aiohttp_jwt import JWTMiddleware, login_required
from music_assistant.constants import (
    CONF_KEY_SECURITY,
    CONF_KEY_SECURITY_APP_TOKENS,
    CONF_KEY_SECURITY_LOGIN,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from music_assistant.constants import __version__ as MASS_VERSION
from music_assistant.helpers import repath
from music_assistant.helpers.encryption import decrypt_string
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
        self.jwt_key = None
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
        self.jwt_key = decrypt_string(self.mass.config.stored_config["jwt_key"])
        jwt_middleware = JWTMiddleware(
            self.jwt_key,
            request_property="user",
            credentials_required=False,
            is_revoked=self.is_token_revoked,
        )
        self.app = web.Application(middlewares=[jwt_middleware])
        self.app["mass"] = self.mass
        self.app["websockets"] = []
        # add all routes routes
        self.app.add_routes(stream_routes)
        if not self.mass.config.stored_config["initialized"]:
            self.app.router.add_post("/setup", self.setup)
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
        LOGGER.info("Started Music Assistant server on %s", self.url)
        self.mass.add_event_listener(self.__async_handle_mass_events)

    async def async_stop(self):
        """Stop the webserver."""
        for ws_client in self.app["websockets"]:
            await ws_client.close("server shutdown")

    def register_api_route(self, cmd: str, func: Awaitable):
        """Register a command(handler) to the websocket api."""
        pattern = repath.pattern(cmd)
        self.api_routes[pattern] = func

    def register_api_routes(self, cls: Any):
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
    def server_id(self):
        """Return the device ID for this Music Assistant Server."""
        return self.mass.config.stored_config["server_id"]

    @api_route("info")
    async def discovery_info(self):
        """Return (discovery) info about this instance."""
        return {
            "id": self.server_id,
            "url": self.url,
            "host": self.host,
            "port": self.port,
            "version": MASS_VERSION,
            "friendly_name": get_hostname(),
            "initialized": self.mass.config.stored_config["initialized"],
        }

    async def login(self, request: Request):
        """Handle user login by form/json post. Will issue JWT token."""
        form = await request.post()
        try:
            username = form["username"]
            password = form["password"]
            app_id = form.get("app_id")
        except KeyError:
            data = await request.json()
            username = data["username"]
            password = data["password"]
            app_id = data.get("app_id")
        token_info = await self.get_token(username, password, app_id)
        if token_info:
            return web.Response(
                body=json_serializer(token_info), content_type="application/json"
            )
        return web.HTTPUnauthorized(body="Invalid username and/or password provided!")

    async def get_token(self, username: str, password: str, app_id: str = "") -> dict:
        """
        Validate given credentials and return JWT token.

        If app_id is provided, a long lived token will be issued which can be withdrawn by the user.
        """
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
                token_info["exp"] = datetime.datetime.utcnow() + datetime.timedelta(
                    days=365 * 10
                )
            else:
                token_info["exp"] = datetime.datetime.utcnow() + datetime.timedelta(
                    hours=8
                )
            token = jwt.encode(token_info, self.jwt_key).decode()
            if app_id:
                self.mass.config.stored_config[CONF_KEY_SECURITY][
                    CONF_KEY_SECURITY_APP_TOKENS
                ][client_id] = token_info
                self.mass.config.save()
            token_info["token"] = token
            return token_info
        return None

    async def setup(self, request: Request):
        """Handle first-time server setup through onboarding wizard."""
        if self.mass.config.stored_config["initialized"]:
            return web.HTTPUnauthorized()
        form = await request.post()
        username = form["username"]
        password = form["password"]
        # save credentials in config
        self.mass.config.security[CONF_KEY_SECURITY_LOGIN][CONF_USERNAME] = username
        self.mass.config.security[CONF_KEY_SECURITY_LOGIN][CONF_PASSWORD] = password
        self.mass.config.stored_config["initialized"] = True
        self.mass.config.save()
        return web.Response(status=200)

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
        if not self.mass.config.stored_config["initialized"]:
            return web.FileResponse(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup.html")
            )
        html_app = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "static/index.html"
        )
        if not os.path.isfile(html_app):
            raise web.HTTPFound("https://music-assistant.github.io/app")
        return web.FileResponse(html_app)

    async def __async_handle_mass_events(self, event, event_data):
        """Broadcast events to connected websocket clients."""
        for ws_client in self.app["websockets"]:
            if not ws_client.authenticated:
                continue
            try:
                await ws_client.send(event=event, data=event_data)
            except ConnectionResetError:
                # connection lost to this client, cleanup
                await ws_client.close()
            except Exception as exc:  # pylint: disable=broad-except
                # log all other errors but continue sending to all other clients
                LOGGER.exception(exc)

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
        if url:
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

    def is_token_revoked(self, request: Request, token_info: dict):
        """Return bool is token is revoked."""
        return self.mass.config.security.is_token_revoked(token_info)
