"""Tests for MSXHTTPServer routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock, patch
from urllib.parse import quote

import pytest
from aiohttp.test_utils import TestClient, TestServer

if TYPE_CHECKING:
    from aiohttp.web import Application, Request

from music_assistant.providers.msx_bridge.http_server import MSXHTTPServer
from music_assistant.providers.msx_bridge.player import MSXPlayer
from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider

# --- Bootstrap and CORS ---


async def test_health(http_client: TestClient[Request, Application]) -> None:
    """GET /health should return 200 with status ok."""
    resp = await http_client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert data["provider"] == "msx_bridge"


async def test_root_html(http_client: TestClient[Request, Application]) -> None:
    """GET / should return 200 with text/html content."""
    resp = await http_client.get("/")
    assert resp.status == 200
    assert "text/html" in resp.headers["Content-Type"]
    body = await resp.text()
    assert "MSX" in body


async def test_start_json(http_client: TestClient[Request, Application]) -> None:
    """GET /msx/start.json should return interaction mode config."""
    resp = await http_client.get("/msx/start.json")
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "Music Assistant"
    assert "menu:request:interaction:init@" in data["parameter"]
    assert "plugin.html" in data["parameter"]
    assert "scripts" not in data


async def test_plugin_html(http_client: TestClient[Request, Application]) -> None:
    """GET /msx/plugin.html should return HTML with interaction plugin."""
    resp = await http_client.get("/msx/plugin.html")
    assert resp.status == 200
    assert "text/html" in resp.headers["Content-Type"]
    body = await resp.text()
    assert "tvx.InteractionPlugin" in body
    assert "handleRequest" in body


async def test_tvx_lib(http_client: TestClient[Request, Application]) -> None:
    """GET /msx/tvx-plugin-module.min.js should return JS library."""
    resp = await http_client.get("/msx/tvx-plugin-module.min.js")
    assert resp.status == 200
    assert "javascript" in resp.headers["Content-Type"]


async def test_cors_headers(http_client: TestClient[Request, Application]) -> None:
    """Responses should include CORS Access-Control-Allow-Origin header."""
    resp = await http_client.get("/health")
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


# --- Stream proxy ---


async def test_stream_player_not_found(http_client: TestClient[Request, Application]) -> None:
    """GET /stream/nonexistent should return 404."""
    resp = await http_client.get("/stream/nonexistent")
    assert resp.status == 404


async def test_stream_no_media(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /stream/{id} should return 404 when player has no current media."""
    mock_player = Mock(spec=MSXPlayer)
    mock_player.current_media = None
    mass_mock.players.get.return_value = mock_player

    server = MSXHTTPServer(provider, 0)
    client: TestClient[Request, Application] = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/stream/msx_test")
        assert resp.status == 404
        body = await resp.text()
        assert "No active stream" in body
    finally:
        await client.close()


async def test_stream_not_msx_player(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /stream/{id} should return 400 for a non-MSX player."""
    # Return a plain Mock (not spec=MSXPlayer)
    non_msx_player = Mock()
    mass_mock.players.get.return_value = non_msx_player

    server = MSXHTTPServer(provider, 0)
    client: TestClient[Request, Application] = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/stream/other_player")
        assert resp.status == 400
        body = await resp.text()
        assert "Not an MSX player" in body
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
    mass_mock.players.get.return_value = mock_player

    # Mock get_stream to return an async generator
    mass_mock.streams = Mock()
    mass_mock.streams.get_stream = Mock(return_value=_async_iter([b"pcm-data"]))

    server = MSXHTTPServer(provider, 0)
    client: TestClient[Request, Application] = TestClient(TestServer(server.app))
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


async def test_albums(http_client: TestClient[Request, Application]) -> None:
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
    client: TestClient[Request, Application] = TestClient(TestServer(server.app))
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


async def test_album_tracks(http_client: TestClient[Request, Application]) -> None:
    """GET /api/albums/{id}/tracks should return items list."""
    resp = await http_client.get("/api/albums/1/tracks")
    assert resp.status == 200
    data = await resp.json()
    assert "items" in data


async def test_artists(http_client: TestClient[Request, Application]) -> None:
    """GET /api/artists should return items list."""
    resp = await http_client.get("/api/artists")
    assert resp.status == 200
    data = await resp.json()
    assert "items" in data
    assert "total" in data


async def test_playlists(http_client: TestClient[Request, Application]) -> None:
    """GET /api/playlists should return items list."""
    resp = await http_client.get("/api/playlists")
    assert resp.status == 200
    data = await resp.json()
    assert "items" in data
    assert "total" in data


async def test_tracks(http_client: TestClient[Request, Application]) -> None:
    """GET /api/tracks should return items list."""
    resp = await http_client.get("/api/tracks")
    assert resp.status == 200
    data = await resp.json()
    assert "items" in data
    assert "total" in data


async def test_search(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /api/search?q=test should return search results."""
    server = MSXHTTPServer(provider, 0)
    client: TestClient[Request, Application] = TestClient(TestServer(server.app))
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


async def test_search_missing_query(http_client: TestClient[Request, Application]) -> None:
    """GET /api/search without q parameter should return 400."""
    resp = await http_client.get("/api/search")
    assert resp.status == 400
    data = await resp.json()
    assert "error" in data


# --- Playback control ---


async def test_play_track(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """POST /api/play should call player_queues.play_media."""
    server = MSXHTTPServer(provider, 0)
    client: TestClient[Request, Application] = TestClient(TestServer(server.app))
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


async def test_play_invalid_body(http_client: TestClient[Request, Application]) -> None:
    """POST /api/play with invalid JSON should return 400."""
    resp = await http_client.post(
        "/api/play",
        data=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


async def test_pause(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """POST /api/pause/{id} should call cmd_pause."""
    server = MSXHTTPServer(provider, 0)
    client: TestClient[Request, Application] = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.post("/api/pause/msx_test")
        assert resp.status == 200
        mass_mock.players.cmd_pause.assert_awaited_once_with("msx_test")
    finally:
        await client.close()


async def test_stop(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """POST /api/stop/{id} should call cmd_stop."""
    server = MSXHTTPServer(provider, 0)
    client: TestClient[Request, Application] = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.post("/api/stop/msx_test")
        assert resp.status == 200
        mass_mock.players.cmd_stop.assert_awaited_once_with("msx_test")
    finally:
        await client.close()


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
    return playlist


async def test_msx_albums_have_action(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/albums.json items should have content: action for drill-down."""
    album = _make_album_mock()
    mock_result = Mock()
    mock_result.__iter__ = Mock(return_value=iter([album]))
    mass_mock.music.albums.library_items.return_value = mock_result

    server = MSXHTTPServer(provider, 0)
    client: TestClient[Request, Application] = TestClient(TestServer(server.app))
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
    client: TestClient[Request, Application] = TestClient(TestServer(server.app))
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
    client: TestClient[Request, Application] = TestClient(TestServer(server.app))
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
    """GET /msx/tracks.json items should have audio: action for playback."""
    track = _make_track_mock()
    mock_result = Mock()
    mock_result.__iter__ = Mock(return_value=iter([track]))
    mass_mock.music.tracks.library_items.return_value = mock_result

    server = MSXHTTPServer(provider, 0)
    client: TestClient[Request, Application] = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/tracks.json")
        assert resp.status == 200
        data = await resp.json()
        item = data["items"][0]
        assert "action" in item
        assert item["action"].startswith("audio:")
        assert "/msx/audio/msx_" in item["action"]
        assert quote("library://track/1", safe="") in item["action"]
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
    client: TestClient[Request, Application] = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/albums/1/tracks.json")
        assert resp.status == 200
        data = await resp.json()
        assert data["headline"] == "Album Tracks"
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["title"] == "Test Track"
        assert item["action"].startswith("audio:")
        assert "/msx/audio/msx_" in item["action"]
    finally:
        await client.close()


async def test_msx_artist_albums(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/artists/{id}/albums.json should return albums with content actions."""
    album = _make_album_mock()
    mass_mock.music.artists.albums.return_value = [album]

    server = MSXHTTPServer(provider, 0)
    client: TestClient[Request, Application] = TestClient(TestServer(server.app))
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

    async def _mock_playlist_tracks(*_args: object, **_kwargs: object) -> AsyncIterator[Any]:
        yield track

    mass_mock.music.playlists.tracks = Mock(
        side_effect=lambda *_args, **_kwargs: _mock_playlist_tracks()
    )

    server = MSXHTTPServer(provider, 0)
    client: TestClient[Request, Application] = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/playlists/1/tracks.json")
        assert resp.status == 200
        data = await resp.json()
        assert data["headline"] == "Playlist Tracks"
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["title"] == "Test Track"
        assert item["action"].startswith("audio:")
    finally:
        await client.close()


# --- MSX audio endpoint ---


async def test_msx_audio_missing_uri(http_client: TestClient[Request, Application]) -> None:
    """GET /msx/audio/msx_default without ?uri= should return 400."""
    resp = await http_client.get("/msx/audio/msx_default")
    assert resp.status == 400
    body = await resp.text()
    assert "Missing uri" in body


async def test_msx_audio_player_not_found(http_client: TestClient[Request, Application]) -> None:
    """GET /msx/audio/nonexistent?uri=x should return 404."""
    resp = await http_client.get("/msx/audio/nonexistent?uri=library://track/1")
    assert resp.status == 404


async def test_msx_audio_not_msx_player(provider: MSXBridgeProvider, mass_mock: Mock) -> None:
    """GET /msx/audio/{id}?uri=x should return 400 for non-MSX player."""
    non_msx_player = Mock()
    mass_mock.players.get.return_value = non_msx_player

    server = MSXHTTPServer(provider, 0)
    client: TestClient[Request, Application] = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/msx/audio/other?uri=library://track/1")
        assert resp.status == 400
        body = await resp.text()
        assert "Not an MSX player" in body
    finally:
        await client.close()


# --- Duration in track formatting ---


def test_format_msx_track_includes_duration(provider: MSXBridgeProvider) -> None:
    """_format_msx_track should include duration in label."""
    server = MSXHTTPServer(provider, 0)
    track = _make_track_mock()  # duration=180
    item = server._format_msx_track(track, "http://localhost", "msx_test")
    assert "3:00" in item["label"]
    assert "Test Artist" in item["label"]
    assert "background" in item
    assert item["background"] == item["image"]


def test_format_msx_track_no_duration(provider: MSXBridgeProvider) -> None:
    """_format_msx_track should handle zero/missing duration gracefully."""
    server = MSXHTTPServer(provider, 0)
    track = _make_track_mock()
    track.duration = 0
    item = server._format_msx_track(track, "http://localhost", "msx_test")
    assert item["label"] == "Test Artist"


def test_format_msx_track_duration_only(provider: MSXBridgeProvider) -> None:
    """_format_msx_track should show only duration when no artist."""
    server = MSXHTTPServer(provider, 0)
    track = _make_track_mock()
    track.artist_str = ""
    item = server._format_msx_track(track, "http://localhost", "msx_test")
    assert item["label"] == "3:00"


# --- Async iteration helpers for stream mocking ---


async def _async_iter(items: list[bytes]) -> AsyncIterator[bytes]:
    """Async generator helper for mocking iter_chunked."""
    for item in items:
        yield item


class _AsyncCtx:
    """Async context manager helper for mocking session.get()."""

    def __init__(self, obj: object) -> None:
        self._obj = obj

    async def __aenter__(self) -> object:
        return self._obj

    async def __aexit__(self, *args: object) -> None:
        pass
