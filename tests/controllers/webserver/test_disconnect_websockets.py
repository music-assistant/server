"""Tests for closing the live websocket sessions of a revoked token or user."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import User, UserRole

from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

_USER = User(user_id="user_1", username="listener", role=UserRole.USER)
_OTHER_USER = User(user_id="user_2", username="other", role=UserRole.USER)


@pytest.fixture
async def webserver(mass_minimal: MusicAssistant) -> AsyncIterator[WebserverController]:
    """Return a WebserverController with stubbed serialization dependencies."""
    mass_minimal.metadata = SimpleNamespace(  # type: ignore[assignment]
        compute_image_id=lambda provider, path: f"{provider}--{path}"
    )
    mass_minimal.translations = SimpleNamespace(  # type: ignore[assignment]
        get_translation=lambda _key, **_kwargs: None
    )
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    yield webserver
    for client in list(webserver.clients):
        client.cancel()
    await asyncio.gather(
        *(client._handle_task for client in webserver.clients if client._handle_task),
        return_exceptions=True,
    )


def _create_ws_client(
    webserver: WebserverController,
    *,
    token_id: str | None = None,
    user: User | None = None,
) -> WebsocketClientHandler:
    """Create a registered websocket client whose handle task can be cancelled."""
    request = make_mocked_request("GET", "/ws", app=web.Application())
    client = WebsocketClientHandler(webserver, request)
    client._authenticated_user = user
    client._token_id = token_id
    client._handle_task = asyncio.get_running_loop().create_task(asyncio.sleep(60))
    webserver.register_websocket_client(client)
    return client


async def test_only_the_sessions_of_the_revoked_token_are_closed(
    webserver: WebserverController,
) -> None:
    """
    Test that revoking one token leaves the other sessions of the same user alone.

    :param webserver: WebserverController instance.
    """
    revoked = _create_ws_client(webserver, token_id="token-a", user=_USER)
    other_device = _create_ws_client(webserver, token_id="token-b", user=_USER)

    webserver.disconnect_websockets_for_token("token-a")
    await asyncio.sleep(0)

    assert revoked._handle_task is not None
    assert revoked._handle_task.cancelled()
    assert other_device._handle_task is not None
    assert not other_device._handle_task.cancelled()


async def test_a_session_without_a_token_survives_a_token_revocation(
    webserver: WebserverController,
) -> None:
    """
    Test that a session holding no token is not caught by a token revocation.

    An Ingress session authenticates without a token, so it carries a user but no token id.

    :param webserver: WebserverController instance.
    """
    ingress = _create_ws_client(webserver, user=_USER)

    webserver.disconnect_websockets_for_token("token-a")
    await asyncio.sleep(0)

    assert ingress._handle_task is not None
    assert not ingress._handle_task.cancelled()


async def test_every_session_of_a_user_is_closed(webserver: WebserverController) -> None:
    """
    Test that all sessions of a user are closed regardless of the token they hold.

    :param webserver: WebserverController instance.
    """
    phone = _create_ws_client(webserver, token_id="token-a", user=_USER)
    laptop = _create_ws_client(webserver, token_id="token-b", user=_USER)
    stranger = _create_ws_client(webserver, token_id="token-c", user=_OTHER_USER)
    anonymous = _create_ws_client(webserver)

    webserver.disconnect_websockets_for_user(_USER.user_id)
    await asyncio.sleep(0)

    assert phone._handle_task is not None
    assert phone._handle_task.cancelled()
    assert laptop._handle_task is not None
    assert laptop._handle_task.cancelled()
    assert stranger._handle_task is not None
    assert not stranger._handle_task.cancelled()
    assert anonymous._handle_task is not None
    assert not anonymous._handle_task.cancelled()
