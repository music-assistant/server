"""Tests for binding a sendspin web player to the session that announced it."""

from __future__ import annotations

from music_assistant_models.auth import User, UserRole

from music_assistant.controllers.webserver.controller import WebserverController

from .conftest import _create_ws_client

# Party guests all authenticate as this one shared account, each with its own token.
_GUEST_USER = User(user_id="party_guest", username="party_guest", role=UserRole.GUEST)


def test_web_player_is_bound_to_the_session_that_announced_it(
    webserver: WebserverController,
) -> None:
    """A guest's web player lands only on its own session, not on every guest sharing the account."""
    guest_a = _create_ws_client(webserver, current_token="token-a", user=_GUEST_USER)
    guest_b = _create_ws_client(webserver, current_token="token-b", user=_GUEST_USER)

    webserver.set_sendspin_player_for_token("token-a", "player-a")
    webserver.set_sendspin_player_for_token("token-b", "player-b")

    assert guest_a._sendspin_player_id == "player-a"
    assert guest_b._sendspin_player_id == "player-b"


def test_web_player_reaches_every_session_sharing_a_token(
    webserver: WebserverController,
) -> None:
    """The tabs of one browser share a token, so each of them learns the web player."""
    tab_one = _create_ws_client(webserver, current_token="token-a", user=_GUEST_USER)
    tab_two = _create_ws_client(webserver, current_token="token-a", user=_GUEST_USER)

    webserver.set_sendspin_player_for_token("token-a", "player-a")

    assert tab_one._sendspin_player_id == "player-a"
    assert tab_two._sendspin_player_id == "player-a"


def test_unauthenticated_session_is_left_alone(webserver: WebserverController) -> None:
    """A client that never authenticated holds no token and is never stamped."""
    anonymous = _create_ws_client(webserver, current_token="token-a", user=_GUEST_USER)
    anonymous._authenticated_user = None
    anonymous._current_token = None

    webserver.set_sendspin_player_for_token("token-a", "player-a")

    assert anonymous._sendspin_player_id is None


def test_web_player_is_bound_to_the_webrtc_session_that_announced_it(
    webserver: WebserverController,
) -> None:
    """A player announced over the WebRTC gateway lands on the session that carries its id."""
    gateway = _create_ws_client(
        webserver, current_token="token-a", user=_GUEST_USER, webrtc_session_id="session-a"
    )
    other_gateway = _create_ws_client(
        webserver, current_token="token-b", user=_GUEST_USER, webrtc_session_id="session-b"
    )

    webserver.set_sendspin_player_for_webrtc_session("session-a", "player-a")

    assert gateway._sendspin_player_id == "player-a"
    assert other_gateway._sendspin_player_id is None
