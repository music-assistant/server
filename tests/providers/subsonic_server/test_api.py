"""Tests for the Subsonic API request handling."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from multidict import MultiDict
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import (
    InsufficientPermissions,
    MediaNotFoundError,
    MusicAssistantError,
)

from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.providers.subsonic_server.api import SubsonicApi
from music_assistant.providers.subsonic_server.auth import derive_app_password

SECRET = "0" * 64
ADMIN = User(user_id="admin-id", username="admin", role=UserRole.ADMIN)
MEMBER = User(user_id="member-id", username="member", role=UserRole.USER)
GUEST = User(user_id="guest-id", username="guest", role=UserRole.GUEST)
USERS = {user.username: user for user in (ADMIN, MEMBER, GUEST)}


def _create_api(jukebox_player_id: str | None = "sonos_living_room") -> SubsonicApi:
    provider = MagicMock()
    provider.mass.version = "2.11.0"
    provider.mass.webserver.auth.get_user_by_username = AsyncMock(side_effect=USERS.get)
    provider.get_app_password.side_effect = lambda user_id: derive_app_password(SECRET, user_id)
    provider.jukebox_player_id = jukebox_player_id
    return SubsonicApi(provider)


def _credentials(user: User, salt: str = "c19b2d") -> dict[str, str]:
    password = derive_app_password(SECRET, user.user_id)
    token = hashlib.md5((password + salt).encode(), usedforsecurity=False).hexdigest()
    return {"u": user.username, "t": token, "s": salt, "c": "test", "v": "1.16.1", "f": "json"}


def _request(
    endpoint: str,
    query: dict[str, str],
    method: str = "GET",
    form: dict[str, str] | None = None,
) -> MagicMock:
    request = MagicMock()
    request.path = f"/rest/{endpoint}"
    request.method = method
    request.query = MultiDict(query)
    request.content_type = "application/x-www-form-urlencoded" if form is not None else ""
    request.post = AsyncMock(return_value=MultiDict(form or {}))
    return request


async def _call(api: SubsonicApi, request: MagicMock) -> dict[str, Any]:
    response = await api.handle_request(request)
    assert isinstance(response, web.Response)
    assert response.text is not None
    body: dict[str, Any] = json.loads(response.text)["subsonic-response"]
    return body


@pytest.mark.parametrize("endpoint", ["ping", "ping.view"])
async def test_ping_with_token_auth(endpoint: str) -> None:
    """Ping answers ok with the OpenSubsonic markers, with or without the .view suffix."""
    body = await _call(_create_api(), _request(endpoint, _credentials(MEMBER)))
    assert body["status"] == "ok"
    assert body["openSubsonic"] is True
    assert body["type"] == "music-assistant"
    assert body["serverVersion"] == "2.11.0"


async def test_wrong_password_is_rejected() -> None:
    """A token computed from another password gives error 40."""
    query = _credentials(MEMBER) | {"t": "0" * 32}
    body = await _call(_create_api(), _request("ping", query))
    assert body["status"] == "failed"
    assert body["error"]["code"] == 40


async def test_unknown_user_is_rejected() -> None:
    """An unknown username gives the same error as a wrong password."""
    query = _credentials(MEMBER) | {"u": "nobody"}
    body = await _call(_create_api(), _request("ping", query))
    assert body["error"]["code"] == 40


async def test_missing_username() -> None:
    """A request without ``u`` gives error 10."""
    query = _credentials(MEMBER)
    del query["u"]
    body = await _call(_create_api(), _request("ping", query))
    assert body["error"]["code"] == 10


async def test_api_key_is_not_supported() -> None:
    """API key authentication is refused with error 42."""
    body = await _call(_create_api(), _request("ping", {"apiKey": "key", "f": "json"}))
    assert body["error"]["code"] == 42


async def test_credentials_in_post_form() -> None:
    """Parameters sent as a POST form are honoured (formPost extension)."""
    form = _credentials(MEMBER)
    body = await _call(_create_api(), _request("ping", {}, method="POST", form=form))
    assert body["status"] == "ok"


async def test_unsupported_endpoint() -> None:
    """An endpoint that is not implemented gives error 0 after authentication."""
    body = await _call(_create_api(), _request("download", _credentials(MEMBER) | {"id": "1"}))
    assert body["error"]["code"] == 0


async def test_get_license() -> None:
    """The license is always valid."""
    body = await _call(_create_api(), _request("getLicense", _credentials(MEMBER)))
    assert body["license"] == {"valid": True}


async def test_get_open_subsonic_extensions() -> None:
    """The supported extensions are listed with their versions."""
    body = await _call(_create_api(), _request("getOpenSubsonicExtensions", _credentials(MEMBER)))
    assert body["openSubsonicExtensions"] == [
        {"name": "formPost", "versions": [1]},
        {"name": "songLyrics", "versions": [1]},
    ]


async def test_get_user_roles_of_member() -> None:
    """A member may manage playlists and use the jukebox, but is not an admin."""
    query = _credentials(MEMBER) | {"username": "member"}
    body = await _call(_create_api(), _request("getUser", query))
    user = body["user"]
    assert user["username"] == "member"
    assert user["adminRole"] is False
    assert user["playlistRole"] is True
    assert user["jukeboxRole"] is True
    assert user["streamRole"] is False
    assert user["downloadRole"] is False


async def test_get_user_without_jukebox_player() -> None:
    """Without a configured jukebox player nobody gets the jukebox role."""
    query = _credentials(ADMIN) | {"username": "admin"}
    body = await _call(_create_api(jukebox_player_id=None), _request("getUser", query))
    assert body["user"]["adminRole"] is True
    assert body["user"]["jukeboxRole"] is False


async def test_guest_has_no_playlist_role() -> None:
    """A guest may not edit playlists."""
    query = _credentials(GUEST) | {"username": "guest"}
    body = await _call(_create_api(), _request("getUser", query))
    assert body["user"]["playlistRole"] is False


async def test_member_may_not_read_other_users() -> None:
    """Reading another user requires the users.read scope."""
    query = _credentials(MEMBER) | {"username": "admin"}
    body = await _call(_create_api(), _request("getUser", query))
    assert body["error"]["code"] == 50


async def test_admin_reads_other_user() -> None:
    """An admin may read another user."""
    query = _credentials(ADMIN) | {"username": "member"}
    body = await _call(_create_api(), _request("getUser", query))
    assert body["user"]["username"] == "member"


async def test_get_user_requires_username() -> None:
    """GetUser without ``username`` gives error 10."""
    body = await _call(_create_api(), _request("getUser", _credentials(MEMBER)))
    assert body["error"]["code"] == 10


async def test_current_user_is_set_during_handler_only() -> None:
    """The handler runs as the authenticated user and the context is cleared afterwards."""
    api = _create_api()
    seen: list[User | None] = []

    async def spy(_request: Any) -> dict[str, Any]:
        seen.append(get_current_user())
        return {}

    api._endpoints["ping"] = spy
    await _call(api, _request("ping", _credentials(MEMBER)))
    assert seen == [MEMBER]
    assert get_current_user() is None


async def test_responses_allow_any_origin() -> None:
    """Every response carries the CORS headers browser clients need."""
    response = await _create_api().handle_request(_request("ping", _credentials(MEMBER)))
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    error = await _create_api().handle_request(_request("ping", {"f": "json"}))
    assert error.headers["Access-Control-Allow-Origin"] == "*"


async def test_preflight_request() -> None:
    """A CORS preflight is answered without authentication and echoes the requested headers."""
    request = _request("getAlbum", {}, method="OPTIONS")
    request.headers = {"Access-Control-Request-Headers": "content-type"}
    response = await _create_api().handle_request(request)
    assert response.status == 204
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert response.headers["Access-Control-Allow-Headers"] == "content-type"
    assert "POST" in response.headers["Access-Control-Allow-Methods"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (MediaNotFoundError("gone"), 70),
        (InsufficientPermissions("no"), 50),
        (MusicAssistantError("broken"), 0),
    ],
)
async def test_music_assistant_errors_become_subsonic_errors(
    error: MusicAssistantError, code: int
) -> None:
    """Errors raised by Music Assistant controllers are answered as Subsonic errors."""
    api = _create_api()

    async def failing(_request: Any) -> dict[str, Any]:
        raise error

    api._endpoints["ping"] = failing
    response = await api.handle_request(_request("ping", _credentials(MEMBER)))
    assert isinstance(response, web.Response)
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert response.text is not None
    assert json.loads(response.text)["subsonic-response"]["error"]["code"] == code
