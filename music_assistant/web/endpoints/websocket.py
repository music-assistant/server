"""Websocket API endpoint."""

import logging
from typing import Union

import jwt
import ujson
from aiohttp import WSMsgType
from aiohttp.web import Request, RouteTableDef, WebSocketResponse
from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.helpers.web import json_serializer

from .login import async_get_token

routes = RouteTableDef()
ws_commands = dict()

LOGGER = logging.getLogger("web.endpoints.websocket")


def ws_command(cmd):
    """Register a websocket command."""

    def decorate(func):
        ws_commands[cmd] = func
        return func

    return decorate


@routes.get("/ws")
async def async_websocket_handler(request: Request):
    """Handle websockets connection."""
    ws_response = None
    authenticated = False
    _callbacks = []
    mass = request.app["mass"]
    try:
        ws_response = WebSocketResponse()
        await ws_response.prepare(request)

        # callback for internal events
        async def async_send_message(
            msg: str, msg_details: Union[None, dict, str] = None
        ):
            """Send message (back) to websocket client."""
            ws_msg = {"message": msg, "message_details": msg_details}
            try:
                await ws_response.send_str(json_serializer(ws_msg))
            # pylint: disable=broad-except
            except Exception as exc:
                LOGGER.debug(
                    "Error while trying to send message to websocket (probably disconnected): %s",
                    str(exc),
                )

        # process incoming messages
        async for msg in ws_response:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = msg.json(loads=ujson.loads)
                msg = data["message"]
                msg_details = data["message_details"]
            except (KeyError, ValueError):
                await async_send_message(
                    "error",
                    'commands must be issued in json format \
                        {"message": "command", "message_details":" optional details"}',
                )
                continue
            if not authenticated and not msg == "login":
                # make sure client is authenticated
                await async_send_message("error", "authentication required")
            elif msg == "login" and isinstance(msg_details, str):
                # handle login with token
                try:
                    token_info = jwt.decode(msg_details, mass.web.device_id)
                    await async_send_message("login", token_info)
                    authenticated = True
                except jwt.InvalidTokenError as exc:
                    await async_send_message(
                        "error", "Invalid authorization token, " + str(exc)
                    )
                    authenticated = False
            elif msg == "login" and isinstance(msg_details, dict):
                # handle login with username/password
                token_info = await async_get_token(
                    mass, msg_details["username"], msg_details["password"]
                )
                if token_info:
                    await async_send_message("login", token_info)
                    authenticated = True
                else:
                    await async_send_message("error", "Invalid credentials")
                    authenticated = False
            elif msg in ws_commands:
                res = await ws_commands[msg](mass, msg_details)
                if res is not None:
                    await async_send_message(res)
            elif msg == "add_event_listener":
                _callbacks.append(
                    mass.add_event_listener(async_send_message, msg_details)
                )
                await async_send_message("event listener subscribed", msg_details)
            else:
                # simply echo the message on the eventbus
                request.app["mass"].signal_event(msg, msg_details)
    finally:
        LOGGER.debug("Websocket disconnected")
        for remove_callback in _callbacks:
            remove_callback()
    return ws_response


@ws_command("players")
async def async_players(mass: MusicAssistantType, msg_details: dict):
    """Return players."""
    if msg_details and msg_details.get("player_id"):
        return mass.players.get_player_state(msg_details["player_id"])
    return mass.players.player_states


@ws_command("tracks")
async def tracks(mass: MusicAssistantType, msg_details: dict):
    """Return tracks."""
    if msg_details and msg_details.get("item_id"):
        return await mass.music.async_get_track(msg_details["item_id"])
    return await mass.music.async_get_library_tracks()


@ws_command("albums")
async def albums(mass: MusicAssistantType, msg_details: dict):
    """Return albums."""
    if msg_details and msg_details.get("item_id"):
        return await mass.music.async_get_album(msg_details["item_id"])
    return await mass.music.async_get_library_albums()


@ws_command("artists")
async def artists(mass: MusicAssistantType, msg_details: dict):
    """Return artists."""
    if msg_details and msg_details.get("item_id"):
        return await mass.music.async_get_artist(msg_details["item_id"])
    return await mass.music.async_get_library_artists()


@ws_command("playlists")
async def playlists(mass: MusicAssistantType, msg_details: dict):
    """Return playlists."""
    if msg_details and msg_details.get("item_id"):
        return await mass.music.async_get_playlist(msg_details["item_id"])
    return await mass.music.async_get_library_playlists()


@ws_command("radios")
async def radios(mass: MusicAssistantType, msg_details: dict):
    """Return radios."""
    if msg_details and msg_details.get("item_id"):
        return await mass.music.async_get_radio(msg_details["item_id"])
    return await mass.music.async_get_library_radios()


@ws_command("player_command")
async def async_player_command(mass: MusicAssistantType, msg_details: dict):
    """Handle player command."""
    player_id = msg_details.get("player_id")
    cmd = msg_details.get("cmd")
    cmd_args = msg_details.get("cmd_args")
    player_cmd = getattr(mass.players, f"async_cmd_{cmd}", None)
    if player_cmd and cmd_args is not None:
        result = await player_cmd(player_id, cmd_args)
    elif player_cmd:
        result = await player_cmd(player_id)
    msg_details = {"cmd": cmd, "result": result}
    return msg_details
