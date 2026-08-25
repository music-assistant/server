"""Tests for binding a sendspin web player to the session that announced it."""

from __future__ import annotations

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

# Party guests all authenticate as this one shared account, each with its own token.
_GUEST_USER = User(user_id="party_guest", username="party_guest", role=UserRole.GUEST)


@pytest.fixture
def webserver(mass_minimal: MusicAssistant) -> WebserverController:
    """Return a WebserverController with stubbed serialization dependencies."""
    mass_minimal.metadata = SimpleNamespace(  # type: ignore[assignment]
        compute_image_id=lambda provider, path: f"{provider}--{path}"
    )
    mass_minimal.translations = SimpleNamespace(  # type: ignore[assignment]
        get_translation=lambda _key, **_kwargs: None
    )
    webserver = WebserverController(mass_minimal)
    mass_minimal.webserver = webserver
    return webserver


def _create_ws_client(
    webserver: WebserverController, token: str, user: User = _GUEST_USER
) -> WebsocketClientHandler:
    """Create an authenticated websocket client handler holding the given token."""
    request = make_mocked_request("GET", "/ws", app=web.Application())
    client = WebsocketClientHandler(webserver, request)
    client._authenticated_user = user
    client._current_token = token
    webserver.register_websocket_client(client)
    return client


def test_web_player_is_bound_to_the_session_that_announced_it(
    webserver: WebserverController,
) -> None:
    """A guest's web player lands only on its own session, not on every guest sharing the account."""
    guest_a = _create_ws_client(webserver, "token-a")
    guest_b = _create_ws_client(webserver, "token-b")

    webserver.set_sendspin_player_for_token("token-a", "player-a")
    webserver.set_sendspin_player_for_token("token-b", "player-b")

    assert guest_a._sendspin_player_id == "player-a"
    assert guest_b._sendspin_player_id == "player-b"


def test_web_player_reaches_every_session_sharing_a_token(
    webserver: WebserverController,
) -> None:
    """The tabs of one browser share a token, so each of them learns the web player."""
    tab_one = _create_ws_client(webserver, "token-a")
    tab_two = _create_ws_client(webserver, "token-a")

    webserver.set_sendspin_player_for_token("token-a", "player-a")

    assert tab_one._sendspin_player_id == "player-a"
    assert tab_two._sendspin_player_id == "player-a"


def test_unauthenticated_session_is_left_alone(webserver: WebserverController) -> None:
    """A client that never authenticated holds no token and is never stamped."""
    anonymous = _create_ws_client(webserver, "token-a")
    anonymous._authenticated_user = None
    anonymous._current_token = None

    webserver.set_sendspin_player_for_token("token-a", "player-a")

    assert anonymous._sendspin_player_id is None
