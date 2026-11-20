"""Tests for the first-time setup: the page that hosts the account step and its endpoint."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import UserRole

from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers import auth_middleware

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


def _close_setup_window(webserver: WebserverController) -> None:
    """
    Move the server past the time the first-time setup stays open after it starts.

    :param webserver: The controller whose setup window ends.
    """
    webserver._setup_deadline = time.monotonic() - 1


async def _sign_in_through_ingress(webserver: WebserverController, role: str) -> None:
    """
    Sign a Home Assistant account in through Ingress, which creates its user on first use.

    :param webserver: The controller the account signs in to.
    :param role: The role Home Assistant reports for the account.
    """
    request = make_mocked_request(
        "GET",
        "/",
        headers={"X-Remote-User-ID": "ha-user-1", "X-Remote-User-Name": "paulus"},
        app=_request(webserver.mass, "GET", "/").app,
    )
    with (
        patch.object(auth_middleware, "is_request_from_ingress_proxy", return_value=True),
        patch.object(
            auth_middleware,
            "get_ha_user_details",
            AsyncMock(return_value=("paulus", "Paulus", None)),
        ),
        patch.object(auth_middleware, "get_ha_user_role", AsyncMock(return_value=role)),
    ):
        user = await auth_middleware.get_authenticated_user(request)
    assert user is not None
    assert user.role == role


async def test_setup_creates_the_admin_just_before_the_window_closes(
    webserver: WebserverController,
) -> None:
    """The setup takes the first admin up to the end of its window."""
    webserver._setup_deadline = time.monotonic() + 5

    response = await _post_setup(webserver, ACCOUNT)

    assert response.status == 200
    assert webserver.auth.has_users


async def test_setup_is_refused_once_its_window_has_closed(
    webserver: WebserverController,
) -> None:
    """After the window no account or token is made, until the server starts again."""
    _close_setup_window(webserver)

    response = await _post_setup(webserver, ACCOUNT)

    assert response.status == 403
    answer = json.loads(response.text or "")
    assert answer["success"] is False
    assert "Restart Music Assistant" in answer["error"]
    assert not webserver.auth.has_users
    assert await webserver.auth.list_users() == []
    assert await webserver.auth.database.get_rows("auth_tokens") == []


async def test_the_setup_page_says_it_timed_out_once_its_window_has_closed(
    webserver: WebserverController,
) -> None:
    """The setup page tells the user to restart instead of serving the app."""
    _close_setup_window(webserver)

    with patch.object(webserver._server, "serve_static", _served_app()) as serve_static:
        response = await webserver._handle_setup_page(_request(webserver.mass, "GET", "/setup"))

    assert response.status == 403
    assert "Restart Music Assistant to try again" in _text(response)
    serve_static.assert_not_awaited()


async def test_a_restart_opens_the_setup_window_again(mass_minimal: MusicAssistant) -> None:
    """The window is counted from each start, so a restart gives the user another go."""
    first_start = await _start_webserver(mass_minimal)
    try:
        _close_setup_window(first_start)
        assert (await _post_setup(first_start, ACCOUNT)).status == 403
    finally:
        await first_start.auth.close()

    restarted = await _start_webserver(mass_minimal)
    try:
        assert (await _post_setup(restarted, ACCOUNT)).status == 200
    finally:
        await restarted.auth.close()


@pytest.mark.parametrize("window_open", [True, False], ids=["window_open", "window_closed"])
async def test_ingress_creates_the_first_admin_whatever_the_setup_window(
    webserver: WebserverController, window_open: bool
) -> None:
    """
    A Home Assistant admin signing in through Ingress becomes the first admin at any time.

    :param window_open: Whether the public setup window is still open.
    """
    if not window_open:
        _close_setup_window(webserver)

    await _sign_in_through_ingress(webserver, UserRole.ADMIN)

    assert webserver.auth.has_users


async def test_setup_is_closed_once_ingress_has_created_the_admin(
    mass_minimal: MusicAssistant,
) -> None:
    """
    The admin made through Ingress closes the public setup, also after a restart.

    The setup window is still open here, so only the existing admin keeps setup closed.
    """
    webserver = await _start_webserver(mass_minimal)
    try:
        await _sign_in_through_ingress(webserver, UserRole.ADMIN)
        await _assert_setup_closed(webserver)
    finally:
        await webserver.auth.close()

    restarted = await _start_webserver(mass_minimal)
    try:
        assert restarted.auth.has_users
        await _assert_setup_closed(restarted)
    finally:
        await restarted.auth.close()


async def _assert_setup_closed(webserver: WebserverController) -> None:
    """
    Assert the public setup makes no account and its page is closed.

    :param webserver: The controller to check.
    """
    response = await _post_setup(webserver, {**ACCOUNT, "username": "intruder"})
    assert response.status == 400
    assert await webserver.auth.get_user_by_username("intruder") is None
    with patch.object(webserver._server, "serve_static", _served_app()) as serve_static:
        page = await webserver._handle_setup_page(_request(webserver.mass, "GET", "/setup"))
    assert page.status == 403
    assert "already been completed" in _text(page)
    serve_static.assert_not_awaited()
    with patch.object(webserver._server, "serve_static", _served_app()):
        index = await webserver._handle_index(_request(webserver.mass, "GET", "/"))
    assert index.status == 200
