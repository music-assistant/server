"""Tests for binding a sendspin web player to the session that announced it."""

from __future__ import annotations

from unittest.mock import MagicMock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import User, UserRole

from music_assistant.constants import HASSIO_SUPERVISOR_IP, INGRESS_SERVER_PORT
from music_assistant.controllers.webserver.controller import WebserverController

from .conftest import _create_ws_client

# Party guests all authenticate as this one shared account, each with its own token.
_GUEST_USER = User(user_id="party_guest", username="party_guest", role=UserRole.GUEST)
# A Home Assistant user signed in through Ingress, from more than one browser.
_HA_USER = User(user_id="ha_user", username="ha_user", role=UserRole.USER)
_INGRESS_IP = "172.30.32.1"


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


def test_web_player_is_bound_to_the_ingress_browser_that_announced_it(
    webserver: WebserverController,
) -> None:
    """Over Ingress the browser's key decides, not the Home Assistant user it signed in as."""
    browser_a = _create_ws_client(webserver, user=_HA_USER, request=_ingress_request("key-a"))
    browser_a_again = _create_ws_client(webserver, user=_HA_USER, request=_ingress_request("key-a"))
    browser_b = _create_ws_client(webserver, user=_HA_USER, request=_ingress_request("key-b"))

    webserver.set_sendspin_player_for_ingress_client("ha-user", "key-a", "player-a")

    assert browser_a._sendspin_player_id == "player-a"
    assert browser_a_again._sendspin_player_id == "player-a"
    assert browser_b._sendspin_player_id is None


def test_ingress_client_key_of_another_user_is_not_matched(
    webserver: WebserverController,
) -> None:
    """Another Home Assistant user can not claim a web player by presenting the same key."""
    other_user = _create_ws_client(
        webserver, user=_HA_USER, request=_ingress_request("key-a", ha_user_id="other-user")
    )

    webserver.set_sendspin_player_for_ingress_client("ha-user", "key-a", "player-a")

    assert other_user._sendspin_player_id is None


def test_ingress_client_key_is_ignored_outside_ingress(webserver: WebserverController) -> None:
    """A connection that did not come through Ingress can not claim a web player by its key."""
    request = make_mocked_request(
        "GET",
        "/ws?ingress_client_key=key-a",
        headers={"X-Remote-User-ID": "ha-user"},
        app=web.Application(),
    )
    direct = _create_ws_client(webserver, user=_HA_USER, request=request)

    webserver.set_sendspin_player_for_ingress_client("ha-user", "key-a", "player-a")

    assert direct._sendspin_player_id is None


def _ingress_request(client_key: str, ha_user_id: str = "ha-user") -> web.Request:
    """
    Build an API websocket request relayed by Home Assistant Ingress for a browser.

    :param client_key: The key the browser identifies itself with.
    :param ha_user_id: Id of the Home Assistant user the browser is signed in as.
    """
    app = web.Application()
    app["ingress_site"] = (_INGRESS_IP, INGRESS_SERVER_PORT)
    extra_info = {
        "sockname": (_INGRESS_IP, INGRESS_SERVER_PORT),
        "peername": (HASSIO_SUPERVISOR_IP, 54321),
    }
    transport = MagicMock()
    transport.get_extra_info.side_effect = extra_info.get
    return make_mocked_request(
        "GET",
        f"/ws?ingress_client_key={client_key}",
        headers={"X-Remote-User-ID": ha_user_id},
        app=app,
        transport=transport,
    )
