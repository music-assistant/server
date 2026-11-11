"""Tests for the GENA eventing module."""

from __future__ import annotations

import asyncio
from typing import Self, cast

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from music_assistant.providers.dlna_receiver.eventing import EventingManager


@pytest.fixture
def manager() -> EventingManager:
    """Create a fresh eventing manager."""
    return EventingManager()


async def test_subscribe_returns_sid_and_timeout(manager: EventingManager) -> None:
    """subscribe() returns a uuid SID and the default timeout."""
    sid, timeout = await manager.subscribe("<http://192.168.1.5:8080/callback>")
    assert sid.startswith("uuid:")
    assert timeout == 1800


async def test_subscribe_custom_timeout(manager: EventingManager) -> None:
    """subscribe() honors a custom Second-N timeout header."""
    _sid, timeout = await manager.subscribe(
        "<http://192.168.1.5:8080/callback>",
        "Second-300",
    )
    assert timeout == 300


async def test_subscribe_multiple_callbacks(manager: EventingManager) -> None:
    """subscribe() stores all callback URLs from a multi-URL CALLBACK header."""
    sid, _ = await manager.subscribe(
        "<http://192.168.1.5:8080/cb><http://10.0.0.5:8080/cb>",
    )
    sub = manager._subscriptions[sid]
    assert len(sub.callback_urls) == 2


async def test_subscribe_no_callback_raises(manager: EventingManager) -> None:
    """subscribe() rejects an empty CALLBACK header."""
    with pytest.raises(ValueError, match="No valid callback URLs"):
        await manager.subscribe("")


async def test_subscribe_rejects_loopback_callback(manager: EventingManager) -> None:
    """GENA callbacks cannot target loopback services."""
    with pytest.raises(ValueError, match="callback"):
        await manager.subscribe("<http://127.0.0.1:8080/callback>")

    assert manager._subscriptions == {}


async def test_subscribe_allows_lan_callback(manager: EventingManager) -> None:
    """GENA callbacks on the control point's LAN remain supported."""
    sid, _timeout = await manager.subscribe("<http://192.168.1.5:8080/callback>")

    assert manager._subscriptions[sid].callback_urls == ["http://192.168.1.5:8080/callback"]


async def test_subscribe_rejects_when_active_limit_is_reached(manager: EventingManager) -> None:
    """New subscriptions are rejected once the per-service limit is reached."""
    for idx in range(EventingManager.MAX_SUBSCRIPTIONS):
        await manager.subscribe(f"<http://192.168.1.5:8080/callback/{idx}>")

    with pytest.raises(ValueError, match="limit"):
        await manager.subscribe("<http://192.168.1.5:8080/callback/overflow>")


async def test_subscribe_reclaims_expired_slot_at_limit(manager: EventingManager) -> None:
    """Expired subscriptions are removed before enforcing the active limit."""
    first_sid = ""
    for idx in range(EventingManager.MAX_SUBSCRIPTIONS):
        sid, _timeout = await manager.subscribe(f"<http://192.168.1.5:8080/callback/{idx}>")
        first_sid = first_sid or sid
    manager._subscriptions[first_sid].created_at -= 1801

    await manager.subscribe("<http://192.168.1.5:8080/callback/replacement>")

    assert first_sid not in manager._subscriptions
    assert len(manager._subscriptions) == EventingManager.MAX_SUBSCRIPTIONS


async def test_unsubscribe(manager: EventingManager) -> None:
    """unsubscribe() removes the subscription by SID."""
    sid, _ = await manager.subscribe("<http://192.168.1.5:8080/cb>")
    assert sid in manager._subscriptions
    manager.unsubscribe(sid)
    assert sid not in manager._subscriptions


def test_unsubscribe_unknown_is_noop(manager: EventingManager) -> None:
    """unsubscribe() on an unknown SID is a no-op rather than an error."""
    manager.unsubscribe("uuid:nonexistent")  # should not raise


async def test_renew(manager: EventingManager) -> None:
    """renew() updates the timeout for an active subscription."""
    sid, _ = await manager.subscribe("<http://192.168.1.5:8080/cb>", "Second-100")
    new_timeout = manager.renew(sid, "Second-600")
    assert new_timeout == 600


def test_renew_unknown_raises(manager: EventingManager) -> None:
    """renew() on an unknown SID raises KeyError (412 Precondition Failed)."""
    with pytest.raises(KeyError):
        manager.renew("uuid:nonexistent")


async def test_renew_expired_raises_and_removes(manager: EventingManager) -> None:
    """
    renew() on an expired SID raises KeyError AND evicts the stale entry.

    Per UPnP spec, renewing an expired subscription must fail with 412
    Precondition Failed — the renderer surfaces the KeyError as 412, and
    the manager must not keep the dead subscription around.
    """
    sid, _ = await manager.subscribe("<http://192.168.1.5:8080/cb>", "Second-100")
    # Force expiry by backdating the subscription's creation timestamp.
    manager._subscriptions[sid].created_at -= 1000
    assert manager._subscriptions[sid].is_expired

    with pytest.raises(KeyError):
        manager.renew(sid, "Second-1800")

    assert sid not in manager._subscriptions


def test_parse_callback_header() -> None:
    """_parse_callback_header splits angle-bracketed URLs into a list."""
    urls = EventingManager._parse_callback_header(
        "<http://192.168.1.5:8080/event><http://10.0.0.1:9000/ev>",
    )
    assert urls == ["http://192.168.1.5:8080/event", "http://10.0.0.1:9000/ev"]


def test_parse_callback_header_single() -> None:
    """_parse_callback_header handles a single URL."""
    urls = EventingManager._parse_callback_header("<http://host:1234/cb>")
    assert urls == ["http://host:1234/cb"]


def test_parse_callback_header_rejects_non_http_schemes() -> None:
    """Only http:// and https:// schemes are valid GENA CALLBACK URLs."""
    urls = EventingManager._parse_callback_header(
        "<httpx://evil/cb><httpfake://x><ftp://h/cb><file:///etc/passwd>"
        "<http://ok/cb><https://secure/cb>",
    )
    assert urls == ["http://ok/cb", "https://secure/cb"]


def test_parse_timeout_default() -> None:
    """_parse_timeout falls back to the default when header is missing or empty."""
    assert EventingManager._parse_timeout(None) == 1800
    assert EventingManager._parse_timeout("") == 1800


def test_parse_timeout_infinite() -> None:
    """_parse_timeout maps 'infinite' to the default timeout."""
    assert EventingManager._parse_timeout("infinite") == 1800


def test_parse_timeout_seconds() -> None:
    """_parse_timeout accepts bounded values and clamps excessive ones."""
    assert EventingManager._parse_timeout("Second-300") == 300
    assert EventingManager._parse_timeout("Second-7200") == 1800
    assert EventingManager._parse_timeout("Second-0") == 1


def test_build_propertyset() -> None:
    """_build_propertyset wraps variables in GENA XML structure."""
    xml = EventingManager._build_propertyset({"Volume": "75", "Mute": "0"})
    assert "e:propertyset" in xml
    assert "<Volume>75</Volume>" in xml
    assert "<Mute>0</Mute>" in xml


def test_build_propertyset_escapes_values() -> None:
    """_build_propertyset escapes XML-special characters in values."""
    xml = EventingManager._build_propertyset({"Title": "Tom & Jerry"})
    assert "Tom &amp; Jerry" in xml


async def test_notify_no_subscribers(manager: EventingManager) -> None:
    """Notify with no subscribers should be a no-op."""
    await manager.notify({"TransportState": "PLAYING"})


async def test_notify_serializes_delivery_per_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent state changes never overlap NOTIFY delivery for one SID."""
    active_requests = 0
    max_active_requests = 0
    sequences: list[str] = []

    async def _receive_notify(request: web.Request) -> web.Response:
        nonlocal active_requests, max_active_requests
        active_requests += 1
        max_active_requests = max(max_active_requests, active_requests)
        sequences.append(request.headers["SEQ"])
        await asyncio.sleep(0.02)
        active_requests -= 1
        return web.Response(status=200)

    app = web.Application()
    app.router.add_route("NOTIFY", "/callback", _receive_notify)
    server = TestServer(app)
    await server.start_server()
    session = aiohttp.ClientSession()
    manager = EventingManager(session=session)
    try:
        import music_assistant.providers.dlna_receiver.eventing as eventing_module  # noqa: PLC0415

        async def _allow_test_server(url: str) -> str:
            return url

        monkeypatch.setattr(eventing_module, "validate_outbound_url", _allow_test_server)
        await manager.start()
        sid, _timeout = await manager.subscribe(f"<{server.make_url('/callback')}>")
        manager.track_initial_notify(sid, {"TransportState": "STOPPED"})
        await asyncio.gather(*list(manager._pending_tasks))
        await manager.notify({"TransportState": "PLAYING"})
        await asyncio.gather(*list(manager._pending_tasks))

        assert max_active_requests == 1
        assert sequences == ["0", "1"]
    finally:
        await manager.stop()
        await session.close()
        await server.close()


async def test_injected_session_is_not_closed_on_stop() -> None:
    """
    A caller-owned session must survive ``stop()`` unchanged.

    Provider-level wiring injects ``mass.http_session`` and closing it
    would break the rest of MA. Only managers that created their own
    session are allowed to close it.
    """
    import aiohttp  # noqa: PLC0415

    shared = aiohttp.ClientSession()
    try:
        mgr = EventingManager(session=shared)
        await mgr.start()
        # start() must reuse the injected session, not overwrite it.
        assert mgr._session is shared
        await mgr.stop()
        # stop() must not close a session it does not own.
        assert not shared.closed
    finally:
        await shared.close()


async def test_owned_session_is_closed_on_stop() -> None:
    """When no session is injected, the manager creates + closes its own."""
    mgr = EventingManager()
    await mgr.start()
    owned = mgr._session
    assert owned is not None
    await mgr.stop()
    assert owned.closed


async def test_notify_does_not_hide_unexpected_session_errors() -> None:
    """Unexpected request failures surface instead of looking like network loss."""

    class _BrokenSession:
        closed = False

        def request(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("session contract broken")

    manager = EventingManager(session=cast("aiohttp.ClientSession", _BrokenSession()))
    sid, _timeout = await manager.subscribe("<http://192.168.1.5/callback>")

    with pytest.raises(RuntimeError, match="session contract broken"):
        await manager._send_notify(manager._subscriptions[sid], "<propertyset/>")


async def test_notify_treats_client_errors_as_delivery_failures() -> None:
    """Expected aiohttp errors exhaust callback URLs without escaping."""

    class _OfflineSession:
        closed = False

        def request(self, *_args: object, **_kwargs: object) -> object:
            raise aiohttp.ClientConnectionError("offline")

    manager = EventingManager(session=cast("aiohttp.ClientSession", _OfflineSession()))
    sid, _timeout = await manager.subscribe("<http://192.168.1.5/callback>")

    await manager._send_notify(manager._subscriptions[sid], "<propertyset/>")


async def test_notify_does_not_follow_redirect_and_tries_next_callback() -> None:
    """A redirecting callback fails over without aiohttp following its Location."""

    class _Response:
        def __init__(self, status: int) -> None:
            self.status = status

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _RedirectSession:
        closed = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []
            self._statuses = [302, 200]

        def request(self, _method: str, url: str, **kwargs: object) -> _Response:
            self.calls.append((url, kwargs))
            return _Response(self._statuses.pop(0))

    session = _RedirectSession()
    manager = EventingManager(session=cast("aiohttp.ClientSession", session))
    sid, _timeout = await manager.subscribe("<http://192.168.1.5/first><http://192.168.1.6/second>")

    await manager._send_notify(manager._subscriptions[sid], "<propertyset/>")

    assert [url for url, _kwargs in session.calls] == [
        "http://192.168.1.5/first",
        "http://192.168.1.6/second",
    ]
    assert all(kwargs["allow_redirects"] is False for _url, kwargs in session.calls)


async def test_subscribe_logs_only_redacted_callback_url(
    manager: EventingManager, caplog: pytest.LogCaptureFixture
) -> None:
    """Callback credentials, query, and fragment never enter subscription logs."""
    callback = "http://alice:secret@192.168.1.5/cb?token=signed#private"

    with caplog.at_level("INFO"):
        await manager.subscribe(f"<{callback}>")

    assert "alice" not in caplog.text
    assert "secret" not in caplog.text
    assert "signed" not in caplog.text


async def test_stop_cancels_tracked_initial_notify() -> None:
    """Shutdown cancels and awaits an in-flight initial event task."""
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class _HangingRequest:
        async def __aenter__(self) -> None:
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _HangingSession:
        closed = False

        def request(self, *_args: object, **_kwargs: object) -> _HangingRequest:
            return _HangingRequest()

    manager = EventingManager(session=cast("aiohttp.ClientSession", _HangingSession()))
    sid, _timeout = await manager.subscribe("<http://192.168.1.5/callback>")
    manager.track_initial_notify(sid, {"TransportState": "STOPPED"})
    await entered.wait()

    await manager.stop()

    assert cancelled.is_set()
    assert manager._pending_tasks == set()


async def test_tracked_notify_logs_unexpected_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unexpected background delivery errors are retrieved and logged."""

    class _BrokenSession:
        closed = False

        def request(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("session contract broken in background")

    manager = EventingManager(session=cast("aiohttp.ClientSession", _BrokenSession()))
    sid, _timeout = await manager.subscribe("<http://192.168.1.5/callback>")

    with caplog.at_level("ERROR"):
        manager.track_initial_notify(sid, {"TransportState": "STOPPED"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert "session contract broken in background" in caplog.text
    assert manager._pending_tasks == set()
