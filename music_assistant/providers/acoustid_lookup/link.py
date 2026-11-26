"""
Linking Music Assistant to a user's AcoustID account.

AcoustID hands out a user key together with a link the user opens to approve it. The key
only works for lookups once it is approved, and the user can withdraw it on AcoustID at
any time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.errors import (
    InvalidDataError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)

from music_assistant.helpers.app_vars import app_var
from music_assistant.helpers.datetime import from_iso_string, utc
from music_assistant.helpers.throttle_retry import parse_retry_after

if TYPE_CHECKING:
    from aiohttp import ClientSession

ACOUSTID_REQUEST_LINK_URL = "https://api.acoustid.org/v2/user/request_link"
ACOUSTID_LINK_STATUS_URL = "https://api.acoustid.org/v2/user/link_status"
ACOUSTID_ERROR_TOO_MANY_REQUESTS = 14
# AcoustID accepts a device name of at most this many characters
MAX_DEVICE_LENGTH = 100
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class LinkState(StrEnum):
    """What AcoustID reports about a user key handed out through a link."""

    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"
    EXPIRED = "expired"
    REVOKED = "revoked"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LinkRequest:
    """A user key waiting for the user's approval on AcoustID."""

    # user_key: the key lookups can send once the user approved it
    user_key: str
    # code: the short code AcoustID shows the user for this request
    code: str
    # url: the page the user opens to approve the request
    url: str
    # expires_in: seconds the user has left to approve
    expires_in: float


async def request_link(http_session: ClientSession, device: str) -> LinkRequest:
    """
    Ask AcoustID for a new user key and the link the user approves it with.

    AcoustID limits how often this may be called, so only call it when the user asked to
    connect.

    :param http_session: The aiohttp session to use.
    :param device: Name AcoustID shows the user for this Music Assistant server.
    :raises RateLimited: When AcoustID received too many requests.
    :raises ResourceTemporarilyUnavailable: When AcoustID could not be reached.
    :raises InvalidDataError: When AcoustID did not hand out a usable link.
    """
    payload = await _request(
        http_session,
        "POST",
        ACOUSTID_REQUEST_LINK_URL,
        data={"device": device[:MAX_DEVICE_LENGTH]},
    )
    user = payload.get("user")
    link = user.get("link") if isinstance(user, dict) else None
    if not isinstance(user, dict) or not isinstance(link, dict):
        raise InvalidDataError("AcoustID did not return a link")
    user_key, code, url, expires = (
        user.get("apikey"),
        link.get("code"),
        link.get("url"),
        link.get("expires"),
    )
    if not all(isinstance(value, str) and value for value in (user_key, code, url, expires)):
        raise InvalidDataError("AcoustID returned an incomplete link")
    try:
        expires_in = (from_iso_string(str(expires)) - utc()).total_seconds()
    except (ValueError, TypeError) as err:
        raise InvalidDataError("AcoustID returned a link without a usable expiry") from err
    return LinkRequest(
        user_key=str(user_key), code=str(code), url=str(url), expires_in=max(0.0, expires_in)
    )


async def link_status(http_session: ClientSession, user_key: str) -> LinkState:
    """
    Return what AcoustID reports about the given user key.

    :param http_session: The aiohttp session to use.
    :param user_key: The user key to check.
    :raises RateLimited: When AcoustID received too many requests.
    :raises ResourceTemporarilyUnavailable: When AcoustID could not be reached.
    :raises InvalidDataError: When AcoustID did not report a known state.
    """
    payload = await _request(http_session, "GET", ACOUSTID_LINK_STATUS_URL, user_key=user_key)
    key = payload.get("key")
    state = key.get("state") if isinstance(key, dict) else None
    if not isinstance(state, str) or state not in LinkState:
        raise InvalidDataError(f"AcoustID reported an unexpected link state: {state!r}")
    return LinkState(state)


async def _request(
    http_session: ClientSession,
    method: str,
    url: str,
    *,
    user_key: str | None = None,
    data: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Call an AcoustID account endpoint and return its reply once AcoustID accepted the call."""
    # the keys travel as headers so they stay out of the request URL
    headers = {"Acoustid-App-Key": str(app_var("acoustid_api_key"))}
    if user_key:
        headers["Acoustid-User-Key"] = user_key
    try:
        async with http_session.request(
            method, url, headers=headers, data=data, timeout=REQUEST_TIMEOUT
        ) as response:
            if response.status == 429:
                raise RateLimited(
                    "AcoustID rate limit",
                    backoff_time=parse_retry_after(response.headers.get("Retry-After")),
                )
            if response.status >= 500:
                raise ResourceTemporarilyUnavailable("AcoustID server error")
            payload = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as err:
        msg = f"AcoustID could not be reached ({type(err).__name__})"
        raise ResourceTemporarilyUnavailable(msg) from err
    except json.JSONDecodeError as err:
        raise InvalidDataError("AcoustID did not answer with JSON") from err
    if isinstance(payload, dict) and payload.get("status") == "ok":
        return payload
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        raise InvalidDataError("AcoustID refused the request")
    if error.get("code") == ACOUSTID_ERROR_TOO_MANY_REQUESTS:
        raise RateLimited("AcoustID rate limit")
    raise InvalidDataError(f"AcoustID refused the request: {error.get('message', '')}")
