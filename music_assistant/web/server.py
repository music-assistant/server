"""
The web module handles serving the custom websocket api on a custom port (default is 8095).

All MusicAssistant clients communicate locally with this websockets api.
The server is intended to be used locally only and not exposed outside,
so it is HTTP only. Secure remote connections will be offered by a remote connect broker.
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
from aiohttp import WSMsgType, web
from aiohttp.web import WebSocketResponse
from music_assistant.constants import (
    CONF_KEY_SECURITY_LOGIN,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from music_assistant.constants import __version__ as MASS_VERSION
from music_assistant.helpers import repath
from music_assistant.helpers.encryption import decrypt_string
from music_assistant.helpers.images import get_image_url, get_thumb_file
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import get_hostname, get_ip
from music_assistant.helpers.web import api_route, json_serializer, parse_arguments
from music_assistant.models.media_types import ItemMapping, MediaItem

from .json_rpc import json_rpc_endpoint
from .streams import routes as stream_routes

LOGGER = logging.getLogger("webserver")


class WebServer:
    """Webserver and json/websocket api."""

    def __init__(self, mass: MusicAssistant, port: int):
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
        self.api_routes = {}

    async def setup(self):
        """Perform async setup."""
        self.jwt_key = await decrypt_string(self.mass.config.stored_config["jwt_key"])
        self.app = web.Application()
        self.app["mass"] = self.mass
        self.app["clients"] = []
        # add all routes
        self.app.add_routes(stream_routes)
        self.app.router.add_route("*", "/jsonrpc.js", json_rpc_endpoint)
        self.app.router.add_get("/ws", self._websocket_handler)

        # register all methods decorated as api_route
        for cls in [
            self,
            self.mass.music,
            self.mass.players,
            self.mass.config,
            self.mass.library,
        ]:
            self.register_api_routes(cls)

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
        # Host the frontend app
        webdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static/")
        if os.path.isdir(webdir):
            self.app.router.add_get("/", self.index)
            self.app.router.add_static("/", webdir, append_version=True)
        else:
            self.app.router.add_get("/", self.info)

        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        # set host to None to bind to all addresses on both IPv4 and IPv6
        http_site = web.TCPSite(self._runner, host=None, port=self.port)
        await http_site.start()
        LOGGER.info("Started Music Assistant server on port %s", self.port)
        self.mass.add_event_listener(self._handle_mass_events)

    async def stop(self):
        """Stop the webserver."""
        for ws_client in self.app["clients"]:
            await ws_client.close(message=b"server shutdown")

    def register_api_route(self, cmd: str, func: Awaitable):
        """Register a command(handler) to the websocket api."""
        pattern = repath.path_to_pattern(cmd)
        self.api_routes[pattern] = func

    def register_api_routes(self, cls: Any):
        """Register all methods of a class (instance) that are decorated with api_route."""
        for item in dir(cls):
            func = getattr(cls, item)
            if not hasattr(func, "ws_cmd_path"):
                continue
            # method is decorated with our api decorator
            self.register_api_route(func.ws_cmd_path, func)

    @property
    def hostname(self):
        """Return the hostname for this Music Assistant instance."""
        if not self._hostname.endswith(".local"):
            # probably running in docker, use mdns name instead
            return f"mass_{self.server_id}.local"
        return self._hostname

    @property
    def ip_address(self):
        """Return the local IP(v4) address for this Music Assistant instance."""
        return self._ip_address

    @property
    def port(self):
        """Return the port for this Music Assistant instance."""
        return self._port

    @property
    def stream_url(self):
        """Return the base stream URL for this Music Assistant instance."""
        # dns resolving often fails on stream devices so use IP-address
        return f"http://{self.ip_address}:{self.port}/stream"

    @property
    def address(self):
        """Return the API connect address for this Music Assistant instance."""
        return f"ws://{self.hostname}:{self.port}/ws"

    @property
    def server_id(self):
        """Return the device ID for this Music Assistant Server."""
        return self.mass.config.stored_config["server_id"]

    @property
    def discovery_info(self):
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

    async def index(self, request: web.Request):
        """Get the index page."""
        # pylint: disable=unused-argument
        html_app = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "static/index.html"
        )
        return web.FileResponse(html_app)

    @api_route("info", False)
    async def info(self, request: web.Request = None):
        """Return discovery info on index page."""
        if request:
            return web.json_response(self.discovery_info)
        return self.discovery_info

    @api_route("revoke_token")
    async def revoke_token(self, client_id: str):
        """Revoke token for client."""
        return self.mass.config.security.revoke_app_token(client_id)

    @api_route("get_token", False)
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
                token_info["enabled"] = True
                token_info["exp"] = (
                    datetime.datetime.utcnow() + datetime.timedelta(days=365 * 10)
                ).timestamp()
            else:
                token_info["exp"] = (
                    datetime.datetime.utcnow() + datetime.timedelta(hours=8)
                ).timestamp()
            token = jwt.encode(token_info, self.jwt_key, algorithm="HS256")
            if app_id:
                self.mass.config.security.add_app_token(token_info)
            token_info["token"] = token
            return token_info
        raise AuthenticationError("Invalid credentials")

    @api_route("setup", False)
    async def create_user_setup(self, username: str, password: str):
        """Handle first-time server setup through onboarding wizard."""
        if self.mass.config.stored_config["initialized"]:
            raise AuthenticationError("Already initialized")
        # save credentials in config
        self.mass.config.security[CONF_KEY_SECURITY_LOGIN][CONF_USERNAME] = username
        self.mass.config.security[CONF_KEY_SECURITY_LOGIN][CONF_PASSWORD] = password
        self.mass.config.stored_config["initialized"] = True
        self.mass.config.save()
        # fix discovery info
        await self.mass.setup_discovery()
        return True

    @api_route("images/thumb")
    async def get_image_thumb(
        self,
        size: int,
        url: Optional[str] = "",
        item: Union[None, ItemMapping, MediaItem] = None,
    ):
        """Get (resized) thumb image for given URL or media item as base64 encoded string."""
        if not url and item:
            url = await get_image_url(
                self.mass, item.item_id, item.provider, item.media_type
            )
        if url:
            img_file = await get_thumb_file(self.mass, url, size)
            if img_file:
                with open(img_file, "rb") as _file:
                    icon_data = _file.read()
                    icon_data = b64encode(icon_data)
                    return "data:image/png;base64," + icon_data.decode()
        raise KeyError("Invalid item or url")

    @api_route("images/provider-icons/:provider_id?")
    async def get_provider_icon(self, provider_id: Optional[str]):
        """Get Provider icon as base64 encoded string."""
        if not provider_id:
            return {
                prov.id: await self.get_provider_icon(prov.id)
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

    async def _websocket_handler(self, request: web.Request):
        """Handle websocket client."""

        ws_client = WebSocketResponse()
        ws_client.authenticated = False
        await ws_client.prepare(request)
        request.app["clients"].append(ws_client)

        # handle incoming messages
        async for msg in ws_client:
            try:
                if msg.type == WSMsgType.error:
                    LOGGER.warning(
                        "ws connection closed with exception %s", ws_client.exception()
                    )
                if msg.type != WSMsgType.text:
                    continue
                if msg.data == "close":
                    await ws_client.close()
                    break
                # regular message
                json_msg = msg.json(loads=ujson.loads)
                if "command" in json_msg and "data" in json_msg:
                    # handle command
                    await self._handle_command(
                        ws_client,
                        json_msg["command"],
                        json_msg["data"],
                        json_msg.get("id"),
                    )
                elif "event" in json_msg:
                    # handle event
                    await self._handle_event(
                        ws_client, json_msg["event"], json_msg.get("data")
                    )
            except AuthenticationError as exc:  # pylint:disable=broad-except
                # disconnect client on auth errors
                await self._send_json(ws_client, error=str(exc), **json_msg)
                await ws_client.close(message=str(exc).encode())
            except Exception as exc:  # pylint:disable=broad-except
                # log the error only
                await self._send_json(ws_client, error=str(exc), **json_msg)
                LOGGER.error("Error with WS client", exc_info=exc)

        # websocket disconnected
        request.app["clients"].remove(ws_client)
        LOGGER.debug("websocket connection closed: %s", request.remote)

        return ws_client

    async def _handle_command(
        self,
        ws_client: WebSocketResponse,
        command: str,
        data: Optional[dict],
        msg_id: Any = None,
    ):
        """Handle websocket command."""
        res = None
        if command == "auth":
            return await self._handle_auth(ws_client, data)
        # work out handler for the given path/command
        for key in self.api_routes:
            match = repath.match_pattern(key, command)
            if match:
                params = match.groupdict()
                handler = self.api_routes[key]
                # check authentication
                if (
                    getattr(handler, "ws_require_auth", True)
                    and not ws_client.authenticated
                ):
                    raise AuthenticationError("Not authenticated")
                if not data:
                    data = {}
                params = parse_arguments(handler, {**params, **data})
                res = handler(**params)
                if asyncio.iscoroutine(res):
                    res = await res
                # return result of command to client
                return await self._send_json(
                    ws_client, id=msg_id, result=command, data=res
                )
        raise KeyError("Unknown command")

    async def _handle_event(self, ws_client: WebSocketResponse, event: str, data: Any):
        """Handle event message from ws client."""
        LOGGER.info("received event %s", event)
        if ws_client.authenticated:
            self.mass.signal_event(event, data)

    async def _handle_auth(self, ws_client: WebSocketResponse, token: str):
        """Handle authentication with JWT token."""
        token_info = jwt.decode(token, self.mass.web.jwt_key, algorithms=["HS256"])
        if self.mass.config.security.is_token_revoked(token_info):
            raise AuthenticationError("Token is revoked")
        ws_client.authenticated = True
        self.mass.config.security.set_last_login(token_info["client_id"])
        # TODO: store token/app_id on ws_client obj and periodiclaly check if token is expired or revoked
        await self._send_json(ws_client, result="auth", data=token_info)

    async def _send_json(self, ws_client: WebSocketResponse, **kwargs):
        """Send message (back) to websocket client."""
        await ws_client.send_str(json_serializer(kwargs))

    async def _handle_mass_events(self, event: str, event_data: Any):
        """Broadcast events to connected clients."""
        for ws_client in self.app["clients"]:
            if not ws_client.authenticated:
                continue
            try:
                await self._send_json(ws_client, event=event, data=event_data)
            except ConnectionResetError:
                # client is already disconnected
                self.app["clients"].remove(ws_client)
            except Exception as exc:  # pylint: disable=broad-except
                # log errors and continue sending to all other clients
                LOGGER.debug("Error while sending message to api client", exc_info=exc)


class AuthenticationError(Exception):
    """Custom Exception for all authentication errors."""
