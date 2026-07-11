"""Helpers for handling Sendspin protocol messages."""

from __future__ import annotations

import json
from typing import Any

SENDSPIN_AUDIO_MESSAGE_TYPE = 4
SENDSPIN_BINARY_HEADER_SIZE = 9

_SAFE_SERVER_MESSAGE_TYPES = frozenset(
    {
        "group/update",
        "server/command",
        "server/hello",
        "server/time",
    }
)


def get_sendspin_client_id(message: str) -> str | None:
    """
    Return the client ID from a Sendspin authentication or hello message.

    :param message: Serialized Sendspin client message.
    :return: The client ID, if present.
    """
    try:
        data: Any = json.loads(message)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("type") == "auth":
        client_id = data.get("client_id")
    elif data.get("type") == "client/hello" and isinstance(data.get("payload"), dict):
        client_id = data["payload"].get("client_id")
    else:
        return None
    return client_id if isinstance(client_id, str) and client_id else None


def filter_audio_only_sendspin_message(message: str | bytes) -> str | bytes | None:
    """
    Remove non-audio role data from an outbound Sendspin message.

    :param message: Serialized Sendspin server message.
    :return: The safe message, or ``None`` when it must not be forwarded.
    """
    if isinstance(message, bytes):
        if len(message) < SENDSPIN_BINARY_HEADER_SIZE:
            return None
        return message if message[0] == SENDSPIN_AUDIO_MESSAGE_TYPE else None

    try:
        data: Any = json.loads(message)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("type"), str):
        return None

    message_type = data["type"]
    if message_type in _SAFE_SERVER_MESSAGE_TYPES:
        return message
    if not isinstance(data.get("payload"), dict):
        return None

    payload = data["payload"]
    if message_type == "server/state":
        controller = payload.get("controller")
        if controller is None:
            return None
        data["payload"] = {"controller": controller}
    elif message_type == "stream/start":
        player = payload.get("player")
        if player is None:
            return None
        data["payload"] = {"player": player}
    elif message_type in {"stream/clear", "stream/end"}:
        roles = payload.get("roles")
        if roles is not None and (not isinstance(roles, list) or "player" not in roles):
            return None
        data["payload"] = {"roles": ["player"]}
    else:
        return None
    return json.dumps(data, separators=(",", ":"))
