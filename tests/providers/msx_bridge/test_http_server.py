"""Tests for MSXHTTPServer routes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch
from urllib.parse import parse_qs, quote, urlsplit

import aiohttp
import pytest
from aiohttp.test_utils import TestClient as AiohttpTestClient
from aiohttp.test_utils import TestServer
from music_assistant_models.enums import PlaybackState, RepeatMode
from music_assistant_models.errors import (
    InvalidDataError,
    MusicAssistantError,
    PlayerUnavailableError,
)
from music_assistant_models.media_items import Album, Artist, Playlist, Track
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.streams.constants import output_pacing_args
from music_assistant.providers.msx_bridge.audio_stream import _collect_prebuffer, build_audio_params
from music_assistant.providers.msx_bridge.constants import PRE_BUFFER_BYTES
from music_assistant.providers.msx_bridge.http_server import MSXHTTPServer
from music_assistant.providers.msx_bridge.mappers import map_track_to_msx
from music_assistant.providers.msx_bridge.player import MSXPlayer
from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider
from tests.providers.msx_bridge.factories import album as make_album
from tests.providers.msx_bridge.factories import artist as make_artist
from tests.providers.msx_bridge.factories import playlist as make_playlist
from tests.providers.msx_bridge.factories import track as make_track

if TYPE_CHECKING:
    from aiohttp.test_utils import TestClient

# --- Bootstrap and CORS ---


async def test_health(http_client: TestClient[Any, Any]) -> None:
    """GET /health should return 200 with status ok."""
    resp = await http_client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert data["provider"] == "msx_bridge"


async def test_root_html(http_client: TestClient[Any, Any]) -> None:
    """GET / should return 200 with text/html content."""
    resp = await http_client.get("/")
    assert resp.status == 200
    assert "text/html" in resp.headers["Content-Type"]
    body = await resp.text()
    assert "MSX" in body


async def test_root_html_escapes_host_header(http_client: TestClient[Any, Any]) -> None:
    """A crafted Host header must not be reflected unescaped (XSS)."""
    resp = await http_client.get("/", headers={"Host": 'evil"><script>alert(1)</script>'})
    assert resp.status == 200
    body = await resp.text()
    assert "<script>alert(1)</script>" not in body
    # the quote must be escaped so the host can't break out of href attributes
    assert 'evil">' not in body


async def test_root_html_has_no_web_kiosk(http_client: TestClient[Any, Any]) -> None:
    """The status page must not advertise the removed browser kiosk."""
    resp = await http_client.get("/")
    assert resp.status == 200
    body = await resp.text()
    assert 'id="kiosk-builder"' not in body
    assert 'href="/web"' not in body
    assert "/web?" not in body
    assert "sendspin" not in body.lower()


async def test_start_json(http_client: TestClient[Any, Any]) -> None:
    """GET /msx/start.json should return launcher menu config."""
    resp = await http_client.get("/msx/start.json")
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "Music Assistant"
    assert data["parameter"].startswith("content:")
    assert "/msx/launcher.json" in data["parameter"]
    assert "scripts" not in data


async def test_launcher_has_no_web_kiosk(http_client: TestClient[Any, Any]) -> None:
    """The MSX launcher offers MSX Player only — no browser kiosk shortcut."""
    resp = await http_client.get("/msx/launcher.json")
    assert resp.status == 200
    data = await resp.json()
    labels = [item.get("label") for item in data.get("items", [])]
    assert "MSX Player" in labels
    assert "Web Kiosk" not in labels
    blob = json.dumps(data)
    assert "/web" not in blob


async def test_plugin_html(http_client: TestClient[Any, Any]) -> None:
    """GET /msx/plugin.html should return HTML with interaction plugin."""
    resp = await http_client.get("/msx/plugin.html")
    assert resp.status == 200
    assert "text/html" in resp.headers["Content-Type"]
    body = await resp.text()
    assert "tvx.InteractionPlugin" in body
    assert "handleRequest" in body
    assert "MAHandler.prototype.handleEvent" in body
    assert 'data.event === "video:pause"' in body
    assert 'data.event === "video:seek"' in body
    assert "reportTvSeek" in body
    assert "reportTvSeek(pos) {\n        pausedAtPosition = pos;" in body
    assert 'msg.type === "sendspin"' not in body
    assert "pendingServerSeek" in body
    assert "clearPendingServerSeek" in body
    assert "seekedByServer" not in body
    assert resp.headers.get("Cache-Control") == "no-cache, no-store, must-revalidate"


async def test_tvx_lib(http_client: TestClient[Any, Any]) -> None:
    """GET /msx/tvx-plugin-module.min.js should return JS library."""
    resp = await http_client.get("/msx/tvx-plugin-module.min.js")
    assert resp.status == 200
    assert "javascript" in resp.headers["Content-Type"]


async def test_cors_headers(http_client: TestClient[Any, Any]) -> None:
    """Responses should include CORS Access-Control-Allow-Origin header."""
    resp = await http_client.get("/health")
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


# --- Stream proxy ---


async def test_stream_player_not_found(http_client: TestClient[Any, Any]) -> None:
    """GET /stream/nonexistent should return 404."""
    resp = await http_client.get("/stream/nonexistent")
    assert resp.status == 404


async def test_stream_no_media(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /stream/{id} should return 404 when player has no current media."""
    mock_player = Mock(spec=MSXPlayer)
    mock_player.current_media = None
    token = provider.get_stream_token("msx_test")
    mass_mock.players.get.return_value = mass_mock.players.get_player.return_value = mock_player

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get(f"/stream/msx_test?token={token}")
        assert resp.status == 404
        body = await resp.text()
        assert "No active stream" in body
    finally:
        await client.close()


async def test_stream_token_is_unguessable_and_per_player(
    provider: MSXBridgeProvider,
) -> None:
    """Tokens must be random and distinct per player, not a shared constant."""
    first = provider.get_stream_token("msx_a")
    second = provider.get_stream_token("msx_b")
    assert first
    assert second
    assert first != second
    assert len(first) >= 16


async def test_stream_token_survives_player_reregistration(
    provider: MSXBridgeProvider,
) -> None:
    """
    A token outlives the player object.

    An idle TV is unregistered after the configured timeout; rotating there would
    strand the URLs a long-running kiosk already cached.
    """
    token = provider.get_stream_token("msx_kiosk")
    assert provider.get_stream_token("msx_kiosk") == token


async def test_audio_routes_send_no_cors_header(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Audio must not be readable by a cross-origin fetch, unlike the MSX content pages."""
    token = provider.get_stream_token("msx_test")
    mock_player = Mock(spec=MSXPlayer)
    mock_player.current_media = None
    mass_mock.players.get.return_value = mass_mock.players.get_player.return_value = mock_player

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get(f"/stream/msx_test?token={token}")
        assert resp.headers.get("Access-Control-Allow-Origin") is None
        resp = await client.get("/msx/audio/msx_test")
        assert resp.headers.get("Access-Control-Allow-Origin") is None
        # the MSX content pages still need it — the MSX app loads them cross-origin
        resp = await client.get("/msx/menu.json")
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    finally:
        await client.close()


async def test_stream_rejects_missing_token(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /stream/{id} without the player's token should be refused."""
    mock_player = Mock(spec=MSXPlayer)
    mock_player.current_media = PlayerMedia(uri="library://track/1")
    mass_mock.players.get.return_value = mass_mock.players.get_player.return_value = mock_player

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/stream/msx_test")
        assert resp.status == 403
        resp = await client.get("/stream/msx_test?token=wrong")
        assert resp.status == 403
    finally:
        await client.close()


async def test_stream_not_msx_player(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /stream/{id} should return 404 for a non-MSX player."""
    # Return a plain Mock (not spec=MSXPlayer)
    non_msx_player = Mock()
    mass_mock.players.get.return_value = mass_mock.players.get_player.return_value = non_msx_player

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/stream/other_player")
        assert resp.status == 404
        body = await resp.text()
        assert "Player not found" in body
    finally:
        await client.close()


@pytest.mark.skip(reason="stream test hangs with TestClient/streaming on some platforms")
async def test_stream_success(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /stream/{id} should stream audio via internal API."""
    mock_player = Mock(spec=MSXPlayer)
    mock_media = PlayerMedia(uri="library://track/1", duration=180)
    mock_player.current_media = mock_media
    mock_player.output_format = "mp3"
    token = provider.get_stream_token("msx_test")
    mass_mock.players.get.return_value = mass_mock.players.get_player.return_value = mock_player

    # Mock get_stream to return an async generator
    mass_mock.streams = Mock()
    mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm-data"]))
    mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=InvalidDataError("no session"))

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        chunks = [b"encoded-chunk-1", b"encoded-chunk-2"]
        with patch(
            "music_assistant.providers.msx_bridge.audio_stream.get_ffmpeg_stream",
            return_value=_async_iter(chunks),
        ):
            resp = await client.get(f"/stream/msx_test?token={token}")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "audio/mpeg"
            body = await resp.read()
            assert b"encoded-chunk-1" in body
            assert b"encoded-chunk-2" in body
    finally:
        await client.close()


# --- Library API ---


async def test_albums(http_client: TestClient[Any, Any]) -> None:
    """GET /api/albums should return items list."""
    resp = await http_client.get("/api/albums")
    assert resp.status == 200
    data = await resp.json()
    assert "items" in data
    assert "total" in data


async def test_albums_with_data(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /api/albums should format album data correctly."""
    album = make_album("1", "Test Album", artists=[make_artist(name="Test Artist")])
    mass_mock.music.albums.library_items.return_value = [album]

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/api/albums")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "Test Album"
        assert data["items"][0]["artist"] == "Test Artist"
        assert data["total"] == 1
    finally:
        await client.close()


async def test_album_tracks(http_client: TestClient[Any, Any]) -> None:
    """GET /api/albums/{id}/tracks should return items list."""
    resp = await http_client.get("/api/albums/1/tracks")
    assert resp.status == 200
    data = await resp.json()
    assert "items" in data


async def test_artists(http_client: TestClient[Any, Any]) -> None:
    """GET /api/artists should return items list."""
    resp = await http_client.get("/api/artists")
    assert resp.status == 200
    data = await resp.json()
    assert "items" in data
    assert "total" in data


async def test_playlists(http_client: TestClient[Any, Any]) -> None:
    """GET /api/playlists should return items list."""
    resp = await http_client.get("/api/playlists")
    assert resp.status == 200
    data = await resp.json()
    assert "items" in data
    assert "total" in data


async def test_tracks(http_client: TestClient[Any, Any]) -> None:
    """GET /api/tracks should return items list."""
    resp = await http_client.get("/api/tracks")
    assert resp.status == 200
    data = await resp.json()
    assert "items" in data
    assert "total" in data


async def test_search(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /api/search?q=test should return search results."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/api/search?q=test")
        assert resp.status == 200
        data = await resp.json()
        assert "artists" in data
        assert "albums" in data
        assert "tracks" in data
        assert "playlists" in data
        mass_mock.music.search.assert_awaited_once()
    finally:
        await client.close()


async def test_search_missing_query(http_client: TestClient[Any, Any]) -> None:
    """GET /api/search without q parameter should return 400."""
    resp = await http_client.get("/api/search")
    assert resp.status == 400
    data = await resp.json()
    assert "error" in data


@pytest.mark.parametrize("path", ["/msx/search-input.json?q=beatles", "/msx/search-input.json"])
async def test_msx_search_input_marks_page_compressed(
    http_client: TestClient[Any, Any], path: str
) -> None:
    """
    Search keyboard results must set compress so old MSX can show them.

    The bundled Input Plugin sets template.decompress when compress is missing.
    That decompress path only exists on Media Station X 0.1.155+; older builds
    then show "Decompression not supported" instead of the hits.
    """
    resp = await http_client.get(path)
    assert resp.status == 200
    data = await resp.json()
    assert data["compress"] is True
    assert "template" in data


@pytest.mark.parametrize("error_type", [MusicAssistantError, TimeoutError])
@pytest.mark.parametrize(
    ("route", "controller_name", "method_name", "async_method", "has_placeholder"),
    [
        ("/msx/albums.json", "albums", "library_items", True, True),
        ("/msx/artists.json", "artists", "library_items", True, True),
        ("/msx/playlists.json", "playlists", "library_items", True, True),
        ("/msx/tracks.json", "tracks", "library_items", True, True),
        ("/msx/recently-played.json", "tracks", "library_items", True, True),
        ("/msx/albums/album-1/tracks.json", "albums", "tracks", True, True),
        ("/msx/artists/artist-1/albums.json", "artists", "albums", True, True),
        ("/msx/playlists/playlist-1/tracks.json", "playlists", "tracks", False, True),
        ("/msx/playlist/album/album-1.json", "albums", "tracks", True, False),
        (
            "/msx/playlist/playlist/playlist-1.json",
            "playlists",
            "tracks",
            False,
            False,
        ),
    ],
)
async def test_msx_library_pages_fail_soft_for_expected_errors(
    provider: MSXBridgeProvider,
    mass_mock: Mock,
    route: str,
    controller_name: str,
    method_name: str,
    async_method: bool,
    has_placeholder: bool,
    error_type: type[Exception],
) -> None:
    """Expected MA and timeout failures return an empty MSX page."""
    controller = getattr(mass_mock.music, controller_name)
    failing_method = (
        AsyncMock(side_effect=error_type("library unavailable"))
        if async_method
        else Mock(side_effect=error_type("library unavailable"))
    )
    setattr(controller, method_name, failing_method)

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        response = await client.get(route)
        assert response.status == 200
        assert bool((await response.json())["items"]) is has_placeholder
    finally:
        await client.close()


# --- Playback control ---


def _register_msx_player(mass_mock: Mock, provider: MSXBridgeProvider, player_id: str) -> MSXPlayer:
    """Create an MSXPlayer and register it with the mass_mock so _get_msx_player passes."""
    player = MSXPlayer(provider=provider, player_id=player_id)
    mass_mock.players.get_player = Mock(
        side_effect=lambda pid, **_kwargs: player if pid == player_id else None
    )
    return player


async def test_play_track(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """POST /api/play should call player_queues.play_media."""
    _register_msx_player(mass_mock, provider, "msx_test")
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.post(
            "/api/play",
            json={"track_uri": "library://track/1", "player_id": "msx_test"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        mass_mock.player_queues.play_media.assert_awaited_once_with("msx_test", "library://track/1")
    finally:
        await client.close()


async def test_play_track_uses_external_hardware_context(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Unauthenticated MSX playback should use the external hardware context."""
    _register_msx_player(mass_mock, provider, "msx_test")
    context = MagicMock()
    context.__aenter__ = AsyncMock()
    context.__aexit__ = AsyncMock()

    async def assert_owner_context(_player_id: str, _uri: str) -> None:
        context.__aenter__.assert_awaited_once()
        context.__aexit__.assert_not_awaited()

    mass_mock.player_queues.play_media.side_effect = assert_owner_context
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        with (
            patch(
                "music_assistant.providers.msx_bridge.http_server.ImpersonatedUser",
                return_value=context,
            ) as impersonated,
        ):
            response = await client.post(
                "/api/play",
                json={"track_uri": "library://track/1", "player_id": "msx_test"},
            )

        assert response.status == 200
        impersonated.assert_called_once_with(mass_mock, None)
        context.__aexit__.assert_awaited_once()
    finally:
        await client.close()


async def test_play_context_enqueues_container_then_index(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """GET /api/play-context plays the container, then jumps to start index."""
    player = _register_msx_player(mass_mock, provider, "msx_test")
    items = [
        _make_queue_item("library://track/11", queue_item_id="a"),
        _make_queue_item("library://track/12", queue_item_id="b"),
        _make_queue_item("library://track/13", queue_item_id="c"),
    ]
    _wire_queue(mass_mock, items)
    mass_mock.player_queues.play_media = AsyncMock()
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        with patch.object(player, "wait_for_media", AsyncMock(return_value=player.current_media)):
            resp = await client.get("/api/play-context/msx_test?uri=library://album/9&start=2")
        assert resp.status == 200
        mass_mock.player_queues.play_media.assert_awaited_once_with("msx_test", "library://album/9")
        mass_mock.player_queues.play_index.assert_awaited_once_with("msx_test", "c")
        data = await resp.json()
        action = data["response"]["data"]["action"]
        assert action.startswith("playlist:")
        assert "/msx/queue-playlist/msx_test.json" in action
        assert "start=" in action
    finally:
        await client.close()


async def test_play_context_uses_external_hardware_context(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Unauthenticated MSX menu playback should use the external hardware context."""
    player = _register_msx_player(mass_mock, provider, "msx_test")
    context = MagicMock()
    context.__aenter__ = AsyncMock()
    context.__aexit__ = AsyncMock()

    async def assert_owner_context(_player_id: str, _uri: str) -> None:
        context.__aenter__.assert_awaited_once()
        context.__aexit__.assert_not_awaited()

    mass_mock.player_queues.play_media.side_effect = assert_owner_context
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        with (
            patch.object(player, "wait_for_media", AsyncMock(return_value=player.current_media)),
            patch(
                "music_assistant.providers.msx_bridge.http_server.ImpersonatedUser",
                return_value=context,
            ) as impersonated,
        ):
            response = await client.get("/api/play-context/msx_test?uri=library://album/9&start=0")

        assert response.status == 200
        impersonated.assert_called_once_with(mass_mock, None)
        context.__aexit__.assert_awaited_once()
    finally:
        await client.close()


async def test_play_context_preserves_index_above_ten_thousand(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Container playback must not clamp a valid long-queue index."""
    player = _register_msx_player(mass_mock, provider, "msx_test")
    items = [
        _make_queue_item(f"library://track/{index}", queue_item_id=str(index))
        for index in range(12002)
    ]
    _wire_queue(mass_mock, items)
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        with patch.object(player, "wait_for_media", AsyncMock(return_value=player.current_media)):
            response = await client.get(
                "/api/play-context/msx_test?uri=library://album/9&start=12001"
            )
        assert response.status == 200
        mass_mock.player_queues.play_index.assert_awaited_once_with("msx_test", "12001")
    finally:
        await client.close()


async def test_play_context_starts_at_track_uri(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """GET /api/play-context prefers the selected track over the numeric index."""
    player = _register_msx_player(mass_mock, provider, "msx_test")
    items = [
        _make_queue_item("library://track/11", queue_item_id="a"),
        _make_queue_item("library://track/12", queue_item_id="b"),
        _make_queue_item("library://track/13", queue_item_id="c"),
    ]
    _wire_queue(mass_mock, items)
    mass_mock.player_queues.play_media = AsyncMock()
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        with patch.object(player, "wait_for_media", AsyncMock(return_value=player.current_media)):
            resp = await client.get(
                "/api/play-context/msx_test?uri=library://album/9&start=0&track=library://track/13"
            )
        assert resp.status == 200
        mass_mock.player_queues.play_index.assert_awaited_once_with("msx_test", "c")
    finally:
        await client.close()


async def test_play_context_prefers_start_index_for_duplicate_uri(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """The clicked index wins when the same track URI appears twice."""
    player = _register_msx_player(mass_mock, provider, "msx_test")
    items = [
        _make_queue_item("library://track/11", queue_item_id="a"),
        _make_queue_item("library://track/12", queue_item_id="b"),
        _make_queue_item("library://track/11", queue_item_id="c"),
    ]
    _wire_queue(mass_mock, items)
    mass_mock.player_queues.play_media = AsyncMock()
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        with patch.object(player, "wait_for_media", AsyncMock(return_value=player.current_media)):
            resp = await client.get(
                "/api/play-context/msx_test?uri=library://album/9&start=2&track=library://track/11"
            )
        assert resp.status == 200
        mass_mock.player_queues.play_index.assert_awaited_once_with("msx_test", "c")
    finally:
        await client.close()


async def test_play_context_skips_index_when_already_current(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """play-context must not call play_index when play_media already selected the item."""
    player = _register_msx_player(mass_mock, provider, "msx_test")
    items = [_make_queue_item("library://track/11", queue_item_id="a")]
    _wire_queue(mass_mock, items)
    mass_mock.player_queues.play_media = AsyncMock()
    player._attr_current_media = PlayerMedia(uri="library://track/11", queue_item_id="a")
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        with patch.object(player, "wait_for_media", AsyncMock(return_value=player.current_media)):
            resp = await client.get(
                "/api/play-context/msx_test?uri=library://track/11&start=0&track=library://track/11"
            )
        assert resp.status == 200
        mass_mock.player_queues.play_index.assert_not_awaited()
    finally:
        await client.close()


async def test_play_context_recovers_when_player_was_marked_unavailable(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A TV that still sends play-context is online even if its last WebSocket dropped."""
    player = _register_msx_player(mass_mock, provider, "msx_test")
    player.update_state = Mock()  # type: ignore[misc,method-assign]
    player._attr_available = False
    player._attr_playback_state = PlaybackState.PLAYING

    def get_player(
        pid: str, raise_unavailable: bool = False, **_kwargs: object
    ) -> MSXPlayer | None:
        if pid != "msx_test":
            return None
        if raise_unavailable and not player.available:
            raise PlayerUnavailableError("not available")
        return player

    async def play_media(pid: str, _uri: str) -> None:
        mass_mock.players.get_player(pid, True)

    mass_mock.players.get_player = Mock(side_effect=get_player)
    mass_mock.player_queues.play_media = AsyncMock(side_effect=play_media)
    _wire_queue(mass_mock, [_make_queue_item("library://track/11", queue_item_id="a")])
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        response = await client.get("/api/play-context/msx_test?uri=library://track/11")
        assert response.status == 200
        data = await response.json()
        assert data["response"]["status"] == 200
        assert player.available is True
        mass_mock.player_queues.play_media.assert_awaited_once()
    finally:
        await client.close()


async def test_play_context_returns_msx_error_when_queue_loading_fails(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Queue-loading errors must not make MSX receive an HTTP 500."""
    _register_msx_player(mass_mock, provider, "msx_test")
    mass_mock.player_queues.play_media = AsyncMock(
        side_effect=MusicAssistantError("source authentication expired")
    )
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        response = await client.get("/api/play-context/msx_test?uri=library://track/11")
        assert response.status == 200
        data = await response.json()
        assert data["response"]["status"] == 503
        assert data["response"]["message"] == "Unable to start playback"
    finally:
        await client.close()


async def test_play_context_does_not_hide_programming_errors(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Unexpected queue defects must surface as HTTP 500 instead of an MSX-safe error."""
    _register_msx_player(mass_mock, provider, "msx_test")
    mass_mock.player_queues.play_media.side_effect = RuntimeError("programming defect")
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        response = await client.get("/api/play-context/msx_test?uri=library://track/11")

        assert response.status == 500
    finally:
        await client.close()


async def test_play_unknown_player(provider: MSXBridgeProvider) -> None:
    """POST /api/play with unknown player_id should return 404."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.post(
            "/api/play",
            json={"track_uri": "library://track/1", "player_id": "msx_test"},
        )
        assert resp.status == 404
    finally:
        await client.close()


async def test_play_invalid_body(http_client: TestClient[Any, Any]) -> None:
    """POST /api/play with invalid JSON should return 400."""
    resp = await http_client.post(
        "/api/play",
        data=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


async def test_play_unknown_charset_is_400(http_client: TestClient[Any, Any]) -> None:
    """POST /api/play with an unknown JSON charset should return 400, not 500."""
    resp = await http_client.post(
        "/api/play",
        data=b'{"track_uri":"library://track/1","player_id":"msx_test"}',
        headers={"Content-Type": "application/json; charset=invalid"},
    )
    assert resp.status == 400


async def test_pause(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """POST /api/pause/{id} should call cmd_pause."""
    _register_msx_player(mass_mock, provider, "msx_test")
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.post("/api/pause/msx_test")
        assert resp.status == 200
        mass_mock.players.cmd_pause.assert_awaited_once_with("msx_test")
    finally:
        await client.close()


async def test_stop(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """POST /api/stop/{id} should call cmd_stop."""
    _register_msx_player(mass_mock, provider, "msx_test")
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.post("/api/stop/msx_test")
        assert resp.status == 200
        mass_mock.players.cmd_stop.assert_awaited_once_with("msx_test")
    finally:
        await client.close()


async def test_quick_stop(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """POST /api/quick-stop/{id} should call cmd_stop and notify_play_stopped."""
    _register_msx_player(mass_mock, provider, "msx_test")
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        with patch.object(provider, "notify_play_stopped", Mock()) as mock_notify:
            resp = await client.post("/api/quick-stop/msx_test")
        assert resp.status == 200
        mass_mock.players.cmd_stop.assert_awaited_once_with("msx_test")
        mock_notify.assert_called_once_with("msx_test")
    finally:
        await client.close()


async def test_next_at_queue_end_does_not_reload_playlist(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Complete/next at the last item must not restart that track."""
    _register_msx_player(mass_mock, provider, "msx_test")
    mass_mock.player_queues.get_active_queue = Mock(
        return_value=Mock(current_index=4, repeat_mode=RepeatMode.OFF)
    )
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/api/next/msx_test")
        assert resp.status == 200
        data = await resp.json()
        assert data["response"]["data"]["action"] == "[]"
    finally:
        await client.close()


async def test_next_reloads_playlist_when_queue_advances(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Next that moves the queue must return a rotated playlist action."""
    _register_msx_player(mass_mock, provider, "msx_test")
    indexes = [0]

    def _queue(*_a: object, **_k: object) -> Mock:
        idx = indexes[0]
        if idx == 0:
            indexes[0] = 1
        return Mock(current_index=idx, repeat_mode=RepeatMode.OFF, queue_id="msx_test", items=2)

    mass_mock.player_queues.get_active_queue = Mock(side_effect=_queue)
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/api/next/msx_test")
        assert resp.status == 200
        data = await resp.json()
        assert "/msx/queue-playlist/msx_test.json" in data["response"]["data"]["action"]
    finally:
        await client.close()


async def test_control_unknown_player(provider: MSXBridgeProvider) -> None:
    """Control endpoints with unknown player_id should return 404."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        for path in ("/api/pause/unknown", "/api/stop/unknown", "/api/quick-stop/unknown"):
            resp = await client.post(path)
            assert resp.status == 404, f"{path} should return 404 for unknown player"
    finally:
        await client.close()


async def test_control_endpoints_reject_cross_site(http_client: TestClient[Any, Any]) -> None:
    """
    State-changing endpoints must reject browser cross-site requests (CSRF).

    Any web page can fire a GET via an img/script tag; modern browsers stamp
    such requests with Sec-Fetch-Site: cross-site. The rejection must happen
    before the player lookup so probing is impossible too.
    """
    headers = {"Sec-Fetch-Site": "cross-site"}
    for path in (
        "/api/pause/msx_x",
        "/api/stop/msx_x",
        "/api/quick-stop/msx_x",
        "/api/next/msx_x",
        "/api/previous/msx_x",
    ):
        resp = await http_client.get(path, headers=headers)
        assert resp.status == 403, f"{path} must reject cross-site GET"

    resp = await http_client.post(
        "/api/play",
        json={"track_uri": "library://track/1", "player_id": "msx_x"},
        headers=headers,
    )
    assert resp.status == 403, "/api/play must reject cross-site POST"


async def test_control_endpoints_reject_same_site(http_client: TestClient[Any, Any]) -> None:
    """A sibling service on another port must not pass the CSRF guard."""
    response = await http_client.get("/api/pause/msx_x", headers={"Sec-Fetch-Site": "same-site"})
    assert response.status == 403


async def test_control_endpoints_allow_same_origin(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Same-origin browser requests (web player, MSX plugin) must still work."""
    _register_msx_player(mass_mock, provider, "msx_test")
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/api/pause/msx_test", headers={"Sec-Fetch-Site": "same-origin"})
        assert resp.status == 200
        mass_mock.players.cmd_pause.assert_awaited_once_with("msx_test")
    finally:
        await client.close()


async def test_websocket_rejects_cross_origin_browser(
    provider: MSXBridgeProvider,
) -> None:
    """A browser from another origin cannot claim a TV control socket."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        with pytest.raises(aiohttp.WSServerHandshakeError) as err:
            await client.ws_connect(
                "/ws?device_id=LivingRoom",
                headers={
                    "Origin": "https://attacker.example",
                    "Sec-Fetch-Site": "cross-site",
                },
            )
        assert err.value.status == 403
        assert provider.players == []
    finally:
        await client.close()


async def test_websocket_allows_originless_native_client(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """The native MSX client may connect without browser security headers."""
    _register_msx_player(mass_mock, provider, "msx_LivingRoom")
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws?device_id=LivingRoom")
        assert not ws.closed
        await ws.close()
    finally:
        await client.close()


async def test_control_endpoints_reject_unexpected_methods(
    http_client: TestClient[Any, Any],
) -> None:
    """Control endpoints accept only GET and POST — no wildcard methods."""
    resp = await http_client.delete("/api/pause/msx_x")
    assert resp.status == 405


# --- MSX content page actions ---


def _make_album(item_id: int = 1, name: str = "Test Album") -> Album:
    """Create an album returned by the MA library API."""
    return make_album(str(item_id), name, artists=[make_artist(name="Test Artist")])


def _make_track(
    item_id: int = 1, name: str = "Test Track", artist_name: str = "Test Artist"
) -> Track:
    """Create a track returned by the MA library API."""
    artists = [make_artist(name=artist_name)] if artist_name else []
    return make_track(
        str(item_id), name, artists=artists, album=make_album(name="Test Album"), duration=180
    )


def _make_artist(item_id: int = 1, name: str = "Test Artist") -> Artist:
    """Create an artist returned by the MA library API."""
    return make_artist(str(item_id), name)


def _make_playlist(item_id: int = 1, name: str = "Test Playlist") -> Playlist:
    """Create a playlist returned by the MA library API."""
    return make_playlist(str(item_id), name)


def _make_audio_player(mass_mock: Mock) -> tuple[MSXPlayer, PlayerMedia]:
    """Wire a real MSX player with queue-backed media into the controller mock."""
    provider = Mock()
    provider.mass = mass_mock
    player = MSXPlayer(provider, "msx_test", name="Test TV", output_format="mp3")
    media = PlayerMedia(
        uri="library://track/1",
        title=None,
        artist=None,
        album=None,
        image_url=None,
        duration=180,
    )
    player._attr_current_media = media
    cast("Any", player).wait_for_media = AsyncMock(return_value=media)
    mass_mock.players.get.return_value = mass_mock.players.get_player.return_value = player
    return player, media


async def test_msx_albums_have_action(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/albums.json items should have content: action for drill-down."""
    album = _make_album()
    mock_result = Mock()
    mock_result.__iter__ = Mock(return_value=iter([album]))
    mass_mock.music.albums.library_items.return_value = mock_result

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/albums.json")
        assert resp.status == 200
        data = await resp.json()
        item = data["items"][0]
        assert "action" in item
        assert item["action"].startswith("content:")
        assert "/msx/albums/1/tracks.json" in item["action"]
    finally:
        await client.close()


async def test_msx_artists_have_action(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/artists.json items should have content: action for drill-down."""
    artist = _make_artist()
    mock_result = Mock()
    mock_result.__iter__ = Mock(return_value=iter([artist]))
    mass_mock.music.artists.library_items.return_value = mock_result

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/artists.json")
        assert resp.status == 200
        data = await resp.json()
        item = data["items"][0]
        assert "action" in item
        assert item["action"].startswith("content:")
        assert "/msx/artists/1/albums.json" in item["action"]
    finally:
        await client.close()


async def test_msx_playlists_have_action(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/playlists.json items should have content: action for drill-down."""
    playlist = _make_playlist()
    mock_result = Mock()
    mock_result.__iter__ = Mock(return_value=iter([playlist]))
    mass_mock.music.playlists.library_items.return_value = mock_result

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/playlists.json")
        assert resp.status == 200
        data = await resp.json()
        item = data["items"][0]
        assert "action" in item
        assert item["action"].startswith("content:")
        assert "/msx/playlists/1/tracks.json" in item["action"]
    finally:
        await client.close()


async def test_msx_tracks_have_action(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/tracks.json items should enqueue the track into the MA queue."""
    track = _make_track()
    mock_result = Mock()
    mock_result.__iter__ = Mock(return_value=iter([track]))
    mass_mock.music.tracks.library_items.return_value = mock_result

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/tracks.json")
        assert resp.status == 200
        data = await resp.json()
        item = data["items"][0]
        assert "action" in item
        assert item["action"].startswith("execute:")
        assert "/api/play-context/" in item["action"]
        assert "uri=library%3A%2F%2Ftrack%2F1" in item["action"]
        assert item["titleHeader"] == "{txt:msx-white:Test Track}"
        assert "playerLabel" in item
        assert item["playerLabel"] == "Test Track"
    finally:
        await client.close()


# --- MSX detail pages ---


async def test_msx_album_tracks(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/albums/{id}/tracks.json should return tracks with audio actions."""
    track = _make_track()
    mass_mock.music.albums.tracks.return_value = [track]

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/albums/1/tracks.json")
        assert resp.status == 200
        data = await resp.json()
        assert data["headline"] == "Album Tracks"
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["titleHeader"] == "{txt:msx-white:Test Track}"
        assert item["action"].startswith("execute:")
        assert "/api/play-context/" in item["action"]
        assert "uri=library%3A%2F%2Falbum%2F1" in item["action"]
    finally:
        await client.close()


async def test_msx_artist_albums(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """Artist detail requests must retain the source provider."""
    album = _make_album()
    mass_mock.music.artists.albums.return_value = [album]

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/artists/5531642/albums.json?provider=yandex_music--test")
        assert resp.status == 200
        mass_mock.music.artists.albums.assert_awaited_once_with("5531642", "yandex_music--test")
        data = await resp.json()
        assert data["headline"] == "Artist Albums"
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["title"] == "Test Album"
        assert item["action"].startswith("content:")
        assert "/msx/albums/1/tracks.json" in item["action"]
    finally:
        await client.close()


async def test_msx_playlist_tracks(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/playlists/{id}/tracks.json should return tracks with audio actions."""
    track = _make_track()

    async def _mock_playlist_tracks(*_args: object, **_kwargs: object) -> AsyncGenerator[Any]:
        yield track

    mass_mock.music.playlists.tracks = Mock(side_effect=lambda *_a, **_k: _mock_playlist_tracks())

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/playlists/1/tracks.json")
        assert resp.status == 200
        data = await resp.json()
        assert data["headline"] == "Playlist Tracks"
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["titleHeader"] == "{txt:msx-white:Test Track}"
        assert item["action"].startswith("execute:")
        assert "/api/play-context/" in item["action"]
        assert "uri=library%3A%2F%2Fplaylist%2F1" in item["action"]
    finally:
        await client.close()


async def test_broadcast_play_path_carries_token(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """The pushed stream path must carry the token the /stream route now requires."""
    token = provider.get_stream_token("msx_test")

    server = MSXHTTPServer(provider, 0)
    ws = AsyncMock()
    ws.closed = False
    server._ws_clients["msx_test"] = {ws}
    coros: list[Any] = []

    def _capture_task(coro: Any) -> Mock:
        coros.append(coro)
        return Mock()

    mass_mock.create_task = Mock(side_effect=_capture_task)

    server.broadcast_play("msx_test", title="T")

    await coros[0]
    payload = json.loads(ws.send_str.call_args[0][0])
    assert payload["path"] == f"/stream/msx_test?token={token}"


# --- Stream and WebSocket error boundaries ---


def test_cancel_streams_continues_after_transport_abort_error(
    provider: MSXBridgeProvider,
) -> None:
    """One closing transport must not prevent the remaining transports from aborting."""
    server = MSXHTTPServer(provider, 0)
    failing_transport = Mock()
    failing_transport.abort.side_effect = OSError("already closed")
    healthy_transport = Mock()
    server._active_stream_transports["msx_test"] = {
        failing_transport,
        healthy_transport,
    }

    server.cancel_streams_for_player("msx_test")

    failing_transport.abort.assert_called_once_with()
    healthy_transport.abort.assert_called_once_with()


@pytest.mark.parametrize("error_type", [MusicAssistantError, OSError])
async def test_run_stream_task_logs_expected_errors_and_unregisters(
    provider: MSXBridgeProvider,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
) -> None:
    """Expected stream failures are logged and always unregistered."""
    server = MSXHTTPServer(provider, 0)

    async def _fail() -> None:
        raise error_type("stream failed")

    stream_task = asyncio.create_task(_fail())
    await server.audio.run_stream_task("msx_test", stream_task, None)

    assert "Stream error for player msx_test" in caplog.text
    assert "msx_test" not in server._active_stream_tasks


async def test_run_stream_task_propagates_unexpected_error_and_unregisters(
    provider: MSXBridgeProvider,
) -> None:
    """Programming errors escape the stream boundary after cleanup."""
    server = MSXHTTPServer(provider, 0)

    async def _fail() -> None:
        raise ValueError("bug")

    stream_task = asyncio.create_task(_fail())
    with pytest.raises(ValueError, match="bug"):
        await server.audio.run_stream_task("msx_test", stream_task, None)

    assert "msx_test" not in server._active_stream_tasks


async def test_ws_send_discards_client_after_connection_error(
    provider: MSXBridgeProvider,
) -> None:
    """A failed WebSocket connection is removed from the subscribed clients."""
    server = MSXHTTPServer(provider, 0)
    ws = Mock()
    ws.send_str = AsyncMock(side_effect=aiohttp.ClientConnectionError("closed"))
    server._ws_clients["msx_test"] = {ws}

    await server._ws_send(ws, "payload", "msx_test")

    assert ws not in server._ws_clients["msx_test"]


async def test_ws_send_propagates_unexpected_error(provider: MSXBridgeProvider) -> None:
    """Programming errors from WebSocket serialization are not hidden."""
    server = MSXHTTPServer(provider, 0)
    ws = Mock()
    ws.send_str = AsyncMock(side_effect=ValueError("bug"))
    server._ws_clients["msx_test"] = {ws}

    with pytest.raises(ValueError, match="bug"):
        await server._ws_send(ws, "payload", "msx_test")

    assert ws in server._ws_clients["msx_test"]


# --- MSX audio endpoint ---


async def test_msx_audio_missing_uri(http_client: TestClient[Any, Any]) -> None:
    """GET /msx/audio/msx_default without ?uri= should return 400."""
    resp = await http_client.get("/msx/audio/msx_default")
    assert resp.status == 400
    body = await resp.text()
    assert "uri" in body.lower()  # "Missing uri" or "Invalid uri parameter"


@pytest.mark.parametrize(
    "uri",
    [
        "http://evil.example/payload.mp3",
        "https://evil.example/x",
        "rtsp://evil.example/x",
        # the same destination wrapped in a builtin uri — parse_uri resolves both to
        # ('builtin', 'http://evil.example/…'), so the guard must reject both
        "builtin://track/http://evil.example/payload.mp3",
        "builtin://radio/http://evil.example/x",
        "builtin://unknown/https://evil.example/x",
        "not-a-uri",
    ],
)
async def test_msx_audio_rejects_raw_stream_url(
    provider: MSXBridgeProvider, mass_mock: Mock, uri: str
) -> None:
    """A bare stream URL resolves to the builtin provider and must never be enqueued."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        token = provider.get_stream_token("msx_test")
        resp = await client.get(f"/msx/audio/msx_test?uri={quote(uri, safe='')}&token={token}")
        assert resp.status == 400
        mass_mock.player_queues.play_media.assert_not_called()
    finally:
        await client.close()


async def test_msx_audio_rejects_unqueued_library_item(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A library URI that is not in the active queue must not replace the queue."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        mass_mock.player_queues.get_active_queue = Mock(return_value=None)
        token = provider.get_stream_token("msx_test")
        resp = await client.get(f"/msx/audio/msx_test?uri=library://track/1&token={token}")
        assert resp.status == 400
        mass_mock.player_queues.play_media.assert_not_called()
        mass_mock.player_queues.play_index.assert_not_called()
    finally:
        await client.close()


async def test_msx_audio_returns_gateway_timeout_when_media_is_not_prepared(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A queue command that produces no media is reported as a gateway timeout."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        player, _ = _make_audio_player(mass_mock)
        _wire_queue(mass_mock, [_make_queue_item("library://track/1")])
        cast("Any", player).wait_for_media = AsyncMock(return_value=None)
        token = provider.get_stream_token("msx_test")

        response = await client.get(f"/msx/audio/msx_test?uri=library://track/1&token={token}")

        assert response.status == 504
        assert await response.text() == "Playback setup timeout"
    finally:
        await client.close()


async def test_msx_audio_returns_service_error_when_queue_loading_fails(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Provider failures while loading queued audio must not become HTTP 500s."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        _wire_queue(mass_mock, [_make_queue_item("library://track/1")])
        mass_mock.player_queues.play_index = AsyncMock(
            side_effect=MusicAssistantError("source authentication expired")
        )
        token = provider.get_stream_token("msx_test")

        response = await client.get(f"/msx/audio/msx_test?uri=library://track/1&token={token}")

        assert response.status == 503
        assert await response.text() == "Unable to prepare audio"
    finally:
        await client.close()


async def test_msx_audio_does_not_hide_programming_errors(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Unexpected handshake defects must surface as HTTP 500."""
    _make_audio_player(mass_mock)
    token = provider.get_stream_token("msx_test")
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        with patch(
            "music_assistant.providers.msx_bridge.http_server.prepare_msx_audio",
            AsyncMock(side_effect=RuntimeError("programming defect")),
        ):
            response = await client.get(f"/msx/audio/msx_test?uri=library://track/1&token={token}")

        assert response.status == 500
    finally:
        await client.close()


async def test_api_play_rejects_non_string_body_values(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A malformed body must be a 400, not a 500 from the uri guard."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        for body in (
            {"track_uri": 123, "player_id": "msx_test"},
            {"track_uri": True, "player_id": "msx_test"},
            {"track_uri": "library://track/1", "player_id": 42},
        ):
            resp = await client.post("/api/play", json=body)
            assert resp.status == 400
        mass_mock.player_queues.play_media.assert_not_called()
    finally:
        await client.close()


async def test_api_play_rejects_raw_stream_url(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """POST /api/play must apply the same guard as the MSX audio route."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        for track_uri in (
            "http://evil.example/payload.mp3",
            "builtin://track/http://evil.example/payload.mp3",
        ):
            resp = await client.post(
                "/api/play",
                json={"track_uri": track_uri, "player_id": "msx_test"},
            )
            assert resp.status == 400
        mass_mock.player_queues.play_media.assert_not_called()
    finally:
        await client.close()


async def test_msx_audio_rejects_missing_token(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A caller that was never handed a URL cannot start playback."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        resp = await client.get("/msx/audio/msx_test?uri=library://track/1")
        assert resp.status == 403
        resp = await client.get("/msx/audio/msx_test?uri=library://track/1&token=wrong")
        assert resp.status == 403
        mass_mock.player_queues.play_media.assert_not_called()
    finally:
        await client.close()


def _make_queue_item(
    uri: str, name: str = "Radio Example", queue_item_id: str = "queue-item-1"
) -> QueueItem:
    """Build a real queue item whose media item carries the given URI."""
    track = Track(
        item_id=queue_item_id,
        provider="library",
        name=name,
        uri=uri,
        provider_mappings=set(),
        duration=0,
    )
    return QueueItem(
        queue_id="msx_test",
        queue_item_id=queue_item_id,
        name=name,
        duration=0,
        media_item=track,
    )


def _wire_queue(
    mass_mock: Mock, queue_items: list[QueueItem], queue_id: str = "msx_test"
) -> PlayerQueue:
    """Serve the given items as the player's active queue."""

    def _items(qid: str, limit: int = 500, offset: int = 0) -> list[QueueItem]:
        return queue_items[offset : offset + limit] if qid == queue_id else []

    items_mock = Mock(side_effect=_items)
    mass_mock.player_queues.items = items_mock
    active_queue = PlayerQueue(
        queue_id=queue_id,
        active=True,
        display_name="Test queue",
        available=True,
        items=len(queue_items),
        current_index=0,
    )
    mass_mock.player_queues.get_active_queue = Mock(return_value=active_queue)
    mass_mock.player_queues.get = Mock(return_value=active_queue)
    mass_mock.player_queues.get_item = Mock(
        side_effect=lambda qid, item_id: next(
            (item for item in queue_items if qid == queue_id and item.queue_item_id == item_id),
            None,
        )
    )
    mass_mock.player_queues.play_index = AsyncMock()
    return active_queue


async def test_msx_audio_preserves_two_queued_builtin_items(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Selecting consecutive builtin items must not replace their active queue."""
    first_uri = "builtin://radio/http://radio.example/first"
    second_uri = "builtin://radio/http://radio.example/second"
    queue_items = [
        _make_queue_item(first_uri, queue_item_id="radio-1"),
        _make_queue_item(second_uri, queue_item_id="radio-2"),
    ]
    active_queue = _wire_queue(mass_mock, queue_items)

    async def _replace_queue(_player_id: str, selected_uri: str) -> None:
        queue_items[:] = [item for item in queue_items if item.uri == selected_uri]

    mass_mock.player_queues.play_media = AsyncMock(side_effect=_replace_queue)

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        token = provider.get_stream_token("msx_test")
        mass_mock.streams = Mock()
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))
        mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=InvalidDataError("no session"))

        for uri in (first_uri, second_uri):
            with patch(
                "music_assistant.providers.msx_bridge.audio_stream.get_ffmpeg_stream",
                return_value=_async_iter([b"encoded"]),
            ):
                response = await client.get(
                    f"/msx/audio/msx_test?uri={quote(uri, safe='')}&token={token}"
                )
                assert response.status == 200
            assert [item.queue_item_id for item in queue_items] == ["radio-1", "radio-2"]

        mass_mock.player_queues.play_media.assert_not_awaited()
        assert mass_mock.player_queues.play_index.await_args_list == [
            call(active_queue.queue_id, "radio-1"),
            call(active_queue.queue_id, "radio-2"),
        ]
    finally:
        await client.close()


async def test_msx_audio_preserves_queued_library_items(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """MSX next on an already-queued library track must not append another copy."""
    first_uri = "library://track/11"
    second_uri = "library://track/12"
    queue_items = [
        _make_queue_item(first_uri, queue_item_id="lib-11"),
        _make_queue_item(second_uri, queue_item_id="lib-12"),
    ]
    active_queue = _wire_queue(mass_mock, queue_items)

    async def _append_copy(_player_id: str, selected_uri: str) -> None:
        queue_items.append(_make_queue_item(selected_uri, queue_item_id=f"dup-{len(queue_items)}"))

    mass_mock.player_queues.play_media = AsyncMock(side_effect=_append_copy)

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        token = provider.get_stream_token("msx_test")
        mass_mock.streams = Mock()
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))
        mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=InvalidDataError("no session"))

        for uri in (first_uri, second_uri, second_uri):
            with patch(
                "music_assistant.providers.msx_bridge.audio_stream.get_ffmpeg_stream",
                return_value=_async_iter([b"encoded"]),
            ):
                response = await client.get(
                    f"/msx/audio/msx_test?uri={quote(uri, safe='')}&from_playlist=1&token={token}"
                )
                assert response.status == 200

        assert [item.queue_item_id for item in queue_items] == ["lib-11", "lib-12"]
        mass_mock.player_queues.play_media.assert_not_awaited()
        assert mass_mock.player_queues.play_index.await_args_list == [
            call(active_queue.queue_id, "lib-11"),
            call(active_queue.queue_id, "lib-12"),
            call(active_queue.queue_id, "lib-12"),
        ]
    finally:
        await client.close()


async def test_queue_playlist_without_start_rotates_to_current(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Unspecified start must rotate so the current MA item is MSX index 0."""
    items = [
        _make_queue_item("library://track/1", queue_item_id="a"),
        _make_queue_item("library://track/2", queue_item_id="b"),
        _make_queue_item("library://track/3", queue_item_id="c"),
    ]
    queue = _wire_queue(mass_mock, items)
    queue.current_index = 2
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        resp = await client.get("/msx/queue-playlist/msx_test.json")
        assert resp.status == 200
        body = await resp.json()
        assert "library%3A%2F%2Ftrack%2F3" in body["items"][0]["action"]
    finally:
        await client.close()


async def test_queue_playlist_builtin_item_stays_playable(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """
    An item queued from the MA UI must survive the round trip to the TV.

    The queue playlist takes its uris from the MA queue rather than from our own
    menus, so a URL radio station reaches the TV as ``builtin://radio/<url>``.
    """
    radio_uri = "builtin://radio/http://radio.example/stream"
    _wire_queue(mass_mock, [_make_queue_item(radio_uri)])

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        resp = await client.get("/msx/queue-playlist/msx_test.json")
        assert resp.status == 200
        action = (await resp.json())["items"][0]["action"]
        assert action.startswith("audio:")
        audio_url = urlsplit(action.removeprefix("audio:"))

        mass_mock.streams = Mock()
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))
        mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=InvalidDataError("no session"))
        with patch(
            "music_assistant.providers.msx_bridge.audio_stream.get_ffmpeg_stream",
            return_value=_async_iter([b"encoded"]),
        ):
            resp = await client.get(f"{audio_url.path}?{audio_url.query}")
            assert resp.status == 200

        mass_mock.player_queues.play_media.assert_not_awaited()
        mass_mock.player_queues.play_index.assert_awaited_once_with("msx_test", "queue-item-1")
    finally:
        await client.close()


async def test_queue_playlist_duplicate_uri_selects_exact_item(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Duplicate builtin URIs retain distinct queue identities during playback."""
    radio_uri = "builtin://radio/http://radio.example/stream"
    queue_items = [
        _make_queue_item(radio_uri, name="First", queue_item_id="radio-1"),
        _make_queue_item(radio_uri, name="Second", queue_item_id="radio-2"),
    ]
    _wire_queue(mass_mock, queue_items)

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        response = await client.get("/msx/queue-playlist/msx_test.json")
        assert response.status == 200
        actions = [item["action"] for item in (await response.json())["items"]]
        audio_urls = [urlsplit(action.removeprefix("audio:")) for action in actions]
        assert [parse_qs(url.query)["queue_item_id"] for url in audio_urls] == [
            ["radio-1"],
            ["radio-2"],
        ]

        player, media = _make_audio_player(mass_mock)
        player._playing_from_queue = True
        player._attr_current_media = PlayerMedia(
            uri=radio_uri,
            source_id="msx_test",
            queue_item_id="radio-1",
            duration=180,
        )
        cast("Any", player).wait_for_media = AsyncMock(return_value=media)
        mass_mock.player_queues.get_item = Mock(return_value=queue_items[0])
        mass_mock.streams = Mock()
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))
        mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=InvalidDataError("no session"))

        with patch(
            "music_assistant.providers.msx_bridge.audio_stream.get_ffmpeg_stream",
            return_value=_async_iter([b"encoded"]),
        ):
            response = await client.get(f"{audio_urls[1].path}?{audio_urls[1].query}")
            assert response.status == 200

        mass_mock.player_queues.play_media.assert_not_awaited()
        mass_mock.player_queues.play_index.assert_awaited_once_with("msx_test", "radio-2")
    finally:
        await client.close()


async def test_msx_audio_rejects_mismatched_queue_item_id(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A queue item ID only authorizes the URI belonging to that exact item."""
    first_uri = "builtin://radio/http://radio.example/first"
    second_uri = "builtin://radio/http://radio.example/second"
    _wire_queue(
        mass_mock,
        [
            _make_queue_item(first_uri, queue_item_id="radio-1"),
            _make_queue_item(second_uri, queue_item_id="radio-2"),
        ],
    )

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        token = provider.get_stream_token("msx_test")
        uri = quote(first_uri, safe="")
        response = await client.get(
            f"/msx/audio/msx_test?uri={uri}&token={token}&queue_item_id=radio-2"
        )

        assert response.status == 400
        mass_mock.player_queues.play_index.assert_not_awaited()
        mass_mock.player_queues.play_media.assert_not_awaited()
    finally:
        await client.close()


async def test_msx_audio_rejects_builtin_uri_absent_from_the_queue(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """The queue only vouches for the uris it actually holds."""
    _wire_queue(mass_mock, [_make_queue_item("builtin://radio/http://radio.example/stream")])

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        token = provider.get_stream_token("msx_test")
        uri = quote("builtin://track/http://evil.example/payload.mp3", safe="")
        resp = await client.get(f"/msx/audio/msx_test?uri={uri}&token={token}")
        assert resp.status == 400
        mass_mock.player_queues.play_media.assert_not_called()
    finally:
        await client.close()


async def test_msx_audio_queue_fallback_runs_after_the_token_check(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """An unauthorized caller must not learn what a queue holds."""
    radio_uri = "builtin://radio/http://radio.example/stream"
    _wire_queue(mass_mock, [_make_queue_item(radio_uri)])

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        resp = await client.get(f"/msx/audio/msx_test?uri={quote(radio_uri, safe='')}&token=wrong")
        assert resp.status == 403
        mass_mock.player_queues.get_active_queue.assert_not_called()
        mass_mock.player_queues.items.assert_not_called()
        mass_mock.player_queues.play_media.assert_not_called()
    finally:
        await client.close()


async def test_api_play_has_no_queue_fallback(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """The fallback belongs to the queue-driven route only."""
    radio_uri = "builtin://radio/http://radio.example/stream"
    _wire_queue(mass_mock, [_make_queue_item(radio_uri)])

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        resp = await client.post(
            "/api/play", json={"track_uri": radio_uri, "player_id": "msx_test"}
        )
        assert resp.status == 400
        mass_mock.player_queues.play_media.assert_not_called()
    finally:
        await client.close()


async def test_msx_audio_accepts_uri_from_the_group_leaders_queue(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """
    A grouped TV plays from the leader's queue, and that queue has to vouch for it.

    The playlist a member receives is built from the active queue, so checking the
    member's own id would refuse every builtin item the group is playing.
    """
    radio_uri = "builtin://radio/http://radio.example/stream"
    _wire_queue(mass_mock, [_make_queue_item(radio_uri)], queue_id="msx_leader")

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        token = provider.get_stream_token("msx_test")
        mass_mock.streams = Mock()
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))
        mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=InvalidDataError("no session"))
        with patch(
            "music_assistant.providers.msx_bridge.audio_stream.get_ffmpeg_stream",
            return_value=_async_iter([b"encoded"]),
        ):
            resp = await client.get(
                f"/msx/audio/msx_test?uri={quote(radio_uri, safe='')}&token={token}"
            )
            assert resp.status == 200

        mass_mock.player_queues.play_media.assert_not_awaited()
        mass_mock.player_queues.play_index.assert_awaited_once_with("msx_leader", "queue-item-1")
    finally:
        await client.close()


async def test_msx_audio_finds_a_uri_at_the_end_of_a_long_queue(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A builtin item at the end of a long active queue remains playable."""
    radio_uri = "builtin://radio/http://radio.example/stream"
    queue_items = [_make_queue_item(f"library://track/{i}") for i in range(500)]
    queue_items.append(_make_queue_item(radio_uri, queue_item_id="radio-501"))
    _wire_queue(mass_mock, queue_items)

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        token = provider.get_stream_token("msx_test")
        mass_mock.streams = Mock()
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))
        mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=InvalidDataError("no session"))
        with patch(
            "music_assistant.providers.msx_bridge.audio_stream.get_ffmpeg_stream",
            return_value=_async_iter([b"encoded"]),
        ):
            resp = await client.get(
                f"/msx/audio/msx_test?uri={quote(radio_uri, safe='')}&token={token}"
            )
            assert resp.status == 200

        mass_mock.player_queues.play_media.assert_not_awaited()
        mass_mock.player_queues.play_index.assert_awaited_once_with("msx_test", "radio-501")
    finally:
        await client.close()


async def test_msx_audio_accepts_queue_item_identity_without_media_item(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A queue item without media is addressable only by its queue identity."""
    item = QueueItem(
        queue_id="msx_test",
        queue_item_id="radio-bare",
        name="Bare item",
        duration=0,
    )
    _wire_queue(mass_mock, [item])

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        token = provider.get_stream_token("msx_test")
        mass_mock.streams = Mock()
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))
        mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=InvalidDataError("no session"))
        with patch(
            "music_assistant.providers.msx_bridge.audio_stream.get_ffmpeg_stream",
            return_value=_async_iter([b"encoded"]),
        ):
            resp = await client.get(f"/msx/audio/msx_test?uri=radio-bare&token={token}")
            assert resp.status == 200

        mass_mock.player_queues.play_index.assert_awaited_once_with("msx_test", "radio-bare")
    finally:
        await client.close()


async def test_msx_audio_queue_scan_skips_items_without_a_media_item(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A queue entry without media is skipped before a later matching item."""
    radio_uri = "builtin://radio/http://radio.example/stream"
    placeholder = QueueItem(
        queue_id="msx_test",
        queue_item_id="placeholder",
        name="Placeholder",
        duration=None,
    )
    _wire_queue(
        mass_mock,
        [placeholder, _make_queue_item(radio_uri, queue_item_id="radio-2")],
    )

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        token = provider.get_stream_token("msx_test")
        mass_mock.streams = Mock()
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))
        mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=InvalidDataError("no session"))
        with patch(
            "music_assistant.providers.msx_bridge.audio_stream.get_ffmpeg_stream",
            return_value=_async_iter([b"encoded"]),
        ):
            resp = await client.get(
                f"/msx/audio/msx_test?uri={quote(radio_uri, safe='')}&token={token}"
            )
            assert resp.status == 200

        mass_mock.player_queues.play_media.assert_not_awaited()
        mass_mock.player_queues.play_index.assert_awaited_once_with("msx_test", "radio-2")
    finally:
        await client.close()


async def test_msx_audio_player_not_found(http_client: TestClient[Any, Any]) -> None:
    """GET /msx/audio/nonexistent?uri=x should return 404."""
    resp = await http_client.get("/msx/audio/nonexistent?uri=library://track/1")
    assert resp.status == 404


async def test_msx_audio_not_msx_player(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/audio/{id}?uri=x should return 404 for non-MSX player."""
    non_msx_player = Mock()
    mass_mock.players.get.return_value = mass_mock.players.get_player.return_value = non_msx_player

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/audio/other?uri=library://track/1")
        assert resp.status == 404
        body = await resp.text()
        assert "Player not found" in body
    finally:
        await client.close()


async def test_msx_audio_per_track_mode(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/audio should always use force_flow_mode=False (per-track)."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        _wire_queue(mass_mock, [_make_queue_item("library://track/1")])
        token = provider.get_stream_token("msx_test")

        mass_mock.streams = Mock()
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))
        mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=InvalidDataError("no session"))

        chunks = [b"encoded-chunk-1"]
        with patch(
            "music_assistant.providers.msx_bridge.audio_stream.get_ffmpeg_stream",
            return_value=_async_iter(chunks),
        ):
            resp = await client.get(f"/msx/audio/msx_test?uri=library://track/1&token={token}")
            assert resp.status == 200

        mass_mock.streams.get_stream.assert_called_once()
        _args, _pos, kwargs = mass_mock.streams.get_stream.mock_calls[0]
        assert kwargs.get("force_flow_mode") is False
    finally:
        await client.close()


async def test_msx_audio_plays_queued_library_item_without_play_media(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A library URI already in the queue is selected by index, not re-enqueued."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        _wire_queue(mass_mock, [_make_queue_item("library://track/1")])
        token = provider.get_stream_token("msx_test")
        mass_mock.streams = Mock()
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))
        mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=InvalidDataError("no session"))

        with patch(
            "music_assistant.providers.msx_bridge.audio_stream.get_ffmpeg_stream",
            return_value=_async_iter([b"encoded"]),
        ):
            resp = await client.get(f"/msx/audio/msx_test?uri=library://track/1&token={token}")

        assert resp.status == 200
        mass_mock.player_queues.play_media.assert_not_awaited()
        mass_mock.player_queues.play_index.assert_awaited_once()
    finally:
        await client.close()


async def test_msx_audio_proxy_paces_output(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """The local proxy must carry the core streamserver's pacing ceiling."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        _wire_queue(mass_mock, [_make_queue_item("library://track/1")])
        token = provider.get_stream_token("msx_test")
        mass_mock.streams = Mock()
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))
        mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=InvalidDataError("no session"))

        with patch(
            "music_assistant.providers.msx_bridge.audio_stream.get_ffmpeg_stream",
            return_value=_async_iter([b"encoded"]),
        ) as ffmpeg_mock:
            resp = await client.get(f"/msx/audio/msx_test?uri=library://track/1&token={token}")
            assert resp.status == 200

        extra_args = ffmpeg_mock.call_args.kwargs["extra_input_args"]
        assert extra_args == output_pacing_args("gapless_burst")
    finally:
        await client.close()


async def test_msx_audio_from_playlist_skips_ws(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """GET /msx/audio with from_playlist=1 should set _skip_ws_notify on the player."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        player, _media = _make_audio_player(mass_mock)
        _wire_queue(mass_mock, [_make_queue_item("library://track/1")])
        token = provider.get_stream_token("msx_test")

        mass_mock.streams = Mock()
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))
        mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=InvalidDataError("no session"))

        notify_states: list[bool] = []

        async def _capture_play_index(*_a: object, **_k: object) -> None:
            notify_states.append(player._skip_ws_notify)

        mass_mock.player_queues.play_index = _capture_play_index

        chunks = [b"encoded-chunk-1"]
        with patch(
            "music_assistant.providers.msx_bridge.audio_stream.get_ffmpeg_stream",
            return_value=_async_iter(chunks),
        ):
            resp = await client.get(
                f"/msx/audio/msx_test?uri=library://track/1&from_playlist=1&token={token}"
            )
            assert resp.status == 200

        assert notify_states == [True]
        # And reset to False after
        assert player._skip_ws_notify is False

    finally:
        await client.close()


async def test_msx_audio_arms_wait_before_enqueue(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """GET /msx/audio must arm expect_new_media() BEFORE enqueuing new playback."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        player, _media = _make_audio_player(mass_mock)
        _wire_queue(mass_mock, [_make_queue_item("library://track/1")])
        token = provider.get_stream_token("msx_test")

        mass_mock.streams = Mock()
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))
        mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=InvalidDataError("no session"))

        call_order: list[str] = []
        cast("Any", player).expect_new_media = Mock(side_effect=lambda: call_order.append("arm"))

        async def _record_enqueue(*_a: object, **_k: object) -> None:
            call_order.append("enqueue")

        mass_mock.player_queues.play_index = _record_enqueue

        with patch(
            "music_assistant.providers.msx_bridge.audio_stream.get_ffmpeg_stream",
            return_value=_async_iter([b"encoded-chunk-1"]),
        ):
            resp = await client.get(f"/msx/audio/msx_test?uri=library://track/1&token={token}")
            assert resp.status == 200

        assert call_order == ["arm", "enqueue"]
    finally:
        await client.close()


# --- Served audio length (Content-Length) ---


def test_audio_params_include_content_length_by_default() -> None:
    """The compatibility header remains enabled by default."""
    _pcm, _out, headers = build_audio_params("mp3", 180)

    assert headers["Content-Length"] == str(180 * 40_000)


def test_audio_params_can_omit_content_length() -> None:
    """TVs that reject estimated lengths can use chunked local delivery."""
    _pcm, _out, headers = build_audio_params("mp3", 180, include_content_length=False)

    assert "Content-Length" not in headers


def test_served_duration_uses_media_duration(provider: MSXBridgeProvider) -> None:
    """Without a seek the served audio is the whole media item."""
    server = MSXHTTPServer(provider, 0)
    media = PlayerMedia(uri="library://track/1", duration=180)

    assert server._resolve_served_duration(media) == 180


def test_served_duration_prefers_stream_duration(provider: MSXBridgeProvider) -> None:
    """Starting mid-track serves less audio than the media item is long."""
    server = MSXHTTPServer(provider, 0)
    media = PlayerMedia(uri="library://track/1", duration=180, stream_duration=60)

    assert server._resolve_served_duration(media) == 60


def test_served_duration_falls_back_to_queue_item(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """A media item of unknown length is resolved through its queue item."""
    server = MSXHTTPServer(provider, 0)
    queue_item = QueueItem(
        queue_id="q1",
        queue_item_id="item1",
        name="Unknown duration track",
        duration=240,
    )
    mass_mock.player_queues.get_item.return_value = queue_item
    media = PlayerMedia(uri="library://track/1", source_id="q1", queue_item_id="item1")

    assert server._resolve_served_duration(media) == 240


# --- MSX playlist endpoints ---


async def test_msx_album_playlist_endpoint(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/playlist/album/{id}.json should return playlist JSON."""
    track = _make_track()
    mass_mock.music.albums.tracks.return_value = [track]

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/playlist/album/42.json?start=0")
        assert resp.status == 200
        data = await resp.json()
        assert data["type"] == "list"
        assert data["action"] == "player:play"
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["action"].startswith("audio:")
        assert "/msx/audio/" in item["action"]
        assert "from_playlist=1" in item["action"]
    finally:
        await client.close()


async def test_msx_playlist_playlist_endpoint(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/playlist/playlist/{id}.json should return playlist JSON."""
    track = _make_track()

    async def _mock_playlist_tracks(*_args: object, **_kwargs: object) -> AsyncGenerator[Any]:
        yield track

    mass_mock.music.playlists.tracks = Mock(side_effect=lambda *_a, **_k: _mock_playlist_tracks())

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/playlist/playlist/5.json?start=1")
        assert resp.status == 200
        data = await resp.json()
        assert data["type"] == "list"
        assert data["action"] == "player:play"
        assert len(data["items"]) == 1
    finally:
        await client.close()


async def test_msx_tracks_playlist_endpoint(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/playlist/tracks.json should return playlist JSON."""
    track = _make_track()
    mass_mock.music.tracks.library_items.return_value = [track]

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/playlist/tracks.json?start=0")
        assert resp.status == 200
        data = await resp.json()
        assert data["type"] == "list"
        assert len(data["items"]) == 1
    finally:
        await client.close()


# --- Duration in track formatting ---


def test_format_msx_track_includes_duration(provider: MSXBridgeProvider) -> None:
    """map_track_to_msx should include artist and duration in titleFooter."""
    track = _make_track()  # duration=180
    assert track.uri is not None
    item = map_track_to_msx(track, "http://localhost", "msx_test", provider, context_uri=track.uri)
    assert item.title_header == "{txt:msx-white:Test Track}"
    assert item.title_footer == "Test Artist · 3:00"
    assert item.background == item.image


def test_format_msx_track_no_duration(provider: MSXBridgeProvider) -> None:
    """map_track_to_msx should handle zero/missing duration gracefully."""
    track = _make_track()
    track.duration = 0
    assert track.uri is not None
    item = map_track_to_msx(track, "http://localhost", "msx_test", provider, context_uri=track.uri)
    assert item.title_header == "{txt:msx-white:Test Track}"
    assert item.title_footer == "Test Artist"


def test_format_msx_track_duration_only(provider: MSXBridgeProvider) -> None:
    """map_track_to_msx should show only duration when no artist."""
    track = _make_track(artist_name="")
    assert track.uri is not None
    item = map_track_to_msx(track, "http://localhost", "msx_test", provider, context_uri=track.uri)
    assert item.title_header == "{txt:msx-white:Test Track}"
    assert item.title_footer == "3:00"


# --- Async iteration helpers for stream mocking ---


async def _async_iter(items: list[Any]) -> AsyncGenerator[Any]:
    """Async generator helper for mocking iter_chunked."""
    for item in items:
        yield item


# --- MSX queue-playlist endpoint ---


async def test_msx_queue_playlist_endpoint(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/queue-playlist/{player_id}.json should return MSX playlist from MA queue."""
    qi1 = _make_queue_item("library://track/1", name="Track 1", queue_item_id="track-1")
    qi1.duration = 180
    assert qi1.media_item is not None
    qi1.media_item.duration = 180
    qi2 = _make_queue_item("library://track/2", name="Track 2", queue_item_id="track-2")
    qi2.duration = 200
    assert qi2.media_item is not None
    qi2.media_item.duration = 200

    _wire_queue(mass_mock, [qi1, qi2])

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/queue-playlist/msx_test.json?start=0")
        assert resp.status == 200
        data = await resp.json()
        assert data["type"] == "list"
        assert data["action"] == "player:play"
        assert len(data["items"]) == 2
        assert data["items"][0]["title"] == "Track 1"
        assert data["items"][1]["title"] == "Track 2"
        assert "from_playlist=1" in data["items"][0]["action"]
    finally:
        await client.close()


@pytest.mark.parametrize(
    "path",
    [
        "/msx/playlist/album/42.json",
        "/msx/playlist/playlist/5.json",
        "/msx/playlist/tracks.json",
        "/msx/playlist/recently-played.json",
        "/msx/playlist/search.json?q=test",
        "/msx/queue-playlist/msx_test.json",
    ],
)
async def test_token_bearing_playlists_reject_cross_site_requests(
    provider: MSXBridgeProvider, path: str
) -> None:
    """Cross-site callers must not receive token-bearing playlist actions."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        response = await client.get(path, headers={"Sec-Fetch-Site": "cross-site"})

        assert response.status == 403
        assert provider.get_stream_token("msx_test") not in await response.text()
    finally:
        await client.close()


async def test_queue_playlist_does_not_register_request_derived_player(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """An explicit-player queue route must not create an IP-derived duplicate."""
    mass_mock.player_queues.items = Mock(return_value=[])
    provider.get_or_register_player = AsyncMock()  # type: ignore[method-assign]
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        response = await client.get("/msx/queue-playlist/msx_route.json?device_id=Living%20Room")
        assert response.status == 200
        provider.get_or_register_player.assert_not_awaited()
    finally:
        await client.close()


@pytest.mark.parametrize("body", [[], None, "track", 1])
async def test_api_play_rejects_non_object_json(provider: MSXBridgeProvider, body: object) -> None:
    """Valid JSON primitives are invalid request bodies, not server errors."""
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        response = await client.post("/api/play", json=body)
        assert response.status == 400
    finally:
        await client.close()


async def test_msx_queue_playlist_with_start_index(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """GET /msx/queue-playlist with start=1 should use player:play action."""
    qi = _make_queue_item("library://track/1", name="Track 1")
    qi.duration = 180
    assert qi.media_item is not None
    qi.media_item.duration = 180

    mass_mock.player_queues.items = Mock(return_value=[qi])

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/queue-playlist/msx_test.json?start=1")
        assert resp.status == 200
        data = await resp.json()
        assert data["action"] == "player:play"
    finally:
        await client.close()


async def test_msx_queue_playlist_preserves_start_above_ten_thousand(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Queue playlist rotation must not clamp a valid long-queue index."""
    items = [
        _make_queue_item(f"library://track/{index}", queue_item_id=str(index))
        for index in range(12002)
    ]
    _wire_queue(mass_mock, items)
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        response = await client.get("/msx/queue-playlist/msx_test.json?start=12001")
        assert response.status == 200
        data = await response.json()
        assert "queue_item_id=12001" in data["items"][0]["action"]
    finally:
        await client.close()


async def test_msx_queue_playlist_reads_full_queue(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """Queue playlists must ask for every item, not the default 500-item page."""
    mass_mock.player_queues.get = Mock(
        return_value=PlayerQueue(
            queue_id="msx_test",
            active=True,
            display_name="Test queue",
            available=True,
            items=800,
            current_index=0,
        )
    )
    mass_mock.player_queues.items = Mock(return_value=[])
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/queue-playlist/msx_test.json?start=0")
        assert resp.status == 200
        mass_mock.player_queues.items.assert_called_with("msx_test", limit=800)
    finally:
        await client.close()


async def test_msx_queue_playlist_empty_queue(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/queue-playlist with empty queue should return empty playlist."""
    mass_mock.player_queues.items = Mock(return_value=[])

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/queue-playlist/msx_test.json?start=0")
        assert resp.status == 200
        data = await resp.json()
        assert data["type"] == "list"
        assert data["items"] == []
    finally:
        await client.close()


# --- WebSocket inbound message handling ---


async def test_ws_position_message(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """WS position message should update player's elapsed time."""
    player = MSXPlayer(provider, "msx_test", name="Test TV", output_format="mp3")
    player.update_state = Mock()  # type: ignore[misc,method-assign]
    player._attr_playback_state = PlaybackState.PLAYING
    mass_mock.players.get.return_value = mass_mock.players.get_player.return_value = player
    provider.http_server = MSXHTTPServer(provider, 0)

    server_obj = provider.http_server
    server_obj._handle_ws_message("msx_test", '{"type": "position", "position": 42.5}')

    assert player._attr_elapsed_time == 42.5
    assert player._last_ws_position is not None


async def test_ws_position_message_unknown_player(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """WS position message for unknown player should not crash."""
    mass_mock.players.get.return_value = mass_mock.players.get_player.return_value = None
    provider.http_server = MSXHTTPServer(provider, 0)

    # Should not raise
    provider.http_server._handle_ws_message("msx_unknown", '{"type": "position", "position": 10}')


async def test_ws_invalid_json(provider: MSXBridgeProvider) -> None:
    """WS invalid JSON should not crash."""
    provider.http_server = MSXHTTPServer(provider, 0)
    # Should not raise
    provider.http_server._handle_ws_message("msx_test", "not json")


@pytest.mark.parametrize("message", ["[]", "null", '"text"', "1"])
def test_ws_non_object_json_is_ignored(provider: MSXBridgeProvider, message: str) -> None:
    """Valid non-object JSON must not terminate WebSocket processing."""
    provider.http_server = MSXHTTPServer(provider, 0)
    provider.http_server._handle_ws_message("msx_test", message)


@pytest.mark.parametrize("position", ["NaN", "Infinity", "-1", "true"])
def test_ws_invalid_position_is_ignored(
    provider: MSXBridgeProvider, mass_mock: Mock, position: str
) -> None:
    """Non-finite, negative, and boolean positions must not mutate player state."""
    player = MSXPlayer(provider, "msx_test", name="Test TV", output_format="mp3")
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 10.0
    mass_mock.players.get_player.return_value = player
    server = MSXHTTPServer(provider, 0)

    server._handle_ws_message("msx_test", f'{{"type":"position","position":{position}}}')

    assert player._attr_elapsed_time == 10.0


async def test_ws_pause_message(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """WS pause message should update position and call cmd_pause."""
    player = MSXPlayer(provider, "msx_test", name="Test TV", output_format="mp3")
    player.update_state = Mock()  # type: ignore[misc,method-assign]
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 10.0
    mass_mock.players.get.return_value = mass_mock.players.get_player.return_value = player
    provider.http_server = MSXHTTPServer(provider, 0)

    provider.http_server._handle_ws_message("msx_test", '{"type": "pause", "position": 30.5}')

    assert player._attr_elapsed_time == 30.5
    # Flag is now managed inside _cmd_pause_no_echo; verify the task was scheduled
    mass_mock.create_task.assert_called_once()


async def test_ws_resume_message(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """WS resume message should call cmd_play."""
    player = MSXPlayer(provider, "msx_test", name="Test TV", output_format="mp3")
    player.update_state = Mock()  # type: ignore[misc,method-assign]
    player._attr_playback_state = PlaybackState.PAUSED
    mass_mock.players.get.return_value = mass_mock.players.get_player.return_value = player
    provider.http_server = MSXHTTPServer(provider, 0)

    provider.http_server._handle_ws_message("msx_test", '{"type": "resume"}')

    # Flag is now managed inside _cmd_play_no_echo; verify the task was scheduled
    mass_mock.create_task.assert_called_once()


async def test_ws_unknown_message_type(provider: MSXBridgeProvider) -> None:
    """WS unknown message type should not crash."""
    provider.http_server = MSXHTTPServer(provider, 0)
    # Should not raise
    provider.http_server._handle_ws_message("msx_test", '{"type": "unknown_cmd"}')


# --- Removed kiosk/sendspin routes ---


async def test_removed_kiosk_and_sendspin_routes_404(
    http_client: TestClient[Any, Any],
) -> None:
    """Removed kiosk and sendspin routes should return 404."""
    for path in [
        "/msx/kiosk-plugin.html",
        "/msx/kiosk.html",
        "/msx/kiosk-content.json",
        "/msx/kiosk-page.json",
        "/msx/kiosk-album.json",
        "/msx/sendspin-plugin.html",
        "/msx/sendspin-standalone.html",
        "/msx/sendspin-bundle.js",
        "/web",
        "/web/",
        "/web/index.html",
        "/web/web.js",
        "/web/sendspin-js/index.js",
        "/api/lyrics/msx_test",
        "/api/queue/msx_test",
    ]:
        resp = await http_client.get(path)
        assert resp.status == 404, f"Expected 404 for {path}, got {resp.status}"


# --- Server shutdown ---


async def test_server_stop_survives_ws_self_deregistration(
    provider: MSXBridgeProvider,
) -> None:
    """Closing a WS during stop() triggers its cleanup discard; stop() must survive it."""
    server = MSXHTTPServer(provider, 0)

    class _SelfRemovingWS:
        closed = False

        async def close(self) -> None:
            server._ws_clients["msx_test"].discard(self)

    fake_clients: set[Any] = {_SelfRemovingWS(), _SelfRemovingWS()}
    server._ws_clients["msx_test"] = fake_clients

    await server.stop()

    assert server._ws_clients == {}


async def test_server_stop_cancels_party_cover_tasks(provider: MSXBridgeProvider) -> None:
    """HTTP shutdown must not leave Party cover work using the old provider."""
    server = MSXHTTPServer(provider, 0)
    started = asyncio.Event()

    async def _render_forever(*_args: object) -> bytes:
        started.set()
        await asyncio.Event().wait()
        return b"unreachable"

    with patch.object(server.party, "_fetch_and_render_cover", _render_forever):
        task = server.party.qr_cover_task(("cover", "v1"), "cover", "join")
        await started.wait()
        await server.stop()

    assert task.cancelled()
    assert server.party.qr_cover_inflight == {}


async def test_server_stop_clears_client_prefixes(provider: MSXBridgeProvider) -> None:
    """Shutdown must discard addresses learned from old WebSocket clients."""
    server = MSXHTTPServer(provider, 0)
    server._client_prefixes["msx_test"] = "http://old-host:8099"

    await server.stop()

    assert server._client_prefixes == {}


# --- Redirect stream mode (MA streamserver) ---


async def test_msx_audio_redirect_mode(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """In redirect mode /msx/audio must 302-redirect to the MA streamserver stream."""
    provider.group_stream_mode = "redirect"
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        _wire_queue(mass_mock, [_make_queue_item("library://track/1")])
        token = provider.get_stream_token("msx_test")

        stream_url = "http://ma:8097/single/s1/q1/i1/msx_test.mp3"
        mass_mock.streams = Mock()
        mass_mock.streams.resolve_stream_url = AsyncMock(return_value=stream_url)

        resp = await client.get(
            f"/msx/audio/msx_test?uri=library://track/1&token={token}",
            allow_redirects=False,
        )
        assert resp.status == 302
        assert resp.headers["Location"].endswith(":8097/single/s1/q1/i1/msx_test.mp3")
    finally:
        await client.close()


async def test_msx_audio_redirect_rewrites_host_for_client(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """
    The redirect must target the host the TV already uses to reach the provider.

    Behind Docker/NAT the MA streamserver advertises its container IP, which
    the TV cannot reach; only the host of the URL is rewritten — port, path
    and query of the streamserver URL must survive.
    """
    provider.group_stream_mode = "redirect"
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        _wire_queue(mass_mock, [_make_queue_item("library://track/1")])
        token = provider.get_stream_token("msx_test")

        stream_url = "http://172.18.0.2:8097/single/s1/q1/i1/msx_test.mp3?flow=1"
        mass_mock.streams = Mock()
        mass_mock.streams.resolve_stream_url = AsyncMock(return_value=stream_url)

        resp = await client.get(
            f"/msx/audio/msx_test?uri=library://track/1&token={token}",
            allow_redirects=False,
        )
        assert resp.status == 302
        location = urlsplit(resp.headers["Location"])
        assert location.hostname == "127.0.0.1"
        assert location.port == 8097
        assert location.path == "/single/s1/q1/i1/msx_test.mp3"
        assert location.query == "flow=1"
    finally:
        await client.close()


async def test_collect_prebuffer_waits_for_threshold() -> None:
    """Headers must not go out until PRE_BUFFER_BYTES have arrived (or EOF)."""
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    await queue.put(b"a" * 40_000)
    await queue.put(b"b" * 40_000)
    await queue.put(b"tail")
    buf, ended = await _collect_prebuffer(queue)
    assert ended is False
    assert sum(len(chunk) for chunk in buf) >= PRE_BUFFER_BYTES
    assert queue.get_nowait() == b"tail"


async def test_msx_audio_redirect_mode_falls_back_to_proxy(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """When URL resolution fails, redirect mode must fall back to the local proxy."""
    provider.group_stream_mode = "redirect"
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)
        _wire_queue(mass_mock, [_make_queue_item("library://track/1")])
        token = provider.get_stream_token("msx_test")

        mass_mock.streams = Mock()
        mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=InvalidDataError("boom"))
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))
        filter_params = ["volume=0.5"]
        mass_mock.streams.audio.get_player_output_plan.return_value = Mock(
            filter_params=filter_params
        )

        with patch(
            "music_assistant.providers.msx_bridge.audio_stream.get_ffmpeg_stream",
            return_value=_async_iter([b"encoded-chunk-1"]),
        ) as ffmpeg_stream:
            resp = await client.get(
                f"/msx/audio/msx_test?uri=library://track/1&token={token}",
                allow_redirects=False,
            )
            assert resp.status == 200
            body = await resp.read()
            assert b"encoded-chunk-1" in body
            assert ffmpeg_stream.call_args.kwargs["filter_params"] == filter_params
    finally:
        await client.close()


class _AsyncCtx:
    """Async context manager helper for mocking session.get()."""

    def __init__(self, obj: object) -> None:
        self._obj = obj

    async def __aenter__(self) -> object:
        return self._obj

    async def __aexit__(self, *args: object) -> None:
        pass
