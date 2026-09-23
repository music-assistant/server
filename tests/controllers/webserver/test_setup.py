"""Tests for the first-time setup: the page that hosts the account step and its endpoint."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import UserRole
from yarl import URL

from music_assistant.controllers.webserver import controller as controller_module
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.helpers.redirect_validation import build_code_redirect_url

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

INDEX_PATH = "/frontend/index.html"

ACCOUNT = {"username": "marcel", "password": "correct horse battery", "display_name": "Marcel"}


async def _start_webserver(mass: MusicAssistant) -> WebserverController:
    """
    Create a webserver controller with its authentication set up, as a server start does.

    :param mass: The minimal server to run the controller on; its database is what persists.
    """
    # creating the first user migrates playlog rows through the music controller,
    # which the minimal server does not run
    mass.music = MagicMock()
    mass.music.database.execute = AsyncMock()
    mass.music.database.commit = AsyncMock()
    webserver = WebserverController(mass)
    mass.webserver = webserver
    webserver.config = await mass.config.get_core_config("webserver")
    await webserver.auth.setup()
    # the frontend is not installed in the test environment; the index is only ever served
    webserver._index_path = INDEX_PATH
    return webserver


@pytest.fixture
async def webserver(mass_minimal: MusicAssistant) -> AsyncGenerator[WebserverController]:
    """
    Provide a webserver controller on a minimal server without any user.

    :param mass_minimal: The minimal server to run the controller on.
    """
    webserver = await _start_webserver(mass_minimal)
    try:
        yield webserver
    finally:
        await webserver.auth.close()


def _request(
    mass: MusicAssistant,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    raw_body: bytes | None = None,
) -> web.Request:
    """
    Build a request for a handler, with a body when one is given.

    :param mass: The server the request is handled by.
    :param method: The HTTP method.
    :param path: The path, query string included.
    :param body: The JSON body of the request, if any.
    :param raw_body: The body as sent, for one that is not valid JSON.
    """
    app = web.Application()
    app["mass"] = mass
    data = raw_body if raw_body is not None else json.dumps(body).encode() if body else None
    if data is None:
        return make_mocked_request(method, path, app=app)
    payload = StreamReader(MagicMock(), limit=2**16)
    payload.feed_data(data)
    payload.feed_eof()
    return make_mocked_request(
        method, path, headers={"Content-Type": "application/json"}, app=app, payload=payload
    )


async def _post_setup(webserver: WebserverController, body: dict[str, Any]) -> web.Response:
    """
    Post the account details to the setup endpoint.

    :param webserver: The controller handling the request.
    :param body: The JSON body to post.
    """
    return await webserver._handle_setup(_request(webserver.mass, "POST", "/setup", body))


def _served_app() -> AsyncMock:
    """Stand in for serving the frontend's index, which is what the setup page consists of."""
    return AsyncMock(return_value=web.Response(text="<the app>"))


def _text(response: web.StreamResponse) -> str:
    """
    Return the body of a response that carries one.

    :param response: The response a handler returned.
    """
    assert isinstance(response, web.Response)
    return response.text or ""


async def test_the_setup_page_serves_the_app_while_no_user_exists(
    webserver: WebserverController,
) -> None:
    """The setup page is the frontend itself, which opens its wizard on the account step."""
    with patch.object(webserver._server, "serve_static", _served_app()) as serve_static:
        response = await webserver._handle_setup_page(
            _request(webserver.mass, "GET", "/setup?return_url=musicassistant%3A%2F%2Fauth")
        )

    assert response.status == 200
    assert _text(response) == "<the app>"
    serve_static.assert_awaited_once_with(INDEX_PATH, ANY)


async def test_the_setup_page_refuses_an_untrusted_return_url(
    webserver: WebserverController,
) -> None:
    """The token is forwarded without a consent step, so an untrusted destination is refused."""
    with patch.object(webserver._server, "serve_static", _served_app()) as serve_static:
        response = await webserver._handle_setup_page(
            _request(webserver.mass, "GET", "/setup?return_url=https%3A%2F%2Fevil.example%2F")
        )

    assert response.status == 400
    serve_static.assert_not_awaited()


async def test_the_setup_page_is_closed_once_a_user_exists(
    webserver: WebserverController,
) -> None:
    """Once the admin exists the setup page says so instead of serving the app again."""
    await webserver.auth.create_user(username="marcel", role=UserRole.ADMIN)

    with patch.object(webserver._server, "serve_static", _served_app()) as serve_static:
        response = await webserver._handle_setup_page(_request(webserver.mass, "GET", "/setup"))

    assert response.status == 403
    assert "already been completed" in _text(response)
    serve_static.assert_not_awaited()


async def test_the_index_sends_a_fresh_server_to_the_setup_page(
    webserver: WebserverController,
) -> None:
    """Without a user the index redirects to the setup page."""
    with patch.object(webserver._server, "serve_static", _served_app()) as serve_static:
        response = await webserver._handle_index(_request(webserver.mass, "GET", "/"))

    assert response.status == 302
    assert response.headers["Location"] == "setup"
    serve_static.assert_not_awaited()


async def test_the_index_keeps_a_client_hand_back_on_its_way_to_the_setup_page(
    webserver: WebserverController,
) -> None:
    """The query travels along with the redirect, a return url with its own query included."""
    return_url = "https://companion.test/auth?device=phone&next=home"
    path = str(URL("/").with_query({"return_url": return_url, "device_name": "Companion"}))

    response = await webserver._handle_index(_request(webserver.mass, "GET", path))

    assert response.status == 302
    location = URL(response.headers["Location"])
    assert location.path == "setup"
    assert dict(location.query) == {"return_url": return_url, "device_name": "Companion"}


async def test_an_untrusted_return_url_is_refused_on_the_setup_page_it_is_forwarded_to(
    webserver: WebserverController,
) -> None:
    """The forwarded query lands on the setup page's own check, which refuses it there."""
    path = str(URL("/").with_query({"return_url": "https://evil.example/"}))
    redirect = await webserver._handle_index(_request(webserver.mass, "GET", path))
    assert redirect.status == 302

    with patch.object(webserver._server, "serve_static", _served_app()) as serve_static:
        response = await webserver._handle_setup_page(
            _request(webserver.mass, "GET", "/" + redirect.headers["Location"])
        )

    assert response.status == 400
    serve_static.assert_not_awaited()


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ({}, {}),
        ({"return_url": "musicassistant://auth"}, {"return_url": "musicassistant://auth"}),
        (
            {
                "return_url": "https://companion.test/auth?device=phone&next=home",
                "device_name": "Companion phone",
            },
            {
                "return_url": "https://companion.test/auth?device=phone&next=home",
                "device_name": "Companion phone",
            },
        ),
    ],
    ids=["plain", "return_url_only", "with_query_and_spaces"],
)
async def test_the_login_page_sends_a_fresh_server_to_the_setup_page_with_the_hand_back(
    webserver: WebserverController, query: dict[str, str], expected: dict[str, str]
) -> None:
    """
    A client that starts at the login page is sent on to the setup page with its hand-back kept.

    :param query: The query the client opened the login page with.
    :param expected: The hand-back the setup page is expected to receive.
    """
    path = str(URL("/login").with_query(query))

    response = await webserver._handle_login_page(_request(webserver.mass, "GET", path))

    assert response.status == 302
    location = URL(response.headers["Location"])
    assert location.path == "/setup"
    assert dict(location.query) == expected


async def test_the_index_serves_the_app_once_a_user_exists(
    webserver: WebserverController,
) -> None:
    """With the admin in place the index is the app, setup page or not."""
    await webserver.auth.create_user(username="marcel", role=UserRole.ADMIN)

    with patch.object(webserver._server, "serve_static", _served_app()) as serve_static:
        response = await webserver._handle_index(_request(webserver.mass, "GET", "/"))

    assert response.status == 200
    serve_static.assert_awaited_once()


@pytest.mark.parametrize(
    ("role", "gets_the_app"),
    [(UserRole.ADMIN, True), (UserRole.USER, False)],
    ids=["admin", "non_admin"],
)
async def test_ingress_never_sees_the_setup_page(
    webserver: WebserverController, role: str, gets_the_app: bool
) -> None:
    """
    Home Assistant Ingress never sends anyone to the setup page.

    The admin is created from the ingress headers, so an admin gets the app straight away
    and anyone else is told to ask an administrator.

    :param role: The role of the Home Assistant account behind the request.
    :param gets_the_app: Whether that account is served the app.
    """
    with (
        patch.object(controller_module, "is_request_from_ingress", return_value=True),
        patch.object(controller_module, "get_ha_user_role", AsyncMock(return_value=role)),
        patch.object(webserver._server, "serve_static", _served_app()) as serve_static,
    ):
        response = await webserver._handle_index(_request(webserver.mass, "GET", "/"))

    assert response.status != 302
    if gets_the_app:
        serve_static.assert_awaited_once()
    else:
        serve_static.assert_not_awaited()
        assert "Administrator permissions are required" in _text(response)


async def test_setup_creates_the_admin_and_hands_back_a_token(
    webserver: WebserverController,
) -> None:
    """The first account is the admin, named as asked, with a token to sign the app in."""
    response = await _post_setup(webserver, ACCOUNT)

    assert response.status == 200
    answer = json.loads(response.text or "")
    assert answer["success"] is True
    assert answer["user"]["username"] == "marcel"
    assert answer["user"]["display_name"] == "Marcel"
    assert answer["user"]["role"] == UserRole.ADMIN
    assert "redirect_to" not in answer
    # the token is the account's
    token_user = await webserver.auth.authenticate_with_token(answer["token"])
    assert token_user is not None
    assert token_user.user_id == answer["user"]["user_id"]
    assert webserver.auth.has_users


@pytest.mark.parametrize(
    "body",
    [
        {"username": "marcel", "password": "correct horse battery"},
        {**ACCOUNT, "display_name": None},
        {**ACCOUNT, "display_name": "  "},
    ],
    ids=["absent", "null", "blank"],
)
async def test_setup_leaves_the_display_name_empty_when_none_was_given(
    webserver: WebserverController, body: dict[str, Any]
) -> None:
    """
    A display name that was not given, or only spaces, is not stored as one.

    :param body: The details posted, without a usable display name.
    """
    response = await _post_setup(webserver, body)

    assert response.status == 200
    assert json.loads(response.text or "")["user"]["display_name"] is None


@pytest.mark.parametrize(
    "raw_body",
    [
        b"not json",
        b"[1, 2]",
        b'"hi"',
        b'{"username": 123456, "password": "correct horse battery"}',
        b'{"username": "marcel", "password": 12345678}',
        b'{"username": "marcel", "password": "correct horse battery", "display_name": {"a": 1}}',
        b'{"username": "marcel", "password": "correct horse battery", "display_name": false}',
        b'{"username": "marcel", "password": "correct horse battery", "display_name": 0}',
        b'{"username": "marcel", "password": "correct horse battery", "device_name": 5}',
    ],
    ids=[
        "not_json",
        "list",
        "string",
        "username_not_a_string",
        "password_not_a_string",
        "display_name_not_a_string",
        "display_name_false",
        "display_name_zero",
        "device_name_not_a_string",
    ],
)
async def test_setup_refuses_a_body_it_cannot_read(
    webserver: WebserverController, raw_body: bytes
) -> None:
    """
    A body that is not the expected object is a client error, and makes no account.

    :param raw_body: The body as the client sent it.
    """
    response = await webserver._handle_setup(
        _request(webserver.mass, "POST", "/setup", raw_body=raw_body)
    )

    assert response.status == 400
    assert not webserver.auth.has_users


@pytest.mark.parametrize("device_name", [None, ""], ids=["null", "empty"])
async def test_setup_names_the_token_itself_when_the_client_gave_no_name(
    webserver: WebserverController, device_name: str | None
) -> None:
    """
    A device name that was not given leaves the token named after the setup.

    :param device_name: What the client sent for the device name, if anything.
    """
    response = await _post_setup(webserver, {**ACCOUNT, "device_name": device_name})

    assert response.status == 200
    user_id = json.loads(response.text or "")["user"]["user_id"]
    rows = await webserver.auth.database.get_rows("auth_tokens", {"user_id": user_id})
    assert [row["name"] for row in rows] == ["Setup (Unknown)"]


async def test_setup_forwards_the_token_to_the_client_that_started_it(
    webserver: WebserverController,
) -> None:
    """A trusted client that started the setup is told where to pick its token up."""
    response = await _post_setup(
        webserver,
        {**ACCOUNT, "return_url": "musicassistant://auth", "device_name": "Companion"},
    )

    assert response.status == 200
    answer = json.loads(response.text or "")
    assert answer["redirect_to"] == build_code_redirect_url(
        "musicassistant://auth", answer["token"], {"onboard": "true"}
    )


async def test_setup_keeps_the_token_from_an_untrusted_client(
    webserver: WebserverController,
) -> None:
    """An untrusted destination is ignored: the account is created, the token stays here."""
    response = await _post_setup(webserver, {**ACCOUNT, "return_url": "https://evil.example/"})

    assert response.status == 200
    answer = json.loads(response.text or "")
    assert answer["success"] is True
    assert "redirect_to" not in answer


@pytest.mark.parametrize(
    "body",
    [
        {**ACCOUNT, "username": "m"},
        {**ACCOUNT, "password": "short"},
        {**ACCOUNT, "username": "   "},
    ],
    ids=["short_username", "short_password", "blank_username"],
)
async def test_setup_refuses_details_that_do_not_hold_up(
    webserver: WebserverController, body: dict[str, Any]
) -> None:
    """
    Details that do not meet the minimums are refused before any account is made.

    :param body: The details posted.
    """
    response = await _post_setup(webserver, body)

    assert response.status == 400
    assert json.loads(response.text or "")["success"] is False
    assert not webserver.auth.has_users


async def test_setup_is_refused_once_a_user_exists(webserver: WebserverController) -> None:
    """The setup makes the first account only; a second attempt is refused outright."""
    assert (await _post_setup(webserver, ACCOUNT)).status == 200

    response = await _post_setup(webserver, {**ACCOUNT, "username": "intruder"})

    # a conflict, which the frontend reads as "sign in instead"
    assert response.status == 409
    assert json.loads(response.text or "")["error"] == "Setup already completed"
    assert await webserver.auth.get_user_by_username("intruder") is None


async def test_two_first_attempts_at_once_make_one_admin(
    webserver: WebserverController,
) -> None:
    """Attempts that arrive together are taken one at a time: the second finds the admin made."""
    first, second = await asyncio.gather(
        _post_setup(webserver, ACCOUNT),
        _post_setup(webserver, {**ACCOUNT, "username": "other"}),
    )

    assert sorted([first.status, second.status]) == [200, 409]
    assert [user.username for user in await webserver.auth.list_users()] == ["marcel"]


async def test_setup_stays_refused_across_a_restart(mass_minimal: MusicAssistant) -> None:
    """A server that starts again on the same database still has its admin, and no setup."""
    first_start = await _start_webserver(mass_minimal)
    try:
        assert (await _post_setup(first_start, ACCOUNT)).status == 200
    finally:
        await first_start.auth.close()

    restarted = await _start_webserver(mass_minimal)
    try:
        assert restarted.auth.has_users
        response = await _post_setup(restarted, {**ACCOUNT, "username": "intruder"})
        assert response.status == 409
        with patch.object(restarted._server, "serve_static", _served_app()) as serve_static:
            page = await restarted._handle_setup_page(_request(mass_minimal, "GET", "/setup"))
        assert page.status == 403
        serve_static.assert_not_awaited()
    finally:
        await restarted.auth.close()
