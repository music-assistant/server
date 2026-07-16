"""Tests for MSXHTTPServer routes."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from urllib.parse import urlsplit

import pytest
from aiohttp.test_utils import TestClient as AiohttpTestClient
from aiohttp.test_utils import TestServer
from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.msx_bridge.http_server import STATIC_DIR, MSXHTTPServer
from music_assistant.providers.msx_bridge.mappers import map_track_to_msx
from music_assistant.providers.msx_bridge.player import MSXPlayer
from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider

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


async def test_root_html_sendspin_urls_escaped(http_client: TestClient[Any, Any]) -> None:
    """Generated sendspin hrefs must be HTML-escaped as a whole, including & separators."""
    resp = await http_client.get("/")
    assert resp.status == 200
    body = await resp.text()
    assert "&amp;sendspin_url=http%3A%2F%2F" in body
    assert "&sendspin_url=http%3A%2F%2F" not in body


async def test_start_json(http_client: TestClient[Any, Any]) -> None:
    """GET /msx/start.json should return launcher menu config."""
    resp = await http_client.get("/msx/start.json")
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "Music Assistant"
    assert data["parameter"].startswith("content:")
    assert "/msx/launcher.json" in data["parameter"]
    assert "scripts" not in data


async def test_plugin_html(http_client: TestClient[Any, Any]) -> None:
    """GET /msx/plugin.html should return HTML with interaction plugin."""
    resp = await http_client.get("/msx/plugin.html")
    assert resp.status == 200
    assert "text/html" in resp.headers["Content-Type"]
    body = await resp.text()
    assert "tvx.InteractionPlugin" in body
    assert "handleRequest" in body
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
    mass_mock.players.get.return_value = mass_mock.players.get_player.return_value = mock_player

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/stream/msx_test")
        assert resp.status == 404
        body = await resp.text()
        assert "No active stream" in body
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
    mock_media = Mock()
    mock_media.duration = 180
    mock_media.source_id = None
    mock_media.queue_item_id = None
    mock_player.current_media = mock_media
    mock_player.output_format = "mp3"
    mass_mock.players.get.return_value = mass_mock.players.get_player.return_value = mock_player

    # Mock get_stream to return an async generator
    mass_mock.streams = Mock()
    mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm-data"]))

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        chunks = [b"encoded-chunk-1", b"encoded-chunk-2"]
        with patch(
            "music_assistant.providers.msx_bridge.http_server.get_ffmpeg_stream",
            return_value=_async_iter(chunks),
        ):
            resp = await client.get("/stream/msx_test")
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
    album = Mock()
    album.item_id = 1
    album.name = "Test Album"
    album.artist_str = "Test Artist"
    album.uri = "library://album/1"
    album.image = None
    mock_result = Mock()
    mock_result.__iter__ = Mock(return_value=iter([album]))
    mock_result.total = 1
    mass_mock.music.albums.library_items.return_value = mock_result

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


async def test_control_endpoints_reject_unexpected_methods(
    http_client: TestClient[Any, Any],
) -> None:
    """Control endpoints accept only GET and POST — no wildcard methods."""
    resp = await http_client.delete("/api/pause/msx_x")
    assert resp.status == 405


# --- MSX content page actions ---


def _make_album_mock(item_id: int = 1, name: str = "Test Album") -> Mock:
    """Create a mock album object."""
    album = Mock()
    album.item_id = item_id
    album.name = name
    album.artist_str = "Test Artist"
    album.uri = f"library://album/{item_id}"
    album.image = None
    return album


def _make_track_mock(item_id: int = 1, name: str = "Test Track") -> Mock:
    """Create a mock track object."""
    track = Mock()
    track.item_id = item_id
    track.name = name
    track.artist_str = "Test Artist"
    track.uri = f"library://track/{item_id}"
    track.image = None
    track.album = Mock(name="Test Album")
    track.duration = 180
    return track


def _make_artist_mock(item_id: int = 1, name: str = "Test Artist") -> Mock:
    """Create a mock artist object."""
    artist = Mock()
    artist.item_id = item_id
    artist.name = name
    artist.uri = f"library://artist/{item_id}"
    artist.image = None
    return artist


def _make_playlist_mock(item_id: int = 1, name: str = "Test Playlist") -> Mock:
    """Create a mock playlist object."""
    playlist = Mock()
    playlist.item_id = item_id
    playlist.name = name
    playlist.uri = f"library://playlist/{item_id}"
    playlist.image = None
    playlist.owner = "test_user"
    playlist.provider = "library"
    return playlist


def _make_audio_player(mass_mock: Mock) -> tuple[MagicMock, PlayerMedia]:
    """Wire a MagicMock MSXPlayer with queue-backed media into mass_mock for audio tests."""
    player = MagicMock(spec=MSXPlayer)
    player.player_id = "msx_test"
    player.output_format = "mp3"
    player._skip_ws_notify = False
    media = PlayerMedia(
        uri="library://track/1",
        title=None,
        artist=None,
        album=None,
        image_url=None,
        duration=180,
    )
    player.current_media = media
    player.wait_for_media = AsyncMock(return_value=media)
    mass_mock.players.get.return_value = mass_mock.players.get_player.return_value = player
    return player, media


async def test_msx_albums_have_action(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/albums.json items should have content: action for drill-down."""
    album = _make_album_mock()
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
    artist = _make_artist_mock()
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
    playlist = _make_playlist_mock()
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
    """GET /msx/tracks.json items should have playlist: action for playback."""
    track = _make_track_mock()
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
        assert item["action"].startswith("playlist:")
        assert "/msx/playlist/tracks.json" in item["action"]
        assert item["titleHeader"] == "{txt:msx-white:Test Track}"
        assert "playerLabel" in item
        assert item["playerLabel"] == "Test Track"
    finally:
        await client.close()


# --- MSX detail pages ---


async def test_msx_album_tracks(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/albums/{id}/tracks.json should return tracks with audio actions."""
    track = _make_track_mock()
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
        assert item["action"].startswith("playlist:")
        assert "/msx/playlist/album/" in item["action"]
    finally:
        await client.close()


async def test_msx_artist_albums(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/artists/{id}/albums.json should return albums with content actions."""
    album = _make_album_mock()
    mass_mock.music.artists.albums.return_value = [album]

    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/artists/1/albums.json")
        assert resp.status == 200
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
    track = _make_track_mock()

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
        assert item["action"].startswith("playlist:")
        assert "/msx/playlist/playlist/" in item["action"]
    finally:
        await client.close()


# --- MSX audio endpoint ---


async def test_msx_audio_missing_uri(http_client: TestClient[Any, Any]) -> None:
    """GET /msx/audio/msx_default without ?uri= should return 400."""
    resp = await http_client.get("/msx/audio/msx_default")
    assert resp.status == 400
    body = await resp.text()
    assert "uri" in body.lower()  # "Missing uri" or "Invalid uri parameter"


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

        mass_mock.streams = Mock()
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))

        chunks = [b"encoded-chunk-1"]
        with patch(
            "music_assistant.providers.msx_bridge.http_server.get_ffmpeg_stream",
            return_value=_async_iter(chunks),
        ):
            resp = await client.get("/msx/audio/msx_test?uri=library://track/1")
            assert resp.status == 200

        mass_mock.streams.get_stream.assert_called_once()
        _args, _pos, kwargs = mass_mock.streams.get_stream.mock_calls[0]
        assert kwargs.get("force_flow_mode") is False

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

        mass_mock.streams = Mock()
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))

        # Track that _skip_ws_notify was set to True during play_media
        notify_states: list[bool] = []

        async def _capture_play_media(*_a: object, **_k: object) -> None:
            notify_states.append(player._skip_ws_notify)

        mass_mock.player_queues.play_media = _capture_play_media

        chunks = [b"encoded-chunk-1"]
        with patch(
            "music_assistant.providers.msx_bridge.http_server.get_ffmpeg_stream",
            return_value=_async_iter(chunks),
        ):
            resp = await client.get("/msx/audio/msx_test?uri=library://track/1&from_playlist=1")
            assert resp.status == 200

        # _skip_ws_notify should have been True during play_media call
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

        mass_mock.streams = Mock()
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))

        call_order: list[str] = []
        player.expect_new_media = Mock(side_effect=lambda: call_order.append("arm"))

        async def _record_enqueue(*_a: object, **_k: object) -> None:
            call_order.append("enqueue")

        mass_mock.player_queues.play_media = _record_enqueue

        with patch(
            "music_assistant.providers.msx_bridge.http_server.get_ffmpeg_stream",
            return_value=_async_iter([b"encoded-chunk-1"]),
        ):
            resp = await client.get("/msx/audio/msx_test?uri=library://track/1")
            assert resp.status == 200

        assert call_order == ["arm", "enqueue"]
    finally:
        await client.close()


# --- MSX playlist endpoints ---


async def test_msx_album_playlist_endpoint(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/playlist/album/{id}.json should return playlist JSON."""
    track = _make_track_mock()
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
    track = _make_track_mock()

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
    track = _make_track_mock()
    mock_result = Mock()
    mock_result.__iter__ = Mock(return_value=iter([track]))
    mass_mock.music.tracks.library_items.return_value = mock_result

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
    track = _make_track_mock()  # duration=180
    item = map_track_to_msx(track, "http://localhost", "msx_test", provider)
    assert item.title_header == "{txt:msx-white:Test Track}"
    assert item.title_footer == "Test Artist · 3:00"
    assert item.background == item.image


def test_format_msx_track_no_duration(provider: MSXBridgeProvider) -> None:
    """map_track_to_msx should handle zero/missing duration gracefully."""
    track = _make_track_mock()
    track.duration = 0
    item = map_track_to_msx(track, "http://localhost", "msx_test", provider)
    assert item.title_header == "{txt:msx-white:Test Track}"
    assert item.title_footer == "Test Artist"


def test_format_msx_track_duration_only(provider: MSXBridgeProvider) -> None:
    """map_track_to_msx should show only duration when no artist."""
    track = _make_track_mock()
    track.artist_str = ""
    item = map_track_to_msx(track, "http://localhost", "msx_test", provider)
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
    qi1 = Mock()
    qi1.name = "Track 1"
    qi1.media_item = Mock()
    qi1.media_item.name = "Track 1"
    qi1.media_item.uri = "library://track/1"
    qi1.media_item.duration = 180
    qi1.media_item.artist_str = "Artist 1"
    qi1.duration = 180
    qi1.image = None

    qi2 = Mock()
    qi2.name = "Track 2"
    qi2.media_item = Mock()
    qi2.media_item.name = "Track 2"
    qi2.media_item.uri = "library://track/2"
    qi2.media_item.duration = 200
    qi2.media_item.artist_str = "Artist 2"
    qi2.duration = 200
    qi2.image = None

    mass_mock.player_queues.items = Mock(return_value=[qi1, qi2])

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


async def test_msx_queue_playlist_with_start_index(
    provider: MSXBridgeProvider, mass_mock: Mock
) -> None:
    """GET /msx/queue-playlist with start=1 should use player:play action."""
    qi = Mock()
    qi.name = "Track 1"
    qi.media_item = Mock()
    qi.media_item.name = "Track 1"
    qi.media_item.uri = "library://track/1"
    qi.media_item.duration = 180
    qi.media_item.artist_str = "Artist 1"
    qi.duration = 180
    qi.image = None

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


# --- Redirect stream mode (MA streamserver) ---


async def test_msx_audio_redirect_mode(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """In redirect mode /msx/audio must 302-redirect to the MA streamserver stream."""
    provider.group_stream_mode = "redirect"
    server = MSXHTTPServer(provider, 0)
    client = AiohttpTestClient(TestServer(server.app))
    await client.start_server()
    try:
        _make_audio_player(mass_mock)

        stream_url = "http://ma:8097/single/s1/q1/i1/msx_test.mp3"
        mass_mock.streams = Mock()
        mass_mock.streams.resolve_stream_url = AsyncMock(return_value=stream_url)

        resp = await client.get("/msx/audio/msx_test?uri=library://track/1", allow_redirects=False)
        assert resp.status == 302
        # the URL host is rewritten to the client-reachable one (see the
        # dedicated rewrite test); the streamserver path must be intact
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

        stream_url = "http://172.18.0.2:8097/single/s1/q1/i1/msx_test.mp3?flow=1"
        mass_mock.streams = Mock()
        mass_mock.streams.resolve_stream_url = AsyncMock(return_value=stream_url)

        resp = await client.get("/msx/audio/msx_test?uri=library://track/1", allow_redirects=False)
        assert resp.status == 302
        location = urlsplit(resp.headers["Location"])
        assert location.hostname == "127.0.0.1"  # host the TestClient connects to
        assert location.port == 8097
        assert location.path == "/single/s1/q1/i1/msx_test.mp3"
        assert location.query == "flow=1"
    finally:
        await client.close()


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

        mass_mock.streams = Mock()
        mass_mock.streams.resolve_stream_url = AsyncMock(side_effect=RuntimeError("boom"))
        mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm"]))

        with patch(
            "music_assistant.providers.msx_bridge.http_server.get_ffmpeg_stream",
            return_value=_async_iter([b"encoded-chunk-1"]),
        ):
            resp = await client.get(
                "/msx/audio/msx_test?uri=library://track/1", allow_redirects=False
            )
            assert resp.status == 200
            body = await resp.read()
            assert b"encoded-chunk-1" in body
    finally:
        await client.close()


# --- Vendored Sendspin JS client ---


async def test_vendored_sendspin_js_served(http_client: TestClient[Any, Any]) -> None:
    """The Sendspin JS client must be served locally — TVs on LAN-only setups have no CDN."""
    resp = await http_client.get("/web/sendspin-js/index.js")
    assert resp.status == 200
    body = await resp.text()
    assert "SendspinPlayer" in body


def test_web_player_has_no_cdn_dependency() -> None:
    """web.js must import the Sendspin SDK from the vendored copy, not from a CDN."""
    web_js = (STATIC_DIR / "web" / "web.js").read_text(encoding="utf-8")
    assert "unpkg.com" not in web_js
    assert "jsdelivr" not in web_js
    assert "sendspin-js/index.js" in web_js


async def test_root_has_kiosk_url_builder(http_client: TestClient[Any, Any]) -> None:
    """The status page offers a kiosk URL builder with the four display toggles."""
    resp = await http_client.get("/")
    assert resp.status == 200
    body = await resp.text()
    assert 'id="kiosk-builder"' in body
    for name in ("controls", "party", "viz", "lyrics"):
        assert f'data-kiosk-param="{name}"' in body
    assert 'id="kiosk-builder-link"' in body
    assert 'id="kiosk-builder-url"' in body


def test_web_player_reads_kiosk_display_params() -> None:
    """web.js must read the four kiosk display params (URL contract)."""
    web_js = (STATIC_DIR / "web" / "web.js").read_text(encoding="utf-8")
    for name in ("controls", "party", "viz", "lyrics"):
        assert f"kioskFlag('{name}')" in web_js, f"missing kiosk param: {name}"


def test_web_player_js_parses() -> None:
    """web.js must be syntactically valid (guards against edits breaking the kiosk)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    result = subprocess.run(  # noqa: S603
        [node, "--check", str(STATIC_DIR / "web" / "web.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_vendored_sendspin_js_imports_are_browser_loadable() -> None:
    """
    Every import specifier in the vendored SDK must be loadable by a browser.

    Relative specifiers must resolve to a vendored file (the upstream dist is
    TypeScript output with extensionless specifiers that only work through CDN
    rewriting). Bare specifiers cannot resolve in browsers at all; the two
    opus-encdec ones are a known opus-decode fallback that stays unreachable
    because the web player stops advertising opus when WebCodecs is missing.
    """
    vendor_dir = STATIC_DIR / "web" / "sendspin-js"
    js_files = list(vendor_dir.rglob("*.js"))
    assert js_files, "vendored sendspin-js dist is missing"
    known_bare_specifiers = {
        "opus-encdec/dist/libopus-decoder.js",
        "opus-encdec/src/oggOpusDecoder.js",
    }
    # static imports/re-exports, side-effect imports, and dynamic import()
    pattern = re.compile(
        r"""(?:import\s*\(\s*|import[^'"()]*?from\s+|import\s+|export[^'"()]*?from\s+)"""
        r"""['"]([^'"]+)['"]"""
    )
    bad: list[str] = []
    for js_file in js_files:
        for specifier in pattern.findall(js_file.read_text(encoding="utf-8")):
            if specifier.startswith("."):
                target = (js_file.parent / specifier).resolve()
                if not (specifier.endswith(".js") and target.is_file()):
                    bad.append(f"{js_file.name}: {specifier}")
            elif specifier not in known_bare_specifiers:
                bad.append(f"{js_file.name}: bare specifier {specifier}")
    assert not bad, f"browser-unloadable import specifiers: {bad}"


# --- Queue API ---


async def test_queue_unknown_player(http_client: TestClient[Any, Any]) -> None:
    """GET /api/queue/{player_id} for unknown player returns empty items."""
    resp = await http_client.get("/api/queue/unknown_player")
    assert resp.status == 200
    data = await resp.json()
    assert data["items"] == []
    assert data["current_index"] == -1


async def test_queue_with_items(
    http_client: TestClient[Any, Any],
    provider: MSXBridgeProvider,
    mass_mock: Mock,
) -> None:
    """GET /api/queue/{player_id} returns queue items with current_index."""
    # Create and register a player
    player = MSXPlayer(provider, "msx_queue_test", name="Queue TV", output_format="mp3")
    player.update_state = Mock()  # type: ignore[misc,method-assign]

    # Set current media
    media = MagicMock(spec=PlayerMedia)
    media.source_id = "msx_queue_test"
    media.queue_item_id = "qi_2"
    player._attr_current_media = media

    # Mock get_player to return our player
    mass_mock.players.get_player = Mock(
        side_effect=lambda pid, **_kw: player if pid == "msx_queue_test" else None
    )

    # Create mock queue items
    qi1 = Mock()
    qi1.name = "Track 1"
    qi1.duration = 180
    qi1.image = None
    mi1 = Mock()
    mi1.name = "Track 1"
    mi1.uri = "library://track/1"
    mi1.artist_str = "Artist A"
    mi1.duration = 180
    qi1.media_item = mi1

    qi2 = Mock()
    qi2.name = "Track 2"
    qi2.duration = 240
    qi2.image = None
    mi2 = Mock()
    mi2.name = "Track 2"
    mi2.uri = "library://track/2"
    mi2.artist_str = "Artist B"
    mi2.duration = 240
    qi2.media_item = mi2

    mass_mock.player_queues.items = Mock(return_value=[qi1, qi2])

    # Mock get_item to resolve current track
    current_qi = Mock()
    current_qi.media_item = mi2
    mass_mock.player_queues.get_item = Mock(return_value=current_qi)

    resp = await http_client.get("/api/queue/msx_queue_test")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["items"]) == 2
    assert data["items"][0]["title"] == "Track 1"
    assert data["items"][0]["artist"] == "Artist A"
    assert data["items"][1]["title"] == "Track 2"
    assert data["current_index"] == 1  # Track 2 is current


class _AsyncCtx:
    """Async context manager helper for mocking session.get()."""

    def __init__(self, obj: object) -> None:
        self._obj = obj

    async def __aenter__(self) -> object:
        return self._obj

    async def __aexit__(self, *args: object) -> None:
        pass
