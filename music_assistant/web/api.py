"""Custom API implementation using websockets."""

import asyncio
import logging
import os
from base64 import b64encode
from typing import Any, Dict, Optional, Union

import aiofiles
import jwt
import ujson
from aiohttp import WSMsgType, web
from aiohttp.http_websocket import WSMessage
from music_assistant.helpers.errors import AuthenticationError
from music_assistant.helpers.images import get_image_url, get_thumb_file
from music_assistant.helpers.logger import HistoryLogHandler
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.web import (
    api_route,
    async_json_response,
    async_json_serializer,
    parse_arguments,
)
from music_assistant.models.media_types import MediaType

LOGGER = logging.getLogger("api")


@api_route("log")
async def get_log(tail: int = 200) -> str:
    """Return current application log."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, HistoryLogHandler):
            return handler.get_history()[-tail:]


@api_route("images/{media_type}/{provider}/{item_id}")
async def get_media_item_image_url(
    mass: MusicAssistant, media_type: MediaType, provider: str, item_id: str
) -> str:
    """Return image URL for given media item."""
    if provider == "url":
        return None
    return await get_image_url(mass, item_id, provider, media_type)


@api_route("images/thumb")
async def get_image_thumb(mass: MusicAssistant, url: str, size: int = 150) -> str:
    """Get (resized) thumb image for given URL as base64 string."""
    img_file = await get_thumb_file(mass, url, size)
    if img_file:
        async with aiofiles.open(img_file, "rb") as _file:
            img_data = await _file.read()
            return "data:image/png;base64," + b64encode(img_data).decode()
    raise KeyError("Invalid url!")


@api_route("images/provider-icons/{provider_id}")
async def get_provider_icon(provider_id: str) -> str:
    """Get Provider icon as base64 string."""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    icon_path = os.path.join(base_dir, "providers", provider_id, "icon.png")
    if os.path.isfile(icon_path):
        async with aiofiles.open(icon_path, "rb") as _file:
            img_data = await _file.read()
            return "data:image/png;base64," + b64encode(img_data).decode()
    raise KeyError("Invalid provider: %s" % provider_id)


@api_route("images/provider-icons")
async def get_provider_icons(mass: MusicAssistant) -> Dict[str, str]:
    """Get Provider icons as base64 strings."""
    return {
        prov.id: await get_provider_icon(prov.id)
        for prov in mass.get_providers(include_unavailable=True)
    }


async def handle_api_request(request: web.Request):
    """Handle API requests."""
    mass: MusicAssistant = request.app["mass"]
    LOGGER.debug("Handling %s", request.path)

    # check auth token
    auth_token = request.headers.get("Authorization", "").split("Bearer ")[-1]
    if not auth_token:
        raise web.HTTPUnauthorized(
            reason="Missing authorization token",
        )
    try:
        token_info = jwt.decode(auth_token, mass.web.jwt_key, algorithms=["HS256"])
    except jwt.InvalidTokenError as exc:
        LOGGER.exception(exc, exc_info=exc)
        msg = "Invalid authorization token, " + str(exc)
        raise web.HTTPUnauthorized(reason=msg)
    if mass.config.security.is_token_revoked(token_info):
        raise web.HTTPUnauthorized(reason="Token is revoked")
    mass.config.security.set_last_login(token_info["client_id"])

    # handle request
    handler, path_params = mass.web.get_api_handler(request.path, request.method)
    data = await request.json() if request.can_read_body else {}
    # execute handler and return results
    try:
        all_params = {**path_params, **request.query, **data}
        params = parse_arguments(mass, handler.signature, all_params)
        res = handler.target(**params)
        if asyncio.iscoroutine(res):
            res = await res
    except Exception as exc:  # pylint: disable=broad-except
        LOGGER.debug("Error while handling %s", request.path, exc_info=exc)
        raise web.HTTPInternalServerError(reason=str(exc))
    return await async_json_response(res)


class WebSocketApi(web.View):
    """RPC-like API implementation using websockets."""

    def __init__(self, request: web.Request):
        """Initialize."""
        super().__init__(request)
        self.authenticated = False
        self.ws_client: Optional[web.WebSocketResponse] = None

    @property
    def mass(self) -> MusicAssistant:
        """Return MusicAssistant instance."""
        return self.request.app["mass"]

    async def get(self):
        """Handle GET."""
        ws_client = web.WebSocketResponse()
        self.ws_client = ws_client
        await ws_client.prepare(self.request)
        self.request.app["ws_clients"].append(ws_client)
        await self._send_json(msg_type="info", data=self.mass.web.discovery_info)

        # add listener for mass events
        remove_listener = self.mass.eventbus.add_listener(self._handle_mass_event)

        # handle incoming messages
        try:
            async for msg in ws_client:
                await self.__handle_msg(msg)
        finally:
            # websocket disconnected
            remove_listener()
            self.request.app["ws_clients"].remove(ws_client)
            LOGGER.debug("websocket connection closed: %s", self.request.remote)

        return ws_client

    async def __handle_msg(self, msg: WSMessage):
        """Handle incoming message."""
        try:
            if msg.type == WSMsgType.error:
                LOGGER.warning(
                    "ws connection closed with exception %s", self.ws_client.exception()
                )
                return
            if msg.type != WSMsgType.text:
                return
            if msg.data == "close":
                await self.ws_client.close()
                return
            # process message
            json_msg = msg.json(loads=ujson.loads)
            # handle auth command
            if json_msg["type"] == "auth":
                token_info = jwt.decode(
                    json_msg["data"], self.mass.web.jwt_key, algorithms=["HS256"]
                )
                if self.mass.config.security.is_token_revoked(token_info):
                    raise AuthenticationError("Token is revoked")
                self.authenticated = True
                self.mass.config.security.set_last_login(token_info["client_id"])
                # TODO: store token/app_id on ws_client obj and periodically check if token is expired or revoked
                await self._send_json(
                    msg_type="result",
                    msg_id=json_msg.get("id"),
                    data=token_info,
                )
            elif not self.authenticated:
                raise AuthenticationError("Not authenticated")
            # handle regular command
            elif json_msg["type"] == "command":
                await self._handle_command(
                    json_msg["data"],
                    msg_id=json_msg.get("id"),
                )
        except AuthenticationError as exc:  # pylint:disable=broad-except
            # disconnect client on auth errors
            await self._send_json(
                msg_type="error", msg_id=json_msg.get("id"), data=str(exc)
            )
            await self.ws_client.close(message=str(exc).encode())
        except Exception as exc:  # pylint:disable=broad-except
            # log the error only
            await self._send_json(
                msg_type="error", msg_id=json_msg.get("id"), data=str(exc)
            )
            LOGGER.error("Error with WS client", exc_info=exc)

    async def _handle_command(
        self,
        cmd_data: Union[str, dict],
        msg_id: Any = None,
    ):
        """Handle websocket command."""
        # Command may be provided as string or a dict
        if isinstance(cmd_data, str):
            path = cmd_data
            method = "GET"
            params = {}
        else:
            path = cmd_data["path"]
            method = cmd_data.get("method", "GET")
            params = {x: cmd_data[x] for x in cmd_data if x not in ["path", "method"]}
        LOGGER.debug("Handling command %s/%s", method, path)
        # work out handler for the given path/command
        route, path_params = self.mass.web.get_api_handler(path, method)
        args = parse_arguments(self.mass, route.signature, {**params, **path_params})
        res = route.target(**args)
        if asyncio.iscoroutine(res):
            res = await res
        # return result of command to client
        return await self._send_json(msg_type="result", msg_id=msg_id, data=res)

    async def _send_json(
        self,
        msg_type: str,
        msg_id: Optional[int] = None,
        data: Optional[Any] = None,
    ):
        """Send message (back) to websocket client."""
        await self.ws_client.send_str(
            await async_json_serializer({"type": msg_type, "id": msg_id, "data": data})
        )

    async def _handle_mass_event(self, event: str, event_data: Any):
        """Broadcast events to connected client."""
        if not self.authenticated:
            return
        try:
            await self._send_json(
                msg_type="event",
                data={"event": event, "event_data": event_data},
            )
        except ConnectionResetError as exc:
            LOGGER.debug("Error while sending message to api client", exc_info=exc)
            await self.ws_client.close()
