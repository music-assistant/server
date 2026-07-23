"""Tests for the party QR cover compositor (spec 0004)."""

from __future__ import annotations

import asyncio
import io
import json
import threading
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock
from urllib.parse import parse_qs, urlsplit

import segno
from aiohttp.test_utils import TestClient as AiohttpTestClient
from aiohttp.test_utils import TestServer
from PIL import Image

from music_assistant.providers.msx_bridge import http_server as http_server_module
from music_assistant.providers.msx_bridge.http_server import (
    MSXHTTPServer,
    PartyInfo,
    _stamp_qr_on_cover,
)
from music_assistant.providers.msx_bridge.mappers import map_tracks_to_msx_playlist

if TYPE_CHECKING:
    import pytest
from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider

if TYPE_CHECKING:
    import pytest

JOIN_URL = "http://ma.local:8095/?join=ABC123"
COVER_URL = "http://ma.local:8095/imageproxy?path=cover.jpg"


def _party_mock(url: str | None = JOIN_URL) -> Mock:
    """Return a mock Party plugin provider."""
    party = Mock()
    party.get_party_url = AsyncMock(return_value=url)
    config = Mock()
    config.party_name = "My Party"
    config.qr_text = "Scan to join!"
    party.get_party_config = AsyncMock(return_value=config)
    return party


def _black_cover_png(size: int = 200) -> bytes:
    """Return a solid black square PNG."""
    buf = io.BytesIO()
    Image.new("RGB", (size, size), (0, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _qr_png() -> bytes:
    """Return a small QR PNG."""
    buf = io.BytesIO()
    segno.make(JOIN_URL, error="m").save(buf, kind="png", scale=4)
    return buf.getvalue()


def _http_session_mock(body: bytes, status: int = 200) -> Mock:
    """Return a mock aiohttp session whose get() yields the given body."""
    resp = AsyncMock()
    resp.status = status
    resp.read = AsyncMock(return_value=body)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = Mock()
    session.get = Mock(return_value=cm)
    return session


def _slow_http_session_mock(body: bytes, release: asyncio.Event) -> Mock:
    """Return a mock session whose get() blocks reading the body until released."""

    async def _gated_read() -> bytes:
        await release.wait()
        return body

    resp = AsyncMock()
    resp.status = 200
    resp.read = _gated_read
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = Mock()
    session.get = Mock(return_value=cm)
    return session


# --- Compositor ---


def test_stamp_qr_on_cover_composites() -> None:
    """The QR lands bottom-right with a white quiet zone; dimensions are preserved."""
    cover = _black_cover_png(200)
    stamped = _stamp_qr_on_cover(cover, _qr_png())

    assert stamped != cover
    img = Image.open(io.BytesIO(stamped))
    assert img.size == (200, 200)
    # top-left quadrant stays untouched cover (black)
    assert img.convert("RGB").getpixel((10, 10)) == (0, 0, 0)
    # bottom-right quadrant contains white QR quiet-zone pixels
    quadrant = img.convert("RGB").crop((100, 100, 200, 200))
    assert any(px == (255, 255, 255) for px in list(quadrant.getdata()))


# --- /api/party/qr-cover.png endpoint ---


async def test_qr_cover_active_party_returns_png(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """With an active party and an allowed source, the composited PNG is served."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    mass_mock.webserver.base_url = "http://ma.local:8095"
    mass_mock.http_session = _http_session_mock(_black_cover_png())
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get(
            "/api/party/qr-cover.png", params={"image": COVER_URL}, allow_redirects=False
        )
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/png"
        body = await resp.read()
        assert Image.open(io.BytesIO(body)).size == (200, 200)
    finally:
        await client.close()


async def test_qr_cover_composite_runs_off_event_loop(
    provider: MSXBridgeProvider, mass_mock: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PIL compositing must run in a worker thread, never on the event loop."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    mass_mock.webserver.base_url = "http://ma.local:8095"
    mass_mock.http_session = _http_session_mock(_black_cover_png())
    loop_thread = threading.get_ident()
    stamp_threads: list[int] = []

    def _tracking_stamp(cover_bytes: bytes, qr_bytes: bytes) -> bytes:
        stamp_threads.append(threading.get_ident())
        return _stamp_qr_on_cover(cover_bytes, qr_bytes)

    monkeypatch.setattr(http_server_module, "_stamp_qr_on_cover", _tracking_stamp)
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get(
            "/api/party/qr-cover.png", params={"image": COVER_URL}, allow_redirects=False
        )
        assert resp.status == 200
        assert stamp_threads
        assert loop_thread not in stamp_threads
    finally:
        await client.close()


async def test_qr_cover_concurrent_misses_coalesce(
    provider: MSXBridgeProvider, mass_mock: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent requests for the same cover share one fetch and one composite."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    mass_mock.webserver.base_url = "http://ma.local:8095"
    release = asyncio.Event()
    mass_mock.http_session = _slow_http_session_mock(_black_cover_png(), release)
    stamp_calls: list[int] = []

    def _tracking_stamp(cover_bytes: bytes, qr_bytes: bytes) -> bytes:
        stamp_calls.append(1)
        return _stamp_qr_on_cover(cover_bytes, qr_bytes)

    monkeypatch.setattr(http_server_module, "_stamp_qr_on_cover", _tracking_stamp)
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        requests = [
            asyncio.ensure_future(
                client.get(
                    "/api/party/qr-cover.png", params={"image": COVER_URL}, allow_redirects=False
                )
            )
            for _ in range(5)
        ]
        await asyncio.sleep(0.05)
        release.set()
        responses = await asyncio.gather(*requests)
        assert all(r.status == 200 for r in responses)
        assert mass_mock.http_session.get.call_count == 1
        assert len(stamp_calls) == 1
    finally:
        await client.close()


async def test_qr_cover_render_survives_requester_cancellation(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A disconnected TV must not cancel the shared render; the cache still fills."""
    release = asyncio.Event()
    mass_mock.http_session = _slow_http_session_mock(_black_cover_png(), release)
    server = MSXHTTPServer(provider, 0)
    cache_key = (COVER_URL, "v1")

    task = server._qr_cover_task(cache_key, COVER_URL, JOIN_URL)
    assert server._qr_cover_task(cache_key, COVER_URL, JOIN_URL) is task
    waiter = asyncio.ensure_future(asyncio.shield(task))
    await asyncio.sleep(0)
    waiter.cancel()
    release.set()
    rendered = await task

    assert server._qr_cover_cache[cache_key] == rendered
    assert cache_key not in server._qr_cover_inflight


async def test_qr_cover_no_party_redirects_to_original(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Without an active party the endpoint redirects to the (allowed) original image."""
    mass_mock.webserver.base_url = "http://ma.local:8095"
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get(
            "/api/party/qr-cover.png", params={"image": COVER_URL}, allow_redirects=False
        )
        assert resp.status == 302
        assert resp.headers["Location"] == COVER_URL
    finally:
        await client.close()


async def test_qr_cover_disallowed_source_rejected(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """External-host image URLs are never fetched NOR redirected to (open redirect)."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    mass_mock.webserver.base_url = "http://ma.local:8095"
    mass_mock.http_session = _http_session_mock(_black_cover_png())
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        evil = "http://evil.example/img.png"
        resp = await client.get(
            "/api/party/qr-cover.png", params={"image": evil}, allow_redirects=False
        )
        assert resp.status == 400
        mass_mock.http_session.get.assert_not_called()
    finally:
        await client.close()


async def test_qr_cover_prefix_bypass_rejected(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A host that merely starts with an allowed base must be rejected (SSRF bypass)."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    mass_mock.webserver.base_url = "http://ma.local:8095"
    mass_mock.http_session = _http_session_mock(_black_cover_png())
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        bypass = "http://ma.local:8095.evil.example/img.png"
        resp = await client.get(
            "/api/party/qr-cover.png", params={"image": bypass}, allow_redirects=False
        )
        assert resp.status == 400
        mass_mock.http_session.get.assert_not_called()
    finally:
        await client.close()


async def test_qr_cover_fetch_does_not_follow_redirects(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """The cover fetch must not follow redirects (allowlisted host 302 -> loopback)."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    mass_mock.webserver.base_url = "http://ma.local:8095"
    mass_mock.http_session = _http_session_mock(_black_cover_png())
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get(
            "/api/party/qr-cover.png", params={"image": COVER_URL}, allow_redirects=False
        )
        assert resp.status == 200
        assert mass_mock.http_session.get.call_args.kwargs.get("allow_redirects") is False
    finally:
        await client.close()


async def test_qr_cover_fetch_failure_redirects(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A failing cover fetch degrades to a redirect, never a 500."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    mass_mock.webserver.base_url = "http://ma.local:8095"
    mass_mock.http_session = _http_session_mock(b"", status=404)
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get(
            "/api/party/qr-cover.png", params={"image": COVER_URL}, allow_redirects=False
        )
        assert resp.status == 302
        assert resp.headers["Location"] == COVER_URL
    finally:
        await client.close()


# --- Playlist background wiring ---


def _track_mock() -> Mock:
    track = Mock()
    track.name = "Test Track"
    track.uri = "library://track/1"
    track.duration = 180
    track.artist_str = "Artist"
    track.image = Mock()
    return track


def test_playlist_backgrounds_use_qr_cover_when_party_active(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """With a party active, playlist item backgrounds route through the compositor."""
    mass_mock.metadata.get_image_url = Mock(return_value=COVER_URL)
    playlist = map_tracks_to_msx_playlist(
        [_track_mock()],
        0,
        "http://tv-host:8099",
        "msx_test",
        provider,
        qr_cover_base="http://tv-host:8099/api/party/qr-cover.png",
    )
    assert playlist.items is not None
    item = playlist.items[0]
    assert item.background is not None
    assert item.background.startswith("http://tv-host:8099/api/party/qr-cover.png?image=")
    assert "cover.jpg" in item.background
    # the small thumbnail stays a clean cover
    assert item.image == COVER_URL


def test_playlist_qr_cover_uses_proxied_image_for_external_cover(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """
    QR-cover backgrounds wrap the MA-proxied cover URL, not the external one.

    A remotely-accessible cover resolves to an external CDN URL the qr-cover
    endpoint rejects (400) — the composited background must wrap the MA-proxied
    URL instead, so the cover still loads on the TV during a party.
    """

    def _get_image(_image: object, prefer_proxy: bool = False, **_kw: object) -> str:
        return "http://ma.local:8095/imageproxy/abc" if prefer_proxy else "https://cdn.ext/art.jpg"

    mass_mock.metadata.get_image_url = Mock(side_effect=_get_image)
    playlist = map_tracks_to_msx_playlist(
        [_track_mock()],
        0,
        "http://tv-host:8099",
        "msx_test",
        provider,
        qr_cover_base="http://tv-host:8099/api/party/qr-cover.png",
    )
    assert playlist.items is not None
    bg = playlist.items[0].background
    assert bg is not None
    inner = parse_qs(urlsplit(bg).query)["image"][0]
    assert inner == "http://ma.local:8095/imageproxy/abc"  # proxied, not external CDN
    # the small thumbnail stays the direct (non-proxied) cover
    assert playlist.items[0].image == "https://cdn.ext/art.jpg"


def test_playlist_backgrounds_unchanged_without_party(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Without a party, backgrounds keep the original cover URL."""
    mass_mock.metadata.get_image_url = Mock(return_value=COVER_URL)
    playlist = map_tracks_to_msx_playlist(
        [_track_mock()], 0, "http://tv-host:8099", "msx_test", provider
    )
    assert playlist.items is not None
    assert playlist.items[0].background == COVER_URL


# --- WS play background wiring ---


async def test_broadcast_play_rewrites_image_when_party_cached(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """broadcast_play stamps the QR into the play background while a party is active."""
    server = MSXHTTPServer(provider, 0)
    server._party_cache = (
        time.monotonic(),
        PartyInfo(join_url=JOIN_URL, name="My Party", qr_text=None, qr_version="abc123"),
    )
    server._client_prefixes["msx_test"] = "http://tv-host:8099"
    ws = AsyncMock()
    ws.closed = False
    server._ws_clients["msx_test"] = {ws}
    coros: list[Any] = []

    def _capture_task(coro: Any) -> Mock:
        coros.append(coro)
        return Mock()

    mass_mock.create_task = Mock(side_effect=_capture_task)

    server.broadcast_play("msx_test", image_url=COVER_URL, title="T")

    await coros[0]
    payload = json.loads(ws.send_str.call_args[0][0])
    assert payload["image_url"].startswith("http://tv-host:8099/api/party/qr-cover.png?image=")


async def test_broadcast_play_keeps_image_without_party(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """broadcast_play keeps the original background when no party is active."""
    server = MSXHTTPServer(provider, 0)
    ws = AsyncMock()
    ws.closed = False
    server._ws_clients["msx_test"] = {ws}
    coros: list[Any] = []

    def _capture_task(coro: Any) -> Mock:
        coros.append(coro)
        return Mock()

    mass_mock.create_task = Mock(side_effect=_capture_task)

    server.broadcast_play("msx_test", image_url=COVER_URL, title="T")

    await coros[0]
    payload = json.loads(ws.send_str.call_args[0][0])
    assert payload["image_url"] == COVER_URL
