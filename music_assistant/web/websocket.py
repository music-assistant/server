"""Websocket API endpoint."""
import asyncio
import logging
from typing import Any, Optional

import jwt
import ujson
from aiohttp import WSMsgType
from aiohttp.web import View, WebSocketResponse
from music_assistant.helpers import repath
from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.helpers.web import json_serializer, parse_arguments

LOGGER = logging.getLogger("web.endpoints.websocket")


class WebSocketHandler(View):
    """Handler for websockets API."""

    authenticated = False
    _ws = None
    mass: MusicAssistantType = None

    def __init__(self, *args, **kwargs):
        """Initialize."""
        super().__init__(*args, **kwargs)
        self.mass: MusicAssistantType = self.request.app["mass"]

    async def get(self):
        """Handle main ws entrypoint."""
        websocket = WebSocketResponse()
        await websocket.prepare(self.request)

        self.request.app["websockets"].append(self)
        self._ws = websocket

        LOGGER.debug("new client connected: %s", self.request.remote)

        async for msg in websocket:
            if msg.type == WSMsgType.text:
                if msg.data == "close":
                    await websocket.close()
                    break
                try:
                    json_msg = msg.json(loads=ujson.loads)
                    if "command" in json_msg and "data" in json_msg:
                        # handle command
                        await self.handle_command(
                            json_msg["command"],
                            json_msg["data"],
                            json_msg.get("id"),
                        )
                    elif "event" in json_msg:
                        # handle event
                        await self.handle_event(json_msg["event"], json_msg.get("data"))
                    else:
                        raise KeyError
                except (KeyError, ValueError):
                    await self.send(
                        error='commands must be issued in json format \
                            {"command": "command", "data":" optional data"}',
                    )
            elif msg.type == WSMsgType.error:
                LOGGER.warning(
                    "ws connection closed with exception %s", websocket.exception()
                )

        # websocket disconnected
        await self.close()
        return websocket

    async def send(self, **kwargs):
        """Send message (back) to websocket client."""
        ws_msg = kwargs
        await self._ws.send_str(json_serializer(ws_msg))

    async def close(self, reason=""):
        """Close websocket connection."""
        try:
            await self._ws.close(message=reason.encode())
        except Exception:  # pylint: disable=broad-except
            pass
        try:
            self.request.app["websockets"].remove(self)
        except Exception:  # pylint: disable=broad-except
            pass
        LOGGER.debug("websocket connection closed: %s", self.request.remote)

    async def handle_command(self, command: str, data: Optional[dict], id: Any = None):
        """Handle websocket command."""
        res = None
        try:
            if command == "auth":
                res = await self.auth(data)
                return await self.send(id=id, result=command, data=res)
            if command == "get_token":
                res = await self.mass.web.get_token(**data)
                if not res:
                    raise Exception("Invalid credentials")
                return await self.send(id=id, result=command, data=res)
            if not self.authenticated:
                return await self.send(
                    id=id,
                    result=command,
                    error="Not authenticated, please login first.",
                )
            # work out handler for the given path/command
            for key in self.mass.web.api_routes:
                match = repath.match(key, command)
                if match:
                    params = match.groupdict()
                    handler = self.mass.web.api_routes[key]
                    if not data:
                        data = {}
                    params = parse_arguments(handler, {**params, **data})
                    res = handler(**params)
                    if asyncio.iscoroutine(res):
                        res = await res
                    # return result of command to client
                    return await self.send(id=id, result=command, data=res)
            raise KeyError("Unknown command")
        except Exception as exc:  # pylint:disable=broad-except
            return await self.send(result=command, error=str(exc))

    async def handle_event(self, event: str, data: Any):
        """Handle command message."""
        LOGGER.info("received event %s", event)
        if self.authenticated:
            self.mass.signal_event(event, data)

    async def auth(self, token: str):
        """Handle authentication with JWT token."""
        token_info = jwt.decode(token, self.mass.web.jwt_key)
        if self.mass.web.is_token_revoked(None, token_info):
            raise Exception("Token is revoked")
        self.authenticated = True
        return token_info
