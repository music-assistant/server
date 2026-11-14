"""
ARD account login and account data for the ARD Audiothek provider.

ARD only accepts password logins from its own (captcha protected) login page, so the
account is linked with the device login the ARD Sounds TV app uses: the user confirms a
short code on accounts.ard.de in their browser, after which a Firebase refresh token is
handed out that keeps the session alive.

The account's lists (subscribed podcasts and the play history) live in ARD's Firestore
database, where the ARD Sounds apps keep them.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import jwt
from music_assistant_models.errors import LoginFailed

from music_assistant.helpers.datetime import from_iso_string, utc

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from datetime import datetime

    from aiohttp import ClientSession

ARD_ACCOUNTS_URL = "https://accounts.ard.de"
# "at" is the client id of the ARD Sounds TV app (tv.ardaudiothek.de)
DEVICE_CODE_URL = f"{ARD_ACCOUNTS_URL}/device/code?clientId=at"
REFRESH_API_URL = "https://securetoken.googleapis.com/v1/token"
SECURE_TOKEN_KEY = "AIzaSyAL4nnUjVtYRYguHLBcBo7BHTqOyPN-lx0"  # noqa: S105 - public API key of the ARD Sounds TV app
FIRESTORE_DOCUMENTS_URL = (
    "https://firestore.googleapis.com/v1/projects/ardmt-prod/databases/(default)/documents"
)
MIN_POLL_INTERVAL = 3
HEADERS = {"User-Agent": "Music Assistant"}


@dataclass(frozen=True)
class PlayHistoryEntry:
    """The playback progress of an episode in the account's play history."""

    completed: bool
    playtime: float
    updated_on: datetime | None


@dataclass(frozen=True)
class ArdTokens:
    """The tokens and account details of a signed in ARD account."""

    id_token: str
    refresh_token: str
    user_id: str
    display_name: str
    email: str
    expires_at: float


async def request_device_code(session: ClientSession) -> dict[str, Any]:
    """
    Request a new device login code from ARD.

    The result holds the ``user_code`` to show, the ``verification_uri`` to open, the
    ``token_uri`` to poll and the code's lifetime (``expires_in``) in seconds.

    :param session: The aiohttp session to use.
    :raises LoginFailed: When ARD does not hand out a usable code.
    """
    async with session.get(DEVICE_CODE_URL, headers=HEADERS) as response:
        data = await response.json(content_type=None)
    if not isinstance(data, dict) or not all(
        data.get(key) for key in ("user_code", "verification_uri", "token_uri")
    ):
        raise LoginFailed("ARD did not return a device login code")
    return data


async def poll_device_token(session: ClientSession, device: dict[str, Any]) -> ArdTokens:
    """
    Poll ARD until the user confirmed the device login code, returning the tokens.

    Polls indefinitely at the advised interval; the caller bounds the wait.

    :param session: The aiohttp session to use.
    :param device: The device login code from :func:`request_device_code`.
    :raises LoginFailed: When ARD rejects the login.
    """
    interval = max(int(device.get("interval") or 5), MIN_POLL_INTERVAL)
    while True:
        await asyncio.sleep(interval)
        async with session.get(str(device["token_uri"]), headers=HEADERS) as response:
            status = response.status
            text = await response.text()
        # a transient server error must not abort a login the user may be confirming
        if status == 425 or status >= 500 or "authorization_pending" in text:
            continue
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if not isinstance(data, dict) or not data.get("idToken") or not data.get("refreshToken"):
            raise LoginFailed(f"ARD device login failed (HTTP {status})")
        return _tokens_from(str(data["idToken"]), str(data["refreshToken"]))


async def refresh_tokens(session: ClientSession, refresh_token: str) -> ArdTokens:
    """
    Exchange a refresh token for a fresh ID token.

    :param session: The aiohttp session to use.
    :param refresh_token: The refresh token of the signed in account.
    :raises LoginFailed: When the refresh token is no longer accepted.
    """
    async with session.post(
        f"{REFRESH_API_URL}?key={SECURE_TOKEN_KEY}",
        headers=HEADERS,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
    ) as response:
        data = await response.json(content_type=None)
    if not isinstance(data, dict) or not data.get("id_token"):
        message = data.get("error", {}).get("message", "") if isinstance(data, dict) else ""
        raise LoginFailed(f"Refreshing the ARD login failed: {message or 'no token returned'}")
    return _tokens_from(str(data["id_token"]), str(data.get("refresh_token") or refresh_token))


async def get_subscribed_podcast_ids(
    session: ClientSession, id_token: str, user_id: str
) -> list[str]:
    """
    Return the ids of the podcasts the account subscribed to in ARD Sounds.

    :param session: The aiohttp session to use.
    :param id_token: The ID token of the signed in account.
    :param user_id: The user id of the signed in account.
    :raises LoginFailed: When the token is not accepted.
    """
    path = f"audiothek/{user_id}/lists/groupings/items"
    # the document id is the podcast id
    return [doc_id async for doc_id, _ in _list_documents(session, id_token, path)]


async def get_play_history(
    session: ClientSession, id_token: str, user_id: str
) -> dict[str, PlayHistoryEntry]:
    """
    Return the play history of the account per episode asset id.

    :param session: The aiohttp session to use.
    :param id_token: The ID token of the signed in account.
    :param user_id: The user id of the signed in account.
    :raises LoginFailed: When the token is not accepted.
    """
    path = f"audiothek/{user_id}/lists/playhistory/items"
    history: dict[str, PlayHistoryEntry] = {}
    # the document id is the episode's asset id
    async for asset_id, fields in _list_documents(session, id_token, path):
        playtime = fields.get("playtime", {})
        seconds = playtime.get("integerValue", playtime.get("doubleValue", 0))
        history[asset_id] = PlayHistoryEntry(
            completed=bool(fields.get("completed", {}).get("booleanValue", False)),
            playtime=float(seconds),
            updated_on=_parse_timestamp(fields.get("updatedOn", {}).get("timestampValue")),
        )
    return history


async def add_subscription(
    session: ClientSession, id_token: str, user_id: str, podcast_id: str
) -> None:
    """
    Subscribe the account to a podcast in ARD Sounds.

    :param session: The aiohttp session to use.
    :param id_token: The ID token of the signed in account.
    :param user_id: The user id of the signed in account.
    :param podcast_id: The id of the podcast to subscribe to.
    """
    url = f"{FIRESTORE_DOCUMENTS_URL}/audiothek/{user_id}/lists/groupings/items/{podcast_id}"
    body = {
        "fields": {
            "id": {"stringValue": podcast_id},
            "type": {"stringValue": "grouping"},
            "updatedOn": {"timestampValue": _firestore_now()},
        }
    }
    async with session.patch(url, headers=_auth_headers(id_token), json=body) as response:
        data = await response.json(content_type=None)
        status = response.status
    _raise_for_firestore_error(status, data, "Subscribing to the ARD podcast")


async def remove_subscription(
    session: ClientSession, id_token: str, user_id: str, podcast_id: str
) -> None:
    """
    Unsubscribe the account from a podcast in ARD Sounds.

    :param session: The aiohttp session to use.
    :param id_token: The ID token of the signed in account.
    :param user_id: The user id of the signed in account.
    :param podcast_id: The id of the podcast to unsubscribe from.
    """
    url = f"{FIRESTORE_DOCUMENTS_URL}/audiothek/{user_id}/lists/groupings/items/{podcast_id}"
    async with session.delete(url, headers=_auth_headers(id_token)) as response:
        data = await response.json(content_type=None)
        status = response.status
    _raise_for_firestore_error(status, data, "Unsubscribing from the ARD podcast")


async def set_play_history_entry(
    session: ClientSession,
    id_token: str,
    user_id: str,
    episode_id: str,
    playtime: int,
    completed: bool,
) -> None:
    """
    Store the playback progress of an episode in the account's play history.

    :param session: The aiohttp session to use.
    :param id_token: The ID token of the signed in account.
    :param user_id: The user id of the signed in account.
    :param episode_id: The id of the played episode.
    :param playtime: The playback position in seconds.
    :param completed: Whether the episode was played to the end.
    """
    url = f"{FIRESTORE_DOCUMENTS_URL}/audiothek/{user_id}/lists/playhistory/items/{episode_id}"
    # without an update mask the whole document is replaced, like the ARD Sounds apps do
    body = {
        "fields": {
            "id": {"stringValue": episode_id},
            "type": {"stringValue": "aod"},
            "playtime": {"integerValue": str(playtime)},
            "completed": {"booleanValue": completed},
            "updatedOn": {"timestampValue": _firestore_now()},
        }
    }
    async with session.patch(url, headers=_auth_headers(id_token), json=body) as response:
        data = await response.json(content_type=None)
        status = response.status
    _raise_for_firestore_error(status, data, "Storing the ARD play history")


async def remove_play_history_entry(
    session: ClientSession, id_token: str, user_id: str, episode_id: str
) -> None:
    """
    Remove an episode from the account's play history, so it counts as never played.

    :param session: The aiohttp session to use.
    :param id_token: The ID token of the signed in account.
    :param user_id: The user id of the signed in account.
    :param episode_id: The id of the episode to remove.
    """
    url = f"{FIRESTORE_DOCUMENTS_URL}/audiothek/{user_id}/lists/playhistory/items/{episode_id}"
    async with session.delete(url, headers=_auth_headers(id_token)) as response:
        data = await response.json(content_type=None)
        status = response.status
    _raise_for_firestore_error(status, data, "Removing the episode from the ARD play history")


async def _list_documents(
    session: ClientSession, id_token: str, path: str
) -> AsyncGenerator[tuple[str, dict[str, Any]]]:
    """Yield (document id, fields) of all documents in a Firestore collection."""
    url = f"{FIRESTORE_DOCUMENTS_URL}/{path}"
    page_token = ""
    while True:
        params = {"pageSize": "300", **({"pageToken": page_token} if page_token else {})}
        async with session.get(url, headers=_auth_headers(id_token), params=params) as response:
            data = await response.json(content_type=None)
            status = response.status
        _raise_for_firestore_error(status, data, "Fetching the ARD account data")
        for doc in data.get("documents", []):
            if doc.get("name"):
                yield doc["name"].rsplit("/", 1)[-1], doc.get("fields", {})
        page_token = data.get("nextPageToken") or ""
        if not page_token:
            return


def _firestore_now() -> str:
    """Return the current time as a Firestore timestamp."""
    return utc().isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    """Parse a Firestore timestamp, which may carry more fractional digits than Python takes."""
    if not value:
        return None
    try:
        return from_iso_string(re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _auth_headers(id_token: str) -> dict[str, str]:
    """Return the request headers authorizing as the signed in account."""
    return {**HEADERS, "Authorization": f"Bearer {id_token}"}


def _raise_for_firestore_error(status: int, data: Any, action: str) -> None:
    """Raise when a Firestore request failed, LoginFailed when the token was rejected."""
    if status in (401, 403):
        raise LoginFailed(f"ARD rejected the account token (HTTP {status})")
    if not isinstance(data, dict) or "error" in data:
        message = data.get("error", {}).get("message", "") if isinstance(data, dict) else ""
        msg = f"{action} failed: {message or f'HTTP {status}'}"
        raise RuntimeError(msg)


def _tokens_from(id_token: str, refresh_token: str) -> ArdTokens:
    """Build the account tokens, reading the account details from the ID token's claims."""
    try:
        claims = jwt.decode(id_token, options={"verify_signature": False})
    except jwt.InvalidTokenError as err:
        raise LoginFailed("ARD returned an invalid token") from err
    user_id = claims.get("user_id") or claims.get("sub")
    if not user_id:
        raise LoginFailed("ARD returned a token without user id")
    return ArdTokens(
        id_token=id_token,
        refresh_token=refresh_token,
        user_id=str(user_id),
        display_name=str(claims.get("name") or ""),
        email=str(claims.get("email") or ""),
        expires_at=float(claims.get("exp") or 0),
    )
