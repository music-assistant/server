"""Shared helpers for authenticating Sendspin client capabilities."""

from __future__ import annotations

import json
from typing import Final

from music_assistant_models.auth import UserRole

GUEST_SENDSPIN_ROLES: Final = ("player@v1",)


def get_sendspin_role_restriction(role: str) -> tuple[str, ...] | None:
    """
    Return the Sendspin roles an authenticated user may advertise.

    :param role: The authenticated user's role.
    :return: Exact allowed roles, or None when messages may pass through unchanged.
    """
    return GUEST_SENDSPIN_ROLES if role == UserRole.GUEST else None


def restrict_sendspin_client_hello_roles(raw_message: str, allowed_roles: tuple[str, ...]) -> str:
    """
    Restrict the roles advertised by a structurally valid Sendspin client hello.

    :param raw_message: Raw text message received from the client.
    :param allowed_roles: Exact roles the client may advertise.
    :return: The rewritten hello or the original message when it is not a valid hello.
    """
    try:
        message = json.loads(raw_message)
    except json.JSONDecodeError:
        return raw_message
    if (
        not isinstance(message, dict)
        or message.get("type") != "client/hello"
        or not isinstance((payload := message.get("payload")), dict)
    ):
        return raw_message
    payload["supported_roles"] = list(allowed_roles)
    return json.dumps(message, separators=(",", ":"))
