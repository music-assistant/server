"""Tests for closing the live websocket sessions of a revoked token or user."""

from __future__ import annotations

import asyncio

from music_assistant_models.auth import User, UserRole

from music_assistant.controllers.webserver.controller import WebserverController

from .conftest import _create_ws_client

_USER = User(user_id="user_1", username="listener", role=UserRole.USER)
_OTHER_USER = User(user_id="user_2", username="other", role=UserRole.USER)


async def test_only_the_sessions_of_the_revoked_token_are_closed(
    webserver: WebserverController,
) -> None:
    """
    Test that revoking one token leaves the other sessions of the same user alone.

    :param webserver: WebserverController instance.
    """
    revoked = _create_ws_client(webserver, token_id="token-a", user=_USER, with_handle_task=True)
    other_device = _create_ws_client(
        webserver, token_id="token-b", user=_USER, with_handle_task=True
    )

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
    ingress = _create_ws_client(webserver, user=_USER, with_handle_task=True)

    webserver.disconnect_websockets_for_token("token-a")
    await asyncio.sleep(0)

    assert ingress._handle_task is not None
    assert not ingress._handle_task.cancelled()


async def test_every_session_of_a_user_is_closed(webserver: WebserverController) -> None:
    """
    Test that all sessions of a user are closed regardless of the token they hold.

    :param webserver: WebserverController instance.
    """
    phone = _create_ws_client(webserver, token_id="token-a", user=_USER, with_handle_task=True)
    laptop = _create_ws_client(webserver, token_id="token-b", user=_USER, with_handle_task=True)
    stranger = _create_ws_client(
        webserver, token_id="token-c", user=_OTHER_USER, with_handle_task=True
    )
    anonymous = _create_ws_client(webserver, with_handle_task=True)

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
