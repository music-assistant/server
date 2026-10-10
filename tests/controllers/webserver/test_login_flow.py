"""Tests for the pending sign-ins, PKCE helpers and transport detection of the sign-in flow."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.errors import RateLimited

from music_assistant.controllers.webserver import websocket_client
from music_assistant.controllers.webserver.helpers import login_flow
from music_assistant.controllers.webserver.helpers.login_flow import (
    MAX_PENDING_LOGINS,
    PENDING_LOGIN_TTL,
    AuthTransport,
    PendingLoginStore,
    pkce_challenge,
    verify_pkce,
)
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant.controllers.webserver.controller import WebserverController

# the code verifier and challenge of RFC 7636, appendix B
RFC_7636_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC_7636_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_pkce_challenge_matches_the_rfc_example() -> None:
    """The S256 challenge of the RFC 7636 example verifier is the one the RFC lists."""
    assert pkce_challenge(RFC_7636_VERIFIER) == RFC_7636_CHALLENGE
    assert verify_pkce(RFC_7636_VERIFIER, RFC_7636_CHALLENGE)
    assert not verify_pkce("another-verifier", RFC_7636_CHALLENGE)


@pytest.mark.parametrize(
    ("return_url", "prefix"),
    [
        (None, "w."),
        ("https://app.music-assistant.io/#/home", "w."),
        ("musicassistant://auth/callback", "n."),
    ],
)
def test_state_prefix_follows_the_return_url(return_url: str | None, prefix: str) -> None:
    """
    A sign-in returning to the mobile app gets an "n." state, any other a "w." state.

    :param return_url: The URL the sign-in returns to.
    :param prefix: The expected state prefix.
    """
    pending = PendingLoginStore().start(
        "homeassistant", AuthTransport.DIRECT, "http://ma.local/cb", return_url=return_url
    )

    assert pending.state.startswith(prefix)
    assert len(pending.state) > 40


def test_a_pending_sign_in_is_used_once() -> None:
    """Popping a pending sign-in returns it once."""
    store = PendingLoginStore()
    pending = store.start("homeassistant", AuthTransport.DIRECT, "http://ma.local/cb")

    assert store.pop(pending.state) is pending
    assert store.pop(pending.state) is None
    assert store.pop("w.unknown") is None


def test_an_expired_sign_in_is_not_returned() -> None:
    """A pending sign-in popped after its lifetime is refused and removed."""
    store = PendingLoginStore()
    pending = store.start("homeassistant", AuthTransport.DIRECT, "http://ma.local/cb")

    with _monotonic_after(PENDING_LOGIN_TTL):
        assert store.pop(pending.state) is None
    assert pending.state not in store._pending


def test_starts_beyond_the_limit_are_refused_until_some_expire() -> None:
    """A start at the limit is refused, and accepted again once the pending ones expired."""
    store = PendingLoginStore()
    for _ in range(MAX_PENDING_LOGINS):
        store.start("homeassistant", AuthTransport.DIRECT, "http://ma.local/cb")

    with pytest.raises(RateLimited):
        store.start("homeassistant", AuthTransport.DIRECT, "http://ma.local/cb")
    with _monotonic_after(PENDING_LOGIN_TTL):
        pending = store.start("homeassistant", AuthTransport.DIRECT, "http://ma.local/cb")

    assert list(store._pending) == [pending.state]


@pytest.mark.parametrize(
    ("connect_ip", "peer_ip", "session_id", "expected"),
    [
        ("127.0.0.1", "127.0.0.1", "live", AuthTransport.REMOTE),
        ("::1", "::1", "live", AuthTransport.REMOTE),
        ("192.168.1.10", "192.168.1.10", "live", AuthTransport.REMOTE),
        ("192.168.1.10", "127.0.0.1", "live", AuthTransport.DIRECT),
        ("127.0.0.1", "192.168.1.20", "live", AuthTransport.DIRECT),
        ("127.0.0.1", "127.0.0.1", "gone", AuthTransport.DIRECT),
        ("127.0.0.1", "127.0.0.1", None, AuthTransport.DIRECT),
    ],
    ids=[
        "loopback",
        "ipv6_loopback",
        "bind_ip",
        "loopback_while_bound_to_an_ip",
        "other_peer",
        "spoofed_session",
        "no_session",
    ],
)
async def test_remote_access_is_detected_from_the_gateway_connection(
    webserver: WebserverController,
    connect_ip: str,
    peer_ip: str,
    session_id: str | None,
    expected: AuthTransport,
) -> None:
    """
    A connection is remote only when the gateway opened it, for a session it knows.

    :param connect_ip: The address the gateway connects to the local websocket on.
    :param peer_ip: The address the connection came from.
    :param session_id: The WebRTC session id the connection claims, if any.
    :param expected: The expected transport.
    """
    host = f"[{connect_ip}]" if ":" in connect_ip else connect_ip
    webserver.remote_access.gateway = MagicMock(
        local_ws_url=f"ws://{host}:8095/ws", sessions={"live": MagicMock()}
    )
    client = _ws_client(webserver, peer_ip, session_id)

    assert client.auth_transport is expected


async def test_no_remote_access_without_a_gateway(webserver: WebserverController) -> None:
    """Without a running gateway no connection is remote."""
    client = _ws_client(webserver, "127.0.0.1", "live")

    assert client.auth_transport is AuthTransport.DIRECT


async def test_an_ingress_connection_is_ingress(webserver: WebserverController) -> None:
    """A connection from the ingress proxy is an ingress connection."""
    with patch.object(websocket_client, "is_request_from_ingress_proxy", return_value=True):
        client = _ws_client(webserver, "172.30.32.2", None)

    assert client.auth_transport is AuthTransport.INGRESS


def _ws_client(
    webserver: WebserverController, peer_ip: str, session_id: str | None
) -> WebsocketClientHandler:
    """
    Return a websocket connection from the given address, claiming the given session.

    :param webserver: The webserver the connection is made to.
    :param peer_ip: The address the connection came from.
    :param session_id: The WebRTC session id the connection claims, if any.
    """
    transport = MagicMock()
    transport.get_extra_info.side_effect = {"peername": (peer_ip, 54321)}.get
    path = f"/ws?webrtc_session_id={session_id}" if session_id else "/ws"
    request = make_mocked_request("GET", path, app=web.Application(), transport=transport)
    return WebsocketClientHandler(webserver, request)


@contextmanager
def _monotonic_after(seconds: float) -> Iterator[None]:
    """
    Make the pending sign-ins see a clock the given number of seconds ahead.

    :param seconds: How far ahead of the real clock the pending sign-ins' clock runs.
    """
    clock = MagicMock(monotonic=MagicMock(return_value=time.monotonic() + seconds))
    with patch.object(login_flow, "time", clock):
        yield
