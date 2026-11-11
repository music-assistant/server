"""Helpers to talk to the Home Assistant Supervisor API."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientTimeout

from music_assistant.helpers.json import json_loads

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

SUPERVISOR_URL = "http://supervisor"


class SupervisorError(Exception):
    """The Supervisor answered a request with an error, or did not answer at all."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """
        Initialize the error.

        :param message: The message of the Supervisor, or why it could not be reached.
        :param status: The HTTP status of the answer, None when there was no answer.
        """
        super().__init__(message)
        self.message = message
        self.status = status


def supervisor_token() -> str | None:
    """Return the token of this app for the Supervisor API, None when not running under one."""
    return os.environ.get("SUPERVISOR_TOKEN")


async def supervisor_request(
    mass: MusicAssistant,
    method: str,
    path: str,
    json_data: dict[str, Any] | None = None,
    timeout: float = 10,
) -> Any:
    """
    Send a request to the Supervisor API and return the data of its answer.

    :param mass: The Music Assistant instance.
    :param method: The HTTP method of the request.
    :param path: The path of the API endpoint, starting with a slash.
    :param json_data: The body of the request.
    :param timeout: How long to wait for the answer, in seconds.
    :raises SupervisorError: When the Supervisor answers with an error or can not be reached.
    """
    if (token := supervisor_token()) is None:
        msg = "Not running under a Supervisor"
        raise SupervisorError(msg)
    try:
        async with mass.http_session_no_ssl.request(
            method,
            f"{SUPERVISOR_URL}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=json_data,
            timeout=ClientTimeout(total=timeout),
        ) as response:
            if response.ok:
                body = await response.json()
                return body.get("data") if isinstance(body, dict) else None
            text = (await response.text()).strip()
            status = response.status
            message = _error_message(text) or response.reason or f"HTTP {status}"
    except (ClientError, TimeoutError) as err:
        raise SupervisorError(str(err) or type(err).__name__) from err
    raise SupervisorError(message, status)


def _error_message(text: str) -> str:
    """Return the message in the error answer of the Supervisor, which may not be JSON."""
    try:
        body = json_loads(text)
    except ValueError:
        # a request the Supervisor refuses before handling it gets a plain text answer
        return text
    if isinstance(body, dict) and isinstance(message := body.get("message"), str):
        return message
    return text
