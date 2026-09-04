"""Tests for the party QR cover compositor (spec 0004)."""

from __future__ import annotations

import asyncio
import io
import json
import threading
import time
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import segno
from aiohttp import web
from aiohttp.test_utils import TestClient as AiohttpTestClient
from aiohttp.test_utils import TestServer, make_mocked_request
from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import MediaItemImage
from PIL import Image

from music_assistant.helpers.util import join_task
from music_assistant.providers.msx_bridge import party as party_module
from music_assistant.providers.msx_bridge.http_server import MSXHTTPServer, PartyInfo
from music_assistant.providers.msx_bridge.mappers import PlaylistTrack, map_tracks_to_msx_playlist
from music_assistant.providers.msx_bridge.party import COVER_FETCH_MAX_BYTES, stamp_qr_on_cover
from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider
from tests.common import collect_loop_errors

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

    async def _chunks(_size: int) -> AsyncGenerator[bytes]:
        yield body

    resp = AsyncMock()
    resp.status = status
    resp.content.iter_chunked = Mock(side_effect=_chunks)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = Mock()
    session.get = Mock(return_value=cm)
    return session


def _failing_http_session_mock(release: asyncio.Event) -> Mock:
    """Return a mock session whose get() fails once released."""

    async def _gated_chunks(_size: int) -> AsyncGenerator[bytes]:
        await release.wait()
        if release.is_set():
            raise ConnectionResetError("connection reset while fetching the cover")
        yield b""

    resp = AsyncMock()
    resp.status = 200
    resp.content.iter_chunked = Mock(side_effect=_gated_chunks)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = Mock()
    session.get = Mock(return_value=cm)
    return session


def _slow_http_session_mock(body: bytes, release: asyncio.Event) -> Mock:
    """Return a mock session whose get() blocks reading the body until released."""

    async def _gated_chunks(_size: int) -> AsyncGenerator[bytes]:
        await release.wait()
        yield body

    resp = AsyncMock()
    resp.status = 200
    resp.content.iter_chunked = Mock(side_effect=_gated_chunks)
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
    stamped = stamp_qr_on_cover(cover, _qr_png())

    assert stamped != cover
    img = Image.open(io.BytesIO(stamped))
    assert img.size == (200, 200)
    # top-left quadrant stays untouched cover (black)
    assert img.convert("RGB").getpixel((10, 10)) == (0, 0, 0)
    # bottom-right quadrant contains white QR quiet-zone pixels
    quadrant = img.convert("RGB").crop((100, 100, 200, 200))
    assert any(px == (255, 255, 255) for px in list(quadrant.getdata()))


def test_stamp_qr_on_cover_resizes_large_cover() -> None:
    """Large decoded covers are reduced before compositing and caching."""
    stamped = stamp_qr_on_cover(_black_cover_png(2048), _qr_png())

    assert Image.open(io.BytesIO(stamped)).size == (1024, 1024)


def test_qr_cover_cache_enforces_byte_budget(
    provider: MSXBridgeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rendered covers evict least-recently-used entries by total bytes."""
    monkeypatch.setattr(party_module, "COVER_CACHE_MAX_BYTES", 10)
    server = MSXHTTPServer(provider, 0)

    server.party._cache_qr_cover(("first", "v1"), b"123456")
    server.party._cache_qr_cover(("second", "v1"), b"abcdef")

    assert list(server.party.qr_cover_cache) == [("second", "v1")]
    assert server.party._qr_cover_cache_bytes == 6


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
        return stamp_qr_on_cover(cover_bytes, qr_bytes)

    monkeypatch.setattr(party_module, "stamp_qr_on_cover", _tracking_stamp)
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
        return stamp_qr_on_cover(cover_bytes, qr_bytes)

    monkeypatch.setattr(party_module, "stamp_qr_on_cover", _tracking_stamp)
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


async def test_qr_cover_rejects_excess_unique_renders(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A third unique render redirects while both bounded slots are occupied."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    mass_mock.webserver.base_url = "http://ma.local:8095"
    release = asyncio.Event()
    mass_mock.http_session = _slow_http_session_mock(_black_cover_png(), release)
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    urls = [f"{COVER_URL}&id={index}" for index in range(3)]
    try:
        first = asyncio.create_task(
            client.get("/api/party/qr-cover.png", params={"image": urls[0]}, allow_redirects=False)
        )
        second = asyncio.create_task(
            client.get("/api/party/qr-cover.png", params={"image": urls[1]}, allow_redirects=False)
        )
        while len(server.party.qr_cover_inflight) < 2:
            await asyncio.sleep(0)

        excess = await client.get(
            "/api/party/qr-cover.png", params={"image": urls[2]}, allow_redirects=False
        )

        assert excess.status == 302
        assert excess.headers["Location"] == urls[2]
        assert len(server.party.qr_cover_inflight) == 2
        assert mass_mock.http_session.get.call_count == 2
        release.set()
        completed = await asyncio.gather(first, second)
        assert all(response.status == 200 for response in completed)
    finally:
        release.set()
        await client.close()


async def test_qr_cover_render_survives_requester_cancellation(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A disconnected TV must not cancel the shared render; the cache still fills."""
    release = asyncio.Event()
    mass_mock.http_session = _slow_http_session_mock(_black_cover_png(), release)
    server = MSXHTTPServer(provider, 0)
    cache_key = (COVER_URL, "v1")

    task = server.party.qr_cover_task(cache_key, COVER_URL, JOIN_URL)
    assert server.party.qr_cover_task(cache_key, COVER_URL, JOIN_URL) is task
    waiter = asyncio.ensure_future(join_task(task))
    await asyncio.sleep(0)
    waiter.cancel()
    release.set()
    rendered = await task

    assert server.party.qr_cover_cache[cache_key] == rendered
    assert cache_key not in server.party.qr_cover_inflight


async def test_qr_cover_render_failure_after_cancellation_logs_no_loop_error(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A render failing after one TV gave up reaches the waiting TV only, not the log."""
    release = asyncio.Event()
    mass_mock.get_provider = Mock(return_value=_party_mock())
    mass_mock.webserver.base_url = "http://ma.local:8095"
    mass_mock.http_session = _failing_http_session_mock(release)
    server = MSXHTTPServer(provider, 0)
    path = f"/api/party/qr-cover.png?{urlencode({'image': COVER_URL})}"

    with collect_loop_errors() as reported:
        gave_up = asyncio.create_task(
            server._handle_party_qr_cover(make_mocked_request("GET", path))
        )
        waiting = asyncio.create_task(
            server._handle_party_qr_cover(make_mocked_request("GET", path))
        )
        while not server.party.qr_cover_inflight and not gave_up.done():
            await asyncio.sleep(0)
        await asyncio.sleep(0)  # let the second TV join the same render
        gave_up.cancel()
        with pytest.raises(asyncio.CancelledError):
            await gave_up
        # release the fetch only once the cancellation is fully processed, so the failure
        # reliably lands after the TV that gave up is gone
        release.set()
        with pytest.raises(web.HTTPFound) as redirect:
            await waiting

    assert str(redirect.value.location) == COVER_URL
    assert mass_mock.http_session.get.call_count == 1
    assert reported == []


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


async def test_qr_cover_ignores_spoofed_request_host(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A crafted Host header must not allow fetching from that host."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    mass_mock.webserver.base_url = "http://ma.local:8095"
    mass_mock.http_session = _http_session_mock(_black_cover_png())
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        evil = "http://attacker.example:8099/img.png"
        resp = await client.get(
            "/api/party/qr-cover.png",
            params={"image": evil},
            headers={"Host": "attacker.example:8099"},
            allow_redirects=False,
        )
        assert resp.status == 400
        mass_mock.http_session.get.assert_not_called()
    finally:
        await client.close()


async def test_qr_cover_rejects_oversized_body(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A cover larger than the fetch cap must not be decoded."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    mass_mock.webserver.base_url = "http://ma.local:8095"
    mass_mock.http_session = _http_session_mock(_black_cover_png())
    resp = mass_mock.http_session.get.return_value.__aenter__.return_value
    resp.headers = {"Content-Length": str(COVER_FETCH_MAX_BYTES + 1)}
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        result = await client.get(
            "/api/party/qr-cover.png", params={"image": COVER_URL}, allow_redirects=False
        )
        assert result.status == 302
        resp.content.iter_chunked.assert_not_called()
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


async def test_qr_cover_decompression_bomb_redirects(
    provider: MSXBridgeProvider,
    mass_mock: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversized Pillow image degrades to the original cover redirect."""
    mass_mock.get_provider = Mock(return_value=_party_mock())
    mass_mock.webserver.base_url = "http://ma.local:8095"
    mass_mock.http_session = _http_session_mock(_black_cover_png())
    monkeypatch.setattr(
        Image,
        "open",
        Mock(side_effect=Image.DecompressionBombError("oversized cover")),
    )
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


def _track_mock() -> PlaylistTrack:
    return PlaylistTrack(
        name="Test Track",
        uri="library://track/1",
        duration=180,
        artist="Artist",
        image=MediaItemImage(type=ImageType.THUMB, path="cover.jpg", provider="library"),
    )


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
    server.party.cache = (
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
