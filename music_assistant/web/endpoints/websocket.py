"""Websocket API endpoint."""

import logging
from asyncio import CancelledError

import jwt
import orjson
from aiohttp import WSMsgType
from aiohttp.web import Request, RouteTableDef, WebSocketResponse
from music_assistant.helpers.util import json_serializer

routes = RouteTableDef()

LOGGER = logging.getLogger("websocket")


@routes.get("/ws")
async def async_websocket_handler(request: Request):
    """Handle websockets connection."""
    ws_response = None
    authenticated = False
    remove_callbacks = []
    try:
        ws_response = WebSocketResponse()
        await ws_response.prepare(request)

        # callback for internal events
        async def async_send_message(msg, msg_details=None):
            if hasattr(msg_details, "to_dict"):
                msg_details = msg_details.to_dict()
            ws_msg = {"message": msg, "message_details": msg_details}
            await ws_response.send_str(json_serializer(ws_msg).decode())

        # process incoming messages
        async for msg in ws_response:
            if msg.type != WSMsgType.TEXT:
                # not sure when/if this happens but log it anyway
                LOGGER.warning(msg.data)
                continue
            try:
                data = msg.json(loads=orjson.loads)
            except orjson.JSONDecodeError:
                await async_send_message(
                    "error",
                    'commands must be issued in json format \
                        {"message": "command", "message_details":" optional details"}',
                )
                continue
            msg = data.get("message")
            msg_details = data.get("message_details")
            if not authenticated and not msg == "login":
                # make sure client is authenticated
                await async_send_message("error", "authentication required")
            elif msg == "login" and msg_details:
                # authenticate with token
                try:
                    token_info = jwt.decode(
                        msg_details, request.app["mass"].web.device_id
                    )
                except jwt.InvalidTokenError as exc:
                    LOGGER.exception(exc, exc_info=exc)
                    error_msg = "Invalid authorization token, " + str(exc)
                    await async_send_message("error", error_msg)
                else:
                    authenticated = True
                    await async_send_message("login", token_info)
            elif msg == "add_event_listener":
                remove_callbacks.append(
                    request.app["mass"].add_event_listener(
                        async_send_message, msg_details
                    )
                )
                await async_send_message("event listener subscribed", msg_details)
            elif msg == "player_command":
                player_id = msg_details.get("player_id")
                cmd = msg_details.get("cmd")
                cmd_args = msg_details.get("cmd_args")
                player_cmd = getattr(
                    request.app["mass"].players, f"async_cmd_{cmd}", None
                )
                if player_cmd and cmd_args is not None:
                    result = await player_cmd(player_id, cmd_args)
                elif player_cmd:
                    result = await player_cmd(player_id)
                msg_details = {"cmd": cmd, "result": result}
                await async_send_message("player_command_result", msg_details)
            else:
                # simply echo the message on the eventbus
                request.app["mass"].signal_event(msg, msg_details)
    except (AssertionError, CancelledError):
        LOGGER.debug("Websocket disconnected")
    finally:
        for remove_callback in remove_callbacks:
            remove_callback()
    return ws_response
