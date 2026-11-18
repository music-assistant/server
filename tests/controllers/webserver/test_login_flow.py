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
    MAX_PENDING_LOGINS_PER_SOURCE,
    MAX_TRACKED_SOURCES,
    PENDING_LOGIN_TTL,
    AuthTransport,
    PendingLogin,
    PendingLoginStore,
    SourceRateLimiter,
    source_key,
    verify_pkce,
)
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler
from music_assistant.helpers.pkce import pkce_challenge

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
    "verifier", ["short", "x" * 129, "ü" * 43], ids=["short", "long", "non_ascii"]
)
def test_verify_pkce_refuses_a_verifier_outside_rfc_7636(verifier: str) -> None:
    """
    A verifier outside the RFC 7636 syntax is refused, even when its challenge matches.

    :param verifier: The code verifier presented.
    """
    assert not verify_pkce(verifier, pkce_challenge(verifier))


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
    pending = _start(PendingLoginStore(), return_url=return_url)

    assert pending.state.startswith(prefix)
    assert len(pending.state) > 40


def test_a_pending_sign_in_is_used_once() -> None:
    """Popping a pending sign-in returns it once."""
    store = PendingLoginStore()
    pending = _start(store)

    assert store.pop(pending.state) is pending
    assert store.pop(pending.state) is None
    assert store.pop("w.unknown") is None


def test_an_expired_sign_in_is_not_returned() -> None:
    """A pending sign-in popped after its lifetime is refused and removed."""
    store = PendingLoginStore()
    pending = _start(store)

    with _monotonic_after(PENDING_LOGIN_TTL):
        assert store.pop(pending.state) is None
    assert pending.state not in store._pending


def test_starts_beyond_the_limit_are_refused_until_some_expire() -> None:
    """A start at the limit is refused, and accepted again once the pending ones expired."""
    store = PendingLoginStore()
    for index in range(MAX_PENDING_LOGINS):
        _start(store, key=f"peer:10.0.0.{index}")

    with pytest.raises(RateLimited):
        _start(store, key="peer:10.0.1.1")
    with _monotonic_after(PENDING_LOGIN_TTL):
        pending = _start(store, key="peer:10.0.1.1")

    assert list(store._pending) == [pending.state]


def test_one_source_can_not_exhaust_the_store_for_another() -> None:
    """However many sign-ins one source starts, another source can still start one."""
    store = PendingLoginStore()
    for _ in range(MAX_PENDING_LOGINS * 2):
        _start(store, key="peer:10.0.0.66")

    pending = _start(store, key="peer:10.0.0.2")

    assert store.pop(pending.state) is pending


def test_a_source_at_its_cap_replaces_its_oldest_sign_in() -> None:
    """A source at its cap loses its own oldest sign-in, never another source's."""
    store = PendingLoginStore()
    other = _start(store, key="peer:10.0.0.2")
    own = [_start(store) for _ in range(MAX_PENDING_LOGINS_PER_SOURCE)]

    newest = _start(store)

    assert store.pop(own[0].state) is None
    assert all(store.pop(entry.state) is entry for entry in [*own[1:], newest, other])


def test_the_limiter_allows_a_burst_then_recovers_over_time() -> None:
    """A source gets a burst, then one request per refill interval; others are unaffected."""
    limiter = SourceRateLimiter(burst=3, refill_seconds=10)

    assert [limiter.take("peer:a") for _ in range(4)] == [True, True, True, False]
    assert not limiter.allows("peer:a")
    assert limiter.allows("peer:b")
    with _monotonic_after(9):
        assert not limiter.allows("peer:a")
    with _monotonic_after(10):
        assert limiter.take("peer:a")
        assert not limiter.take("peer:a")
    with _monotonic_after(1000):
        assert [limiter.take("peer:a") for _ in range(4)] == [True, True, True, False]


def test_the_limiter_forgets_refilled_sources() -> None:
    """Sources whose bucket refilled completely are dropped once many are tracked."""
    limiter = SourceRateLimiter(burst=1, refill_seconds=1)
    for index in range(MAX_TRACKED_SOURCES):
        limiter.take(f"peer:{index}")

    with _monotonic_after(1):
        limiter.take("peer:new")

    assert list(limiter._buckets) == ["peer:new"]


@pytest.mark.parametrize(
    ("peer_address", "session_id", "expected"),
    [
        ("192.168.1.20", None, "peer:192.168.1.20"),
        ("2001:db8:1:2:aaaa::1", None, "peer:2001:db8:1:2::/64"),
        ("2001:db8:1:2:bbbb::9", None, "peer:2001:db8:1:2::/64"),
        ("fe80::1%eth0", None, "peer:fe80::/64"),
        ("::ffff:192.168.1.20", None, "peer:192.168.1.20"),
        ("127.0.0.1", "abc", "remote:abc"),
        (None, None, "unknown"),
    ],
)
def test_source_key(peer_address: str | None, session_id: str | None, expected: str) -> None:
    """
    IPv4 peers are keyed on the address, IPv6 peers on the /64, remote clients on the session.

    :param peer_address: The address of the socket peer.
    :param session_id: The verified WebRTC session id, if any.
    :param expected: The expected source key.
    """
    assert source_key(peer_address, session_id) == expected


@pytest.mark.parametrize(
    ("connect_ip", "peer_ip", "session_id", "expected"),
    [
        ("127.0.0.1", "127.0.0.1", "live", AuthTransport.REMOTE),
        ("::1", "::1", "live", AuthTransport.REMOTE),
        ("0:0:0:0:0:0:0:1", "::1", "live", AuthTransport.REMOTE),
        ("192.168.1.10", "192.168.1.10", "live", AuthTransport.REMOTE),
        ("192.168.1.10", "127.0.0.1", "live", AuthTransport.DIRECT),
        ("127.0.0.1", "192.168.1.20", "live", AuthTransport.DIRECT),
        ("127.0.0.1", "127.0.0.1", "gone", AuthTransport.DIRECT),
        ("127.0.0.1", "127.0.0.1", None, AuthTransport.DIRECT),
    ],
    ids=[
        "loopback",
        "ipv6_loopback",
        "ipv6_loopback_written_out",
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


@pytest.mark.parametrize(
    ("session_id", "expected"),
    [("live", "remote:live"), ("gone", "peer:127.0.0.1"), (None, "peer:127.0.0.1")],
    ids=["remote", "spoofed_session", "no_session"],
)
async def test_only_a_verified_remote_client_is_keyed_on_its_session(
    webserver: WebserverController, session_id: str | None, expected: str
) -> None:
    """
    A Remote Access client is keyed on its session, a client claiming a session on its address.

    :param session_id: The WebRTC session id the connection claims, if any.
    :param expected: The expected source key.
    """
    webserver.remote_access.gateway = MagicMock(
        local_ws_url="ws://127.0.0.1:8095/ws", sessions={"live": MagicMock()}
    )

    assert _ws_client(webserver, "127.0.0.1", session_id).source_key == expected


def _start(
    store: PendingLoginStore, key: str = "peer:10.0.0.1", return_url: str | None = None
) -> PendingLogin:
    """
    Start a sign-in on a store and return it.

    :param store: The store to start the sign-in on.
    :param key: The source the sign-in is started from.
    :param return_url: The URL the sign-in returns to.
    """
    return store.start(
        "homeassistant",
        AuthTransport.DIRECT,
        "http://ma.local/cb",
        "verifier",
        key=key,
        return_url=return_url,
    )


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
