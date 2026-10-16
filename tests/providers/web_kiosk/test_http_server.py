"""Tests for the Web Kiosk HTTP server routes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from typing import Any, Self, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import ClientError, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature
from music_assistant_models.errors import InvalidDataError, UnsupportedFeaturedException
from music_assistant_models.player import PlayerMedia

import music_assistant.providers.web_kiosk.party as party_module
from music_assistant.controllers.webserver.helpers.auth_providers import AuthFailure
from music_assistant.helpers.api import APICommandHandler
from music_assistant.providers.web_kiosk.http_server import WebKioskHTTPServer
from music_assistant.providers.web_kiosk.ownership import VerifiedSendspinClaim
from music_assistant.providers.web_kiosk.player import WebKioskPlayer
from music_assistant.providers.web_kiosk.provider import WebKioskProvider


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


@pytest.mark.parametrize(
    ("path", "protected"),
    [
        ("/", False),
        ("/", True),
        ("/kiosk", False),
        ("/web", False),
        ("/web/index.html", False),
        ("/web/builder-login.html", False),
    ],
)
async def test_pages_deny_framing(
    http_client: TestClient[Any, Any],
    config_mock: Mock,
    path: str,
    protected: bool,
) -> None:
    """Credential and playback pages deny embedding in modern and legacy browsers."""
    original = config_mock.get_value.side_effect
    config_mock.get_value.side_effect = lambda key, default=None: (
        protected if key == "url_builder_require_auth" else original(key, default)
    )
    response = await http_client.get(path)
    assert response.status == (401 if protected else 200)
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert response.headers.get("Content-Security-Policy") == "frame-ancestors 'none'"


@pytest.mark.parametrize("message_type", ["position", "seek"])
@pytest.mark.parametrize(
    "position",
    [
        True,
        False,
        float("nan"),
        float("inf"),
        -float("inf"),
        -1,
        pytest.param(10**400, id="overflow"),
    ],
)
async def test_ws_rejects_invalid_positions(
    http_client: TestClient[Any, Any],
    mass_mock: Mock,
    player: WebKioskPlayer,
    message_type: str,
    position: object,
) -> None:
    """Invalid positions cannot corrupt durationless playback or break the connection."""
    mass_mock.players.get_player.return_value = player
    ws = await http_client.ws_connect("/ws?device_id=test")
    await ws.receive_json()
    player._attr_playback_state = PlaybackState.PLAYING
    player.update_position(12.0)
    await ws.send_str(
        json.dumps(
            {
                "type": message_type,
                "position": position,
                "playback_generation": player.playback_generation,
            }
        )
    )
    # A following valid capability message confirms the preceding frame was processed.
    await ws.send_json({"type": "capabilities", "volume_control": True})
    async with asyncio.timeout(1):
        while PlayerFeature.VOLUME_SET not in player.supported_features:
            await asyncio.sleep(0)
    assert PlayerFeature.VOLUME_SET in player.supported_features
    assert player.elapsed_time == 12.0
    await ws.close()


@pytest.mark.parametrize("message_type", ["position", "seek"])
@pytest.mark.parametrize("position", [0, 15.5])
async def test_ws_accepts_finite_positions(
    http_client: TestClient[Any, Any],
    mass_mock: Mock,
    player: WebKioskPlayer,
    message_type: str,
    position: float,
) -> None:
    """Finite non-negative reports still update the durationless player clock."""
    mass_mock.players.get_player.return_value = player
    ws = await http_client.ws_connect("/ws?device_id=test")
    await ws.receive_json()
    player._attr_playback_state = PlaybackState.PLAYING
    player.update_position(12.0)
    await ws.send_json(
        {
            "type": message_type,
            "position": position,
            "playback_generation": player.playback_generation,
        }
    )
    await ws.send_json({"type": "capabilities", "volume_control": True})
    async with asyncio.timeout(1):
        while PlayerFeature.VOLUME_SET not in player.supported_features:
            await asyncio.sleep(0)
    assert PlayerFeature.VOLUME_SET in player.supported_features
    assert player.elapsed_time == position
    await ws.close()


@pytest.mark.parametrize("message_type", ["position", "seek"])
@pytest.mark.parametrize("generation", [None, "", "previous-generation"])
async def test_ws_ignores_position_reports_of_another_playback(
    http_client: TestClient[Any, Any],
    mass_mock: Mock,
    player: WebKioskPlayer,
    message_type: str,
    generation: str | None,
) -> None:
    """A delayed report from a previous stream on another tab cannot move the clock."""
    mass_mock.players.get_player.return_value = player
    current = await http_client.ws_connect("/ws?device_id=test")
    await current.receive_json()
    stale = await http_client.ws_connect("/ws?device_id=test")
    await stale.receive_json()
    await player.play_media(PlayerMedia(uri="new", duration=300, source_id="wk_test"))
    player.note_seek(12.0)
    report: dict[str, Any] = {"type": message_type, "position": 250.0}
    if generation is not None:
        report["playback_generation"] = generation
    await stale.send_json(report)
    # Capability frames sent after the report confirm both sockets were processed.
    await stale.send_json({"type": "capabilities", "volume_control": True})
    await current.send_json({"type": "capabilities", "volume_control": True})
    async with asyncio.timeout(1):
        while PlayerFeature.VOLUME_SET not in player.supported_features:
            await asyncio.sleep(0)
    assert player.elapsed_time == 12.0
    await stale.close()
    await current.close()


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


async def test_ws_playback_error_asks_the_provider_to_stop(
    http_client: TestClient[Any, Any], provider: WebKioskProvider
) -> None:
    """A browser stream failure reaches the provider with its playback generation."""
    provider.playback_failed = Mock()  # type: ignore[method-assign]
    ws = await http_client.ws_connect("/ws?device_id=failing")
    hello = json.loads((await ws.receive()).data)

    await ws.send_str(
        json.dumps(
            {"type": "playback_error", "playback_generation": "gen", "reason": "NotSupportedError"}
        )
    )
    await asyncio.sleep(0.05)

    provider.playback_failed.assert_called_once_with(hello["player_id"], "gen")
    await ws.close()


async def test_ws_ended_forwards_final_position(
    http_client: TestClient[Any, Any], provider: WebKioskProvider
) -> None:
    """The final stream position sent with the end report reaches the provider."""
    provider.track_ended = Mock()  # type: ignore[method-assign]
    ws = await http_client.ws_connect("/ws?device_id=endpos")
    hello = json.loads((await ws.receive()).data)

    await ws.send_str(
        json.dumps({"type": "ended", "playback_generation": "test-generation", "position": 149.5})
    )
    await asyncio.sleep(0.05)

    provider.track_ended.assert_called_once_with(
        hello["player_id"], "test-generation", position=149.5
    )
    await ws.close()


async def test_ws_stores_verified_sendspin_client_id(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """Only an identity proven by the browser's private key reaches registration."""
    key = X25519PrivateKey.generate()
    client_id = base64.urlsafe_b64encode(key.public_key().public_bytes_raw()).decode().rstrip("=")
    ws = await http_client.ws_connect("/ws?device_id=proof&sendspin_client_id=" + client_id)
    challenge = await ws.receive_json()
    public = X25519PublicKey.from_public_bytes(
        base64.urlsafe_b64decode(challenge["server_key"] + "=")
    )
    proof = hmac.new(
        key.exchange(public), challenge["message"].encode(), hashlib.sha256
    ).hexdigest()
    await ws.send_json({"type": "sendspin_proof", "proof": proof})
    assert (await ws.receive_json())["type"] == "welcome"
    registered = mass_mock.players.register.await_args.args[0]
    assert registered.sendspin_client_id == client_id
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
    mass_mock.command_handlers = {
        "party/url": APICommandHandler.parse("party/url", plugin.get_party_url),
        "party/config": APICommandHandler.parse("party/config", plugin.get_party_config),
    }

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

    qr = await http_client.get("/api/party/qr.svg")
    assert qr.status == 200
    assert qr.content_type == "image/svg+xml"
    assert "<svg" in await qr.text()
    mass_mock.get_provider.assert_not_called()


@pytest.mark.parametrize("command", ["party/url", "party/config"])
@pytest.mark.parametrize("failure", ["absent", "error", "timeout"])
async def test_party_commands_unavailable_keep_kiosk_working(
    http_client: TestClient[Any, Any],
    mass_mock: Mock,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    failure: str,
) -> None:
    """Unavailable Party API commands disable QR without breaking the kiosk."""

    async def delayed() -> None:
        await asyncio.Event().wait()

    mass_mock.command_handlers = {
        "party/url": Mock(target=AsyncMock(return_value="http://join.example/code")),
        "party/config": Mock(target=AsyncMock(return_value=Mock(party_name="Room"))),
    }
    if failure == "absent":
        del mass_mock.command_handlers[command]
    elif failure == "error":
        mass_mock.command_handlers[command].target.side_effect = InvalidDataError("No party")
    else:
        monkeypatch.setattr(party_module, "PARTY_CALL_TIMEOUT", 0.01)
        mass_mock.command_handlers[command].target = delayed

    response = await http_client.get("/api/party")
    assert response.status == 200
    assert await response.json() == {"active": False}
    qr = await http_client.get("/api/party/qr.svg")
    assert qr.status == 404


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


async def test_builder_signin_rejects_failed_ma_login(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """A failed MA sign-in is reported as unauthorized instead of a server error."""
    mass_mock.webserver.auth.login = AsyncMock(
        return_value=AuthFailure("Invalid username or password")
    )
    mass_mock.webserver.auth.authenticate_with_token = AsyncMock()
    response = await http_client.post(
        "/builder-auth", json={"username": "test-user", "password": "wrong-test-password"}
    )
    assert response.status == 401
    assert "wk_builder_auth" not in response.cookies
    mass_mock.webserver.auth.authenticate_with_token.assert_not_awaited()


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
    provider.spawn_wave = Mock()  # type: ignore[method-assign]
    provider.spawn_lyrics = Mock()  # type: ignore[method-assign]
    await player.play_media(
        PlayerMedia(
            uri="old",
            duration=30,
            media_type=MediaType.TRACK,
            source_id="wk_test",
            queue_item_id="old-item",
        )
    )
    first = await http_client.ws_connect("/ws?device_id=test")
    await first.receive_json()
    old_play = await first.receive_json()
    await player.play_media(
        PlayerMedia(
            uri="new",
            duration=duration,
            stream_duration=stream_duration,
            media_type=MediaType.TRACK,
            source_id="wk_test",
            queue_item_id="new-item",
        )
    )
    second = await http_client.ws_connect("/ws?device_id=test")
    await second.receive_json()
    new_play = await second.receive_json()
    following = PlayerMedia(uri="next", duration=30, source_id="wk_test", queue_item_id="next")
    later = PlayerMedia(uri="later", duration=30, source_id="wk_test", queue_item_id="later")
    await player.enqueue_next_media(following)
    processed = asyncio.Event()
    provider.on_player_activity = Mock(side_effect=lambda _: processed.set())  # type: ignore[method-assign]
    try:
        await first.send_json(
            {"type": "ended", "playback_generation": old_play.get("playback_generation", "old")}
        )
        await processed.wait()
        await asyncio.sleep(0.05)
        assert player.current_media is not None
        assert player.current_media.uri == "new"
        processed.clear()
        await second.send_json(
            {"type": "ended", "playback_generation": new_play.get("playback_generation", "new")}
        )
        await processed.wait()
        await asyncio.sleep(0.05)
        assert player.current_media is following
        await player.enqueue_next_media(later)
        processed.clear()
        await second.send_json(
            {"type": "ended", "playback_generation": new_play["playback_generation"]}
        )
        await processed.wait()
        await asyncio.sleep(0.05)
        assert player.current_media is following
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


@pytest.mark.parametrize("origin", ["https://untrusted.example", "null", "http://127.0.0.1:1"])
async def test_legacy_ws_rejects_foreign_origin_before_registration(
    http_client: TestClient[Any, Any], mass_mock: Mock, origin: str
) -> None:
    """Old browsers without Fetch Metadata cannot open a cross-origin player socket."""
    with pytest.raises(WSServerHandshakeError) as error:
        await http_client.ws_connect("/ws", headers={"Origin": origin})
    assert error.value.status == 403
    mass_mock.players.register.assert_not_awaited()


async def test_legacy_ws_accepts_matching_origin(http_client: TestClient[Any, Any]) -> None:
    """A same-origin legacy browser can still register without Fetch Metadata."""
    origin = str(http_client.make_url("/")).rstrip("/")
    ws = await http_client.ws_connect("/ws", headers={"Origin": origin})
    assert (await ws.receive_json())["type"] == "welcome"
    await ws.close()


@pytest.mark.parametrize("volume_control", [None, "0", "1"])
async def test_ws_reports_browser_volume_capability(
    http_client: TestClient[Any, Any],
    mass_mock: Mock,
    player: WebKioskPlayer,
    volume_control: str | None,
) -> None:
    """MA exposes volume only after the connected browser reports it can apply commands."""
    mass_mock.players.get_player.return_value = player
    query = "" if volume_control is None else "?volume_control=" + volume_control
    ws = await http_client.ws_connect("/ws" + query)
    await ws.receive_json()
    assert (PlayerFeature.VOLUME_SET in player.supported_features) is (volume_control == "1")
    if volume_control != "1":
        before = player.volume_level
        with pytest.raises(UnsupportedFeaturedException):
            await player.volume_set(42)
        assert player.volume_level == before
    else:
        await player.volume_set(42)
        assert player.volume_level == 42
    await ws.close()


async def test_ws_volume_capability_tracks_sendspin_and_html5_playback(
    http_client: TestClient[Any, Any], mass_mock: Mock, player: WebKioskPlayer
) -> None:
    """Sendspin gains software volume, and returning to hardware-only HTML5 removes it."""
    mass_mock.players.get_player.return_value = player
    ws = await http_client.ws_connect("/ws?volume_control=0")
    await ws.receive_json()
    changed = asyncio.Event()
    with patch.object(player, "update_state", side_effect=changed.set):
        for supported in (True, False):
            changed.clear()
            await ws.send_json({"type": "capabilities", "volume_control": supported})
            await asyncio.wait_for(changed.wait(), 1)
            assert (PlayerFeature.VOLUME_SET in player.supported_features) is supported
    await ws.close()


async def test_builder_omits_inactive_ma_url_option(http_client: TestClient[Any, Any]) -> None:
    """The builder does not offer a remote MA base URL the runtime cannot use."""
    response = await http_client.get("/")
    assert response.status == 200
    assert 'id="ma_url"' not in await response.text()


async def test_browser_device_info_describes_browser(
    http_client: TestClient[Any, Any], mass_mock: Mock, player: WebKioskPlayer
) -> None:
    """Browser identification replaces repeated provider-name placeholders."""
    mass_mock.players.get_player.return_value = player
    agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/129.0.0.0 Safari/537.36"
    ws = await http_client.ws_connect("/ws", headers={"User-Agent": agent})
    welcome = await ws.receive_json()
    assert player.device_info.model == "Chrome 129 (Linux)"
    assert player.device_info.manufacturer == "Google Inc."
    assert player.device_info.software_version == "129.0.0.0"
    assert welcome["device_info"]["model"] == player.device_info.model
    await ws.close()


async def test_same_origin_routes_do_not_expose_wildcard_cors(
    http_client: TestClient[Any, Any],
) -> None:
    """Untrusted origins cannot read kiosk pages, Party credentials or API responses."""
    for path in ("/", "/kiosk", "/api/party", "/health"):
        response = await http_client.get(path, headers={"Origin": "https://untrusted.example"})
        assert "Access-Control-Allow-Origin" not in response.headers
    response = await http_client.options(
        "/api",
        headers={"Origin": "https://untrusted.example", "Access-Control-Request-Method": "POST"},
    )
    assert "Access-Control-Allow-Origin" not in response.headers


@pytest.mark.parametrize("paused", [False, True])
async def test_html5_reconnect_prepares_stream_at_current_song_position(
    http_client: TestClient[Any, Any], provider: WebKioskProvider, mass_mock: Mock, paused: bool
) -> None:
    """A reconnect gets a freshly sought stream rather than seeking unbuffered bytes."""
    registered = WebKioskPlayer(provider, "wk_resume", name="Resume")
    registered.update_state = Mock()  # type: ignore[misc,method-assign]
    await registered.play_media(
        PlayerMedia(
            uri="track",
            duration=180,
            stream_duration=160,
            title="Track",
            source_id="wk_resume",
            queue_item_id="item",
            queue_session_id="session",
        )
    )
    registered.note_seek(30)
    if paused:
        await registered.pause()
    mass_mock.players.get_player.return_value = registered

    seek_positions: list[int] = []

    async def seek_stream(player_id: str, position: int) -> None:
        assert player_id == "wk_resume"
        expected = 50 if not seek_positions else 70
        assert expected <= position <= expected + 1
        seek_positions.append(position)
        await registered.play_media(
            PlayerMedia(
                uri="track",
                duration=180,
                stream_duration=180 - position,
                title="Track",
                source_id="wk_resume",
                queue_item_id="item",
                queue_session_id="resumed",
            )
        )

    mass_mock.players.cmd_seek = AsyncMock(side_effect=seek_stream)

    async def pause_stream(_player_id: str) -> None:
        await registered.pause()

    mass_mock.players.cmd_pause = AsyncMock(side_effect=pause_stream)
    ws = await http_client.ws_connect("/ws?device_id=resume")
    await ws.receive_json()
    async with asyncio.timeout(1):
        while True:
            message = await ws.receive_json()
            if message.get("type") == "play":
                break
    assert 50 <= message["start"] <= 51
    assert message["duration"] == 180
    mass_mock.players.cmd_seek.assert_awaited_once()
    if paused:
        assert (await ws.receive_json())["type"] == "pause"
    if paused:
        await registered.play()
    registered.note_seek(20)
    if paused:
        await registered.pause()
    second = await http_client.ws_connect("/ws?device_id=resume")
    await second.receive_json()

    async def next_play(socket: Any) -> dict[str, Any]:
        async with asyncio.timeout(1):
            while True:
                payload = await socket.receive_json()
                if payload.get("type") == "play":
                    return cast("dict[str, Any]", payload)

    new_play = await next_play(second)
    existing_play = await next_play(ws)
    assert 70 <= new_play["start"] <= 71
    assert existing_play["playback_generation"] == new_play["playback_generation"]
    await second.close()
    await ws.close()


async def test_idle_reconnect_restores_volume_before_playback(
    http_client: TestClient[Any, Any], provider: WebKioskProvider, mass_mock: Mock
) -> None:
    """A reloaded idle kiosk receives its MA volume so later playback is not at 100%."""
    registered = WebKioskPlayer(provider, "wk_idle", name="Idle")
    registered.update_state = Mock()  # type: ignore[misc,method-assign]
    registered._attr_volume_level = 30
    mass_mock.players.get_player.return_value = registered
    ws = await http_client.ws_connect("/ws?device_id=idle")
    await ws.receive_json()
    async with asyncio.timeout(1):
        while True:
            message = await ws.receive_json()
            if message.get("type") == "volume":
                break
    assert message["level"] == 30
    await ws.close()


async def test_play_broadcast_carries_playback_speed(
    provider: WebKioskProvider, mass_mock: Mock
) -> None:
    """Ordinary play messages tell the browser the speed of the stream."""
    server = WebKioskHTTPServer(provider, 0)
    ws = Mock(closed=False)
    server._ws_clients["wk_test"] = {ws}
    server._ws_send = AsyncMock()  # type: ignore[method-assign]
    mass_mock.create_task = Mock(side_effect=lambda coro: coro)

    server.broadcast_play("wk_test", playback_generation="gen", playback_speed=2.0)
    await mass_mock.create_task.call_args.args[0]

    sent = server._ws_send.await_args
    assert sent is not None
    payload = json.loads(sent.args[1])
    assert payload["type"] == "play"
    assert payload["playback_speed"] == 2.0


@pytest.mark.parametrize(
    "client_id", ["spb_wk_tablet", "AbCdEfGhIjKlMnOpQrStUv", "A" * 42, "A" * 44, "A" * 42 + "B"]
)
async def test_ws_ignores_malformed_sendspin_client_id(
    http_client: TestClient[Any, Any], mass_mock: Mock, client_id: str
) -> None:
    """Legacy ids and malformed public-key encodings cannot register a bridge."""
    ws = await http_client.ws_connect(f"/ws?device_id=test-device&sendspin_client_id={client_id}")
    await ws.receive()

    registered = mass_mock.players.register.await_args.args[0]
    assert registered.sendspin_client_id is None
    await ws.close()


async def test_sendspin_proof_cannot_be_replayed_on_another_socket(
    http_client: TestClient[Any, Any], mass_mock: Mock
) -> None:
    """A valid MAC for one challenge cannot authorize a new connection."""
    key = X25519PrivateKey.generate()
    client_id = base64.urlsafe_b64encode(key.public_key().public_bytes_raw()).decode().rstrip("=")
    path = "/ws?device_id=replay&sendspin_client_id=" + client_id
    first = await http_client.ws_connect(path)
    challenge = await first.receive_json()
    public = X25519PublicKey.from_public_bytes(
        base64.urlsafe_b64decode(challenge["server_key"] + "=")
    )
    proof = hmac.new(
        key.exchange(public), challenge["message"].encode(), hashlib.sha256
    ).hexdigest()
    await first.close()
    second = await http_client.ws_connect(path)
    fresh = await second.receive_json()
    assert fresh["message"] != challenge["message"]
    await second.send_json({"type": "sendspin_proof", "proof": proof})
    async with asyncio.timeout(1):
        await second.receive()
    mass_mock.players.register.assert_not_awaited()
    await second.close()


async def test_lyrics_requested_follows_connected_kiosk_options(
    provider: WebKioskProvider,
) -> None:
    """Only a connected page with the lyrics addition asks for lyric lookups."""
    server = WebKioskHTTPServer(provider, 0)
    async with TestClient(TestServer(server.app)) as http:
        plain = await http.ws_connect("/ws?device_id=lyr")
        await plain.receive_json()
        assert not server.lyrics_requested("wk_lyr")
        showing = await http.ws_connect("/ws?device_id=lyr&lyrics=1")
        await showing.receive_json()
        assert server.lyrics_requested("wk_lyr")
        await showing.close()
        await asyncio.sleep(0.05)
        assert not server.lyrics_requested("wk_lyr")
        await plain.close()


async def test_reattach_prefers_the_verified_sendspin_owner_socket(
    provider: WebKioskProvider,
) -> None:
    """Re-enabling with an owner and an HTML5 tab keeps the owner's Sendspin identity."""
    server = WebKioskHTTPServer(provider, 0)
    sockets = {Mock(closed=False), Mock(closed=False)}
    ordered = list(sockets)
    owner, html5 = ordered[0], ordered[-1]
    claim = VerifiedSendspinClaim("wk_both", "hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo")
    server._ws_clients["wk_both"] = cast("Any", sockets)
    server._ws_claims[owner] = claim
    server._ws_peers[owner] = ("10.0.0.2", "Owner")
    server._ws_peers[html5] = ("10.0.0.3", "Html5")
    provider.get_or_register_player = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await server.reattach_player("wk_both")

    call = provider.get_or_register_player.await_args
    assert call is not None
    kwargs = call.kwargs
    assert kwargs["sendspin_claim"] is claim
    assert kwargs["sendspin_client_id"] == claim.client_id
    assert kwargs["ip_address"] == "10.0.0.2"


async def test_live_and_snapshot_play_messages_share_one_contract(
    provider: WebKioskProvider, mass_mock: Mock, player: WebKioskPlayer
) -> None:
    """A reconnect receives the same play fields as live playback, so neither drifts."""
    server = WebKioskHTTPServer(provider, 0)
    ws = Mock(closed=False)
    server._ws_clients[player.player_id] = {ws}
    sent: list[dict[str, Any]] = []
    server._ws_send = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _ws, data, *_a: sent.append(json.loads(data))
    )
    mass_mock.create_task = Mock(side_effect=lambda coro: coro)
    provider.spawn_wave = Mock()  # type: ignore[method-assign]
    provider.spawn_lyrics = Mock()  # type: ignore[method-assign]
    await player.play_media(
        PlayerMedia(uri="x", duration=120, title="T", artist="A", image_url="img")
    )

    server.broadcast_play(
        player.player_id,
        playback_generation=player.playback_generation,
        title="T",
        artist="A",
        image_url="img",
        duration=120,
        start=0,
        playback_speed=1.0,
        wave_seq=1,
    )
    await mass_mock.create_task.call_args.args[0]
    await server._send_snapshot(ws, player)

    plays = [message for message in sent if message["type"] == "play"]
    assert len(plays) == 2
    live, snapshot = plays
    assert set(live) == set(snapshot)
    assert {key: live[key] for key in live if key != "wave_seq"} == {
        key: snapshot[key] for key in snapshot if key != "wave_seq"
    }


async def test_closed_socket_leaves_no_per_socket_state(provider: WebKioskProvider) -> None:
    """Every per-socket record, including future ones, is dropped when the socket closes."""
    server = WebKioskHTTPServer(provider, 0)
    async with TestClient(TestServer(server.app)) as http:
        ws = await http.ws_connect("/ws?device_id=gone&lyrics=1&volume_control=1")
        await ws.receive_json()
        await ws.close()
        await asyncio.sleep(0.05)
    per_socket = {
        name: value
        for name, value in vars(server).items()
        if name.startswith("_ws_") and isinstance(value, (dict, set))
    }
    assert per_socket
    assert {name: value for name, value in per_socket.items() if value} == {}


async def test_reconnect_snapshot_restores_mute(
    http_client: TestClient[Any, Any], provider: WebKioskProvider, mass_mock: Mock
) -> None:
    """A tab that reloads while the kiosk is muted starts muted."""
    registered = WebKioskPlayer(provider, "wk_muted", name="Muted")
    registered.update_state = Mock()  # type: ignore[misc,method-assign]
    registered._attr_volume_muted = True
    mass_mock.players.get_player.return_value = registered
    ws = await http_client.ws_connect("/ws?device_id=muted")
    await ws.receive_json()
    async with asyncio.timeout(1):
        while True:
            message = await ws.receive_json()
            if message.get("type") == "mute":
                break
    assert message["muted"] is True
    await ws.close()


async def test_snapshot_is_not_interleaved_with_a_track_change(
    provider: WebKioskProvider, mass_mock: Mock, player: WebKioskPlayer
) -> None:
    """A track change during a slow snapshot send reaches every tab after the whole snapshot."""
    server = WebKioskHTTPServer(provider, 0)
    provider.http_server = server
    tabs = [Mock(closed=False), Mock(closed=False)]
    server._ws_clients[player.player_id] = set(tabs)
    sent: dict[object, list[dict[str, Any]]] = {tab: [] for tab in tabs}
    blocking = False
    first_send = asyncio.Event()
    release = asyncio.Event()

    async def send(ws: object, data: str, *_args: object) -> None:
        sent[ws].append(json.loads(data))
        if blocking and not first_send.is_set():
            first_send.set()
            await release.wait()

    server._ws_send = AsyncMock(side_effect=send)  # type: ignore[method-assign]
    tasks: list[asyncio.Task[None]] = []

    def spawn(coro: object) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.create_task(coro)  # type: ignore[arg-type]
        tasks.append(task)
        return task

    mass_mock.create_task.side_effect = spawn
    mass_mock.players.get_player.return_value = player
    mass_mock.players.cmd_seek = AsyncMock()
    mass_mock.players.cmd_pause = AsyncMock()
    provider.spawn_wave = Mock()  # type: ignore[method-assign]
    provider.spawn_lyrics = Mock()  # type: ignore[method-assign]
    await player.play_media(
        PlayerMedia(uri="old", duration=120, source_id="wk_test", queue_item_id="old-item")
    )
    await asyncio.wait_for(asyncio.gather(*tasks), 1)
    player.note_seek(30)
    for messages in sent.values():
        messages.clear()
    blocking = True

    # A rebuilt stream is sent to every tab of the player.
    snapshot = asyncio.create_task(server._send_snapshot(tabs[0], player))
    await asyncio.wait_for(first_send.wait(), 1)
    await player.play_media(
        PlayerMedia(uri="new", duration=120, source_id="wk_test", queue_item_id="new-item")
    )
    await asyncio.sleep(0.01)
    release.set()
    await asyncio.wait_for(snapshot, 1)
    while not all(task.done() for task in tasks):
        await asyncio.wait_for(asyncio.gather(*tasks), 1)

    for messages in sent.values():
        plays = [index for index, message in enumerate(messages) if message["type"] == "play"]
        assert messages[plays[-1]]["playback_generation"] == player.playback_generation
        assert not [m for m in messages[plays[-1] + 1 :] if m["type"] in {"seek", "pause"}]
