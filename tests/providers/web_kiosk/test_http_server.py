"""Tests for the Web Kiosk HTTP server routes."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientError, WSServerHandshakeError
from music_assistant_models.enums import MediaType
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.web_kiosk.player import WebKioskPlayer
from music_assistant.providers.web_kiosk.provider import WebKioskProvider

if TYPE_CHECKING:
    from aiohttp.test_utils import TestClient


async def test_health_returns_ok(http_client: TestClient[Any, Any]) -> None:
    """GET /health reports the provider and player count."""
    resp = await http_client.get("/health")

    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"
    assert body["provider"] == "web_kiosk"


async def test_root_serves_dashboard(http_client: TestClient[Any, Any]) -> None:
    """GET / serves a status dashboard."""
    resp = await http_client.get("/")

    assert resp.status == 200
    assert "Web Kiosk" in await resp.text()


async def test_web_serves_spa(http_client: TestClient[Any, Any]) -> None:
    """GET /web serves the kiosk SPA HTML."""
    resp = await http_client.get("/web")

    assert resp.status == 200
    assert "<html" in (await resp.text()).lower()


async def test_stream_requires_token(
    http_client: TestClient[Any, Any], provider: WebKioskProvider, mass_mock: Mock
) -> None:
    """GET /stream/{player_id} rejects a request without a valid token."""
    registered = WebKioskPlayer(provider, "wk_test", name="Test Kiosk")
    mass_mock.players.get_player.return_value = registered

    resp = await http_client.get("/stream/wk_test?token=wrong")

    assert resp.status == 403


async def test_stream_redirects_to_ma_url(
    http_client: TestClient[Any, Any], provider: WebKioskProvider, mass_mock: Mock
) -> None:
    """GET /stream/{player_id} redirects to the MA streamserver URL."""
    media = Mock(spec=PlayerMedia)
    media.uri = "spotify://track/1"
    registered = WebKioskPlayer(provider, "wk_test", name="Test Kiosk")
    registered._attr_current_media = media
    mass_mock.players.get_player.return_value = registered
    token = provider.get_stream_token("wk_test")

    resp = await http_client.get(f"/stream/wk_test?token={token}", allow_redirects=False)

    assert resp.status == 302
    assert "/stream/1" in resp.headers["Location"]


async def test_ws_welcome_carries_player_id(http_client: TestClient[Any, Any]) -> None:
    """The WS handshake reports the server-derived player id."""
    ws = await http_client.ws_connect("/ws?device_id=test-device")

    msg = await ws.receive()
    payload = json.loads(msg.data)

    assert payload["type"] == "welcome"
    assert payload["player_id"].startswith("wk_")
    await ws.close()


async def test_ws_welcome_carries_server_version(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """The kiosk can identify the running MA version without an API token."""
    mass_mock.version = "2.17.0.dev42"
    ws = await http_client.ws_connect("/ws?device_id=version-test")
    payload = json.loads((await ws.receive()).data)
    assert payload["server_version"] == "2.17.0.dev42"
    await ws.close()


async def test_ws_ended_asks_the_provider_to_advance(
    http_client: TestClient[Any, Any], provider: WebKioskProvider
) -> None:
    """The browser reports track end on the player socket, without an API token."""
    provider.track_ended = Mock()  # type: ignore[method-assign]
    ws = await http_client.ws_connect("/ws?device_id=endnext")
    hello = json.loads((await ws.receive()).data)

    await ws.send_str(json.dumps({"type": "ended", "playback_generation": "test-generation"}))
    await asyncio.sleep(0.05)

    provider.track_ended.assert_called_once_with(hello["player_id"], "test-generation")
    await ws.close()


async def test_ws_stores_announced_sendspin_client_id(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """The browser's Sendspin identity is stored on the player it registers."""
    ws = await http_client.ws_connect(
        "/ws?device_id=test-device&sendspin_client_id=AbCdEfGhIjKlMnOpQrStUv"
    )
    await ws.receive()

    registered = mass_mock.players.register.await_args.args[0]
    assert registered.sendspin_client_id == "AbCdEfGhIjKlMnOpQrStUv"
    await ws.close()


async def test_ws_ignores_synthetic_sendspin_client_id(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """A legacy spb_wk_ id is not a sendspin-js 5 identity and is ignored."""
    ws = await http_client.ws_connect("/ws?device_id=test-device&sendspin_client_id=spb_wk_tablet")
    await ws.receive()

    registered = mass_mock.players.register.await_args.args[0]
    assert registered.sendspin_client_id is None
    await ws.close()


async def test_ws_close_keeps_player_while_another_socket_is_open(
    http_client: TestClient[Any, Any], provider: WebKioskProvider
) -> None:
    """Closing one tab does not schedule removal while another tab holds the player."""
    scheduled: list[str] = []
    provider.schedule_player_close = lambda player_id: scheduled.append(player_id)  # type: ignore[method-assign]

    first = await http_client.ws_connect("/ws?device_id=reloadkeep")
    await first.receive()
    second = await http_client.ws_connect("/ws?device_id=reloadkeep")
    await second.receive()
    await first.close()
    await asyncio.sleep(0.05)

    assert scheduled == []
    await second.close()
    await asyncio.sleep(0.05)
    assert scheduled == ["wk_reloadkeep"]


async def test_ws_rejects_cross_site(http_client: TestClient[Any, Any]) -> None:
    """A cross-site browser is not allowed to open the player WebSocket."""
    resp = await http_client.get("/ws", headers={"Sec-Fetch-Site": "cross-site"})

    assert resp.status == 403


async def test_party_qr_404_without_party_provider(
    http_client: TestClient[Any, Any],
) -> None:
    """GET /api/party/qr.svg returns 404 when the Party plugin is absent."""
    resp = await http_client.get("/api/party/qr.svg")

    assert resp.status == 404


async def test_party_info_is_inactive_without_plugin(
    http_client: TestClient[Any, Any],
) -> None:
    """The kiosk can ask if a party is active without an API token."""
    resp = await http_client.get("/api/party")

    assert resp.status == 200
    assert await resp.json() == {"active": False}


async def test_party_info_hides_the_join_url(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """The caption is public. The guest link stays inside the QR image."""
    plugin = Mock()
    plugin.get_party_url = AsyncMock(return_value="http://join.example/secret-code")
    plugin.get_party_config = AsyncMock(return_value=Mock(party_name="Room", qr_text="Scan"))
    mass_mock.get_provider.return_value = plugin

    resp = await http_client.get("/api/party")
    body = await resp.json()

    assert resp.status == 200
    assert body == {
        "active": True,
        "name": "Room",
        "qr_text": "Scan",
        "version": body["version"],
    }
    assert "secret-code" not in str(body)


class _Upstream:
    """Minimal async context manager standing in for aiohttp's request result."""

    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {"Content-Type": "application/json"}

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _point_mass_at(mass_mock: Mock, base: str) -> _Upstream:
    """Make the mock webserver dialable and capture one upstream response."""
    upstream = _Upstream(200, b'{"items":[],"count":0}')
    mass_mock.webserver = Mock(internal_base_url=base)
    mass_mock.http_session.request = Mock(return_value=upstream)
    return upstream


async def test_api_proxy_forwards_jsonrpc(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """POST /api forwards the bearer token and body to the MA webserver."""
    _point_mass_at(mass_mock, "http://127.0.0.1:8095")

    resp = await http_client.post(
        "/api",
        json={"command": "music/albums/library_items", "args": {"limit": 1}, "message_id": "1"},
        headers={"Authorization": "Bearer test-token"},
    )

    assert resp.status == 200
    assert await resp.json() == {"items": [], "count": 0}
    args, kwargs = mass_mock.http_session.request.call_args
    assert args == ("POST", "http://127.0.0.1:8095/api")
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    forwarded = json.loads(kwargs["data"])
    assert forwarded["command"] == "music/albums/library_items"


async def test_imageproxy_forwards_query(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """GET /imageproxy forwards the path and query without requiring a token."""
    upstream = _Upstream(200, b"PNG", {"Content-Type": "image/png"})
    mass_mock.webserver = Mock(internal_base_url="http://127.0.0.1:8095/")
    mass_mock.http_session.request = Mock(return_value=upstream)

    resp = await http_client.get("/imageproxy/abc123?size=200")

    assert resp.status == 200
    assert await resp.read() == b"PNG"
    assert resp.content_type == "image/png"
    args, kwargs = mass_mock.http_session.request.call_args
    assert args == ("GET", "http://127.0.0.1:8095/imageproxy/abc123?size=200")
    assert kwargs["data"] is None
    assert "Authorization" not in kwargs["headers"]


async def test_api_proxy_falls_back_to_base_url(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """A missing internal URL falls back to the advertised base URL."""
    upstream = _Upstream(200, b"{}")
    webserver = Mock()
    webserver.internal_base_url = None
    webserver.base_url = "http://172.18.0.2:8095/"
    mass_mock.webserver = webserver
    mass_mock.http_session.request = Mock(return_value=upstream)

    resp = await http_client.post("/api", json={"command": "music/search"})

    assert resp.status == 200
    args, _kwargs = mass_mock.http_session.request.call_args
    assert args == ("POST", "http://172.18.0.2:8095/api")


async def test_api_proxy_503_without_webserver(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """POST /api reports unavailable when this process cannot see the webserver."""
    mass_mock.webserver = None

    resp = await http_client.post("/api", json={"command": "music/search"})

    assert resp.status == 503
    mass_mock.http_session.request.assert_not_called()


async def test_imageproxy_rejects_parent_segments(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """A path that still contains '..' is not forwarded."""
    _point_mass_at(mass_mock, "http://127.0.0.1:8095")

    resp = await http_client.get("/imageproxy/foo..bar")

    assert resp.status == 400
    mass_mock.http_session.request.assert_not_called()


async def test_api_proxy_502_when_upstream_fails(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """A connection error to Music Assistant becomes a 502."""
    mass_mock.webserver = Mock(internal_base_url="http://127.0.0.1:8095")
    mass_mock.http_session.request = Mock(side_effect=ClientError())

    resp = await http_client.post("/api", json={"command": "music/search"})

    assert resp.status == 502
    assert "unreachable" in await resp.text()


async def test_canonical_kiosk_route_serves_player(http_client: TestClient[Any, Any]) -> None:
    """The canonical kiosk route serves the player with no query parameters."""
    response = await http_client.get("/kiosk")
    assert response.status == 200
    assert 'id="kiosk-art"' in await response.text()


async def test_protected_builder_requires_valid_ma_auth(
    http_client: TestClient[Any, Any], config_mock: Mock, mass_mock: Mock
) -> None:
    """The protected URL builder never discloses its content without MA sign-in."""
    original_get_value = config_mock.get_value.side_effect
    config_mock.get_value.side_effect = lambda key, default=None: (
        True if key == "url_builder_require_auth" else original_get_value(key, default)
    )
    mass_mock.webserver.auth.authenticate_with_token = AsyncMock(return_value=None)
    response = await http_client.get("/")
    assert response.status == 401
    assert "Kiosk URL Builder" not in await response.text()
    response = await http_client.post("/builder-auth", json={"token": "invalid-test-token"})
    assert response.status == 401
    mass_mock.webserver.auth.authenticate_with_token.return_value = Mock(username="test-user")
    response = await http_client.post("/builder-auth", json={"token": "valid-test-token"})
    assert response.status == 200
    assert response.cookies["wk_builder_auth"]["httponly"]
    response = await http_client.get("/")
    assert response.status == 200
    assert "Kiosk URL Builder" in await response.text()
    mass_mock.webserver.auth.authenticate_with_token.return_value = None
    response = await http_client.get("/")
    assert response.status == 401
    assert (await http_client.get("/kiosk")).status == 200


async def test_builder_signin_uses_ma_credentials_and_rejects_cross_site(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """Builder sign-in delegates credentials to MA and rejects cross-site sign-in."""
    mass_mock.webserver.auth.login = AsyncMock(
        return_value={"success": True, "access_token": "test-access-token"}
    )
    mass_mock.webserver.auth.authenticate_with_token = AsyncMock(
        return_value=Mock(username="test-user")
    )
    response = await http_client.post(
        "/builder-auth", json={"username": "test-user", "password": "test-password"}
    )
    assert response.status == 200
    mass_mock.webserver.auth.login.assert_awaited_once_with(
        username="test-user", password="test-password", device_name="Web Kiosk URL Builder"
    )
    response = await http_client.post(
        "/builder-auth",
        json={"token": "test-access-token"},
        headers={"Origin": "https://other.example"},
    )
    assert response.status == 403
    mass_mock.webserver.auth.login.return_value = {"success": False}
    response = await http_client.post(
        "/builder-auth", json={"username": "test-user", "password": "wrong-test-password"}
    )
    assert response.status == 401


async def test_ws_rejects_same_site_browser(http_client: TestClient[Any, Any]) -> None:
    """A browser on another origin must not receive player snapshots or stream tokens."""
    with pytest.raises(WSServerHandshakeError) as error:
        await http_client.ws_connect("/ws", headers={"Sec-Fetch-Site": "same-site"})

    assert error.value.status == 403


@pytest.mark.parametrize("fetch_site", ["same-origin", "none"])
async def test_ws_accepts_same_origin_or_direct_navigation(
    http_client: TestClient[Any, Any], fetch_site: str
) -> None:
    """The origin guard keeps legitimate browser connections working."""
    ws = await http_client.ws_connect("/ws", headers={"Sec-Fetch-Site": fetch_site})
    payload = json.loads((await ws.receive()).data)
    assert payload["type"] == "welcome"
    await ws.close()


@pytest.mark.parametrize(("duration", "stream_duration"), [(5, 5), (180, 4)])
async def test_delayed_ended_cannot_advance_a_replacement_stream(
    http_client: TestClient[Any, Any],
    provider: WebKioskProvider,
    mass_mock: Mock,
    player: WebKioskPlayer,
    duration: int,
    stream_duration: int,
) -> None:
    """Old end messages cannot skip a short replacement track or a seek near its end."""
    tasks: list[asyncio.Task[None]] = []

    def spawn(coro: object) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.create_task(coro)  # type: ignore[arg-type]
        tasks.append(task)
        return task

    mass_mock.create_task.side_effect = spawn
    mass_mock.players.get_player.return_value = player
    mass_mock.player_queues.get_active_queue.return_value = Mock(
        queue_id="wk_test", current_index=0
    )
    mass_mock.player_queues.get_next_item.return_value = Mock(queue_item_id="next")
    mass_mock.player_queues.index_by_id.return_value = 1
    mass_mock.player_queues.play_index = AsyncMock()
    provider.spawn_wave = Mock()  # type: ignore[method-assign]
    provider.spawn_lyrics = Mock()  # type: ignore[method-assign]
    await player.play_media(PlayerMedia(uri="old", duration=30, media_type=MediaType.TRACK))
    first = await http_client.ws_connect("/ws?device_id=test")
    await first.receive_json()
    old_play = await first.receive_json()
    await player.play_media(
        PlayerMedia(
            uri="new",
            duration=duration,
            stream_duration=stream_duration,
            media_type=MediaType.TRACK,
        )
    )
    second = await http_client.ws_connect("/ws?device_id=test")
    await second.receive_json()
    new_play = await second.receive_json()
    processed = asyncio.Event()
    provider.on_player_activity = Mock(side_effect=lambda _: processed.set())  # type: ignore[method-assign]
    try:
        await first.send_json(
            {"type": "ended", "playback_generation": old_play.get("playback_generation", "old")}
        )
        await processed.wait()
        await asyncio.sleep(0)
        mass_mock.player_queues.play_index.assert_not_awaited()
        processed.clear()
        await second.send_json(
            {"type": "ended", "playback_generation": new_play.get("playback_generation", "new")}
        )
        await processed.wait()
        await asyncio.sleep(0)
        mass_mock.player_queues.play_index.assert_awaited_once_with("wk_test", 1)
        processed.clear()
        await second.send_json(
            {"type": "ended", "playback_generation": new_play["playback_generation"]}
        )
        await processed.wait()
        await asyncio.sleep(0)
        mass_mock.player_queues.play_index.assert_awaited_once()
    finally:
        await first.close()
        await second.close()
        await provider.unload()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_ws_ignores_ended_without_a_playback_generation(
    http_client: TestClient[Any, Any], provider: WebKioskProvider
) -> None:
    """A cached client cannot advance playback with an unidentified end message."""
    provider.track_ended = Mock()  # type: ignore[method-assign]
    ws = await http_client.ws_connect("/ws?device_id=untagged")
    await ws.receive_json()
    processed = asyncio.Event()
    provider.on_player_activity = Mock(side_effect=lambda _: processed.set())  # type: ignore[method-assign]
    await ws.send_json({"type": "ended"})
    await processed.wait()
    provider.track_ended.assert_not_called()
    await ws.close()
