"""Tests for remote access feature."""

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlparse

import aiohttp
import pytest
from aiolibdatachannel import DataChannel, IceServer, PeerConnection, RTCConfiguration
from cryptography.hazmat.primitives import serialization

from music_assistant.controllers.webserver.remote_access import (
    STARTUP_DELAY,
    TASK_ID_START_GATEWAY,
    RemoteAccessInfo,
    RemoteAccessManager,
)
from music_assistant.controllers.webserver.remote_access.gateway import (
    HTTP_PROXY_CHUNK_SIZE,
    WebRTCGateway,
    WebRTCSession,
)
from music_assistant.helpers.webrtc_certificate import (
    _generate_certificate,
    _remote_id_from_certificate,
)


async def test_remote_id_from_certificate() -> None:
    """Test deterministic remote ID generation from a certificate."""
    _, cert = _generate_certificate()
    remote_id = _remote_id_from_certificate(cert)

    # Should be base32 encoded, uppercase, no padding
    assert remote_id.isalnum()
    assert remote_id == remote_id.upper()
    # 128 bits = 16 bytes -> 26 base32 chars (without padding)
    assert len(remote_id) == 26
    # deterministic: the same certificate always yields the same id
    assert _remote_id_from_certificate(cert) == remote_id


async def test_remote_access_info_dataclass() -> None:
    """Test RemoteAccessInfo dataclass."""
    info = RemoteAccessInfo(
        enabled=True,
        running=True,
        connected=False,
        remote_id="VVPN3TLP34YMGIZDINCEKQKSIR",
        using_ha_cloud=False,
        signaling_url="wss://signaling.music-assistant.io/ws",
    )

    assert info.enabled is True
    assert info.running is True
    assert info.connected is False
    assert info.remote_id == "VVPN3TLP34YMGIZDINCEKQKSIR"
    assert info.using_ha_cloud is False
    assert info.signaling_url == "wss://signaling.music-assistant.io/ws"


def _create_remote_access_manager() -> RemoteAccessManager:
    """Create an enabled remote access manager with mocked dependencies."""
    webserver = Mock()
    webserver.mass = Mock()
    webserver.logger = Mock()
    manager = RemoteAccessManager(webserver)
    manager._enabled = True
    return manager


def test_remote_access_debounces_restart_without_dropping_gateway() -> None:
    """Keep the active gateway connected while a replacement is being debounced."""
    manager = _create_remote_access_manager()
    gateway = Mock()
    gateway.stop = AsyncMock()
    manager.gateway = gateway

    manager._schedule_start()

    gateway.stop.assert_not_awaited()
    cast("Mock", manager.mass.cancel_timer).assert_called_once_with(TASK_ID_START_GATEWAY)
    cast("Mock", manager.mass.call_later).assert_called_once_with(
        STARTUP_DELAY,
        manager._restart_gateway,
        task_id=TASK_ID_START_GATEWAY,
    )


async def test_remote_access_stop_cancels_pending_restart() -> None:
    """Cancel a restart after its debounce timer has promoted it to a task."""
    manager = _create_remote_access_manager()
    gateway = Mock()
    gateway.stop = AsyncMock()
    manager.gateway = gateway

    await manager.stop()

    cast("Mock", manager.mass.cancel_timer).assert_called_once_with(TASK_ID_START_GATEWAY)
    cast("Mock", manager.mass.cancel_task).assert_called_once_with(TASK_ID_START_GATEWAY)
    gateway.stop.assert_awaited_once_with()
    assert manager.gateway is None


async def test_remote_access_serializes_concurrent_starts() -> None:
    """Create only one gateway when immediate start requests overlap."""
    manager = _create_remote_access_manager()
    start_entered = asyncio.Event()
    allow_start = asyncio.Event()
    gateway = Mock()
    gateway.is_running = True

    async def start_gateway() -> None:
        start_entered.set()
        await allow_start.wait()
        manager.gateway = gateway

    with patch.object(
        manager,
        "_start_gateway_locked",
        new=AsyncMock(side_effect=start_gateway),
    ) as start_gateway_locked:
        first_start = asyncio.create_task(manager._start_gateway())
        await start_entered.wait()
        second_start = asyncio.create_task(manager._start_gateway())
        await asyncio.sleep(0)
        allow_start.set()
        await asyncio.gather(first_start, second_start)

    start_gateway_locked.assert_awaited_once()


async def test_remote_access_provider_update_storm_schedules_one_restart() -> None:
    """Schedule one restart when concurrent provider updates detect the same mode change."""
    manager = _create_remote_access_manager()
    ice_servers = [{"urls": "turn:example.com"}]

    with (
        patch.object(
            manager,
            "_get_ha_cloud_status",
            new=AsyncMock(return_value=(True, ice_servers)),
        ),
        patch.object(manager, "_schedule_start") as schedule_start,
    ):
        await asyncio.gather(*(manager._on_providers_updated(Mock()) for _ in range(10)))

    schedule_start.assert_called_once_with()
    assert manager._target_using_ha_cloud is True


async def test_remote_access_skips_restart_after_mode_flap_settles() -> None:
    """Keep the current gateway when a fresh status check matches its active mode."""
    manager = _create_remote_access_manager()
    gateway = Mock()
    gateway.is_running = True
    manager.gateway = gateway
    manager._target_using_ha_cloud = True

    with (
        patch.object(
            manager,
            "_get_ha_cloud_status",
            new=AsyncMock(return_value=(False, None)),
        ),
        patch.object(
            manager,
            "_stop_gateway_locked",
            new=AsyncMock(),
        ) as stop_gateway,
        patch.object(
            manager,
            "_start_gateway_locked",
            new=AsyncMock(),
        ) as start_gateway,
    ):
        await manager._restart_gateway()

    stop_gateway.assert_not_awaited()
    start_gateway.assert_not_awaited()
    assert manager._target_using_ha_cloud is False


async def test_webrtc_gateway_initialization(cert_pems: tuple[str, str]) -> None:
    """Test WebRTCGateway initializes correctly."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
        signaling_url="wss://test.example.com/ws",
        local_ws_url="ws://localhost:8095/ws",
    )

    assert gateway._remote_id == "TEST-REMOTE-ID"
    assert gateway.signaling_url == "wss://test.example.com/ws"
    assert gateway.local_ws_url == "ws://localhost:8095/ws"
    assert gateway.is_running is False
    assert gateway.is_connected is False
    assert len(gateway.ice_servers) > 0


async def test_webrtc_gateway_custom_ice_servers(cert_pems: tuple[str, str]) -> None:
    """Test WebRTCGateway accepts custom ICE servers."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    custom_ice_servers = [
        {"urls": "stun:custom.stun.server:3478"},
        {"urls": "turn:custom.turn.server:3478", "username": "user", "credential": "pass"},
    ]

    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
        ice_servers=custom_ice_servers,
    )

    assert gateway.ice_servers == custom_ice_servers


async def test_webrtc_gateway_start_stop(cert_pems: tuple[str, str]) -> None:
    """Test WebRTCGateway start and stop."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )

    # Mock the _run method to avoid actual connection
    with patch.object(gateway, "_run", new_callable=AsyncMock):
        await gateway.start()
        assert gateway.is_running is True
        assert gateway._run_task is not None

        await gateway.stop()
        assert gateway.is_running is False


async def test_webrtc_gateway_handle_registration_message(cert_pems: tuple[str, str]) -> None:
    """Test WebRTCGateway handles registration confirmation."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )

    # Mock signaling WebSocket
    gateway._signaling_ws = Mock()

    message = {"type": "registered", "remoteId": "TEST-REMOTE-ID"}
    await gateway._handle_signaling_message(message)

    # Should log but not crash


async def test_webrtc_gateway_handle_error_message(cert_pems: tuple[str, str]) -> None:
    """Test WebRTCGateway handles error messages."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )

    message = {"type": "error", "message": "Test error"}
    # Should log error but not crash
    await gateway._handle_signaling_message(message)


async def test_webrtc_gateway_create_session(cert_pems: tuple[str, str]) -> None:
    """Test WebRTCGateway creates sessions for clients."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )

    session_id = "test-session-123"
    with patch.object(gateway, "_get_fresh_ice_servers", AsyncMock(return_value=[])):
        await gateway._create_session(session_id)

        assert session_id in gateway.sessions
        assert gateway.sessions[session_id].session_id == session_id
        assert gateway.sessions[session_id].pc is not None

        # Cleanup
        await gateway._close_session(session_id)


async def test_webrtc_gateway_close_session(cert_pems: tuple[str, str]) -> None:
    """Test WebRTCGateway closes sessions properly."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )

    session_id = "test-session-456"
    with patch.object(gateway, "_get_fresh_ice_servers", AsyncMock(return_value=[])):
        await gateway._create_session(session_id)
        assert session_id in gateway.sessions

        await gateway._close_session(session_id)
        assert session_id not in gateway.sessions


async def test_webrtc_gateway_close_nonexistent_session(cert_pems: tuple[str, str]) -> None:
    """Test WebRTCGateway handles closing non-existent session gracefully."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )

    # Should not raise an error
    await gateway._close_session("nonexistent-session")


async def test_webrtc_gateway_default_ice_servers(cert_pems: tuple[str, str]) -> None:
    """Test WebRTCGateway uses default ICE servers."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )

    assert len(gateway.ice_servers) > 0
    # Should have at least one STUN server
    assert any("stun:" in server["urls"] for server in gateway.ice_servers)


async def test_webrtc_gateway_handle_client_connected(cert_pems: tuple[str, str]) -> None:
    """Test WebRTCGateway handles client-connected message."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )

    with patch.object(gateway, "_get_fresh_ice_servers", AsyncMock(return_value=[])):
        message = {"type": "client-connected", "sessionId": "test-session"}
        await gateway._handle_signaling_message(message)

        # Session should be created
        assert "test-session" in gateway.sessions

        # Cleanup
        await gateway._close_session("test-session")


async def test_webrtc_gateway_handle_client_disconnected(cert_pems: tuple[str, str]) -> None:
    """Test WebRTCGateway handles client-disconnected message."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )

    with patch.object(gateway, "_get_fresh_ice_servers", AsyncMock(return_value=[])):
        # Create a session first
        session_id = "test-disconnect-session"
        await gateway._create_session(session_id)
        assert session_id in gateway.sessions

        # Handle disconnect
        message = {"type": "client-disconnected", "sessionId": session_id}
        await gateway._handle_signaling_message(message)

        # Session should be removed
        assert session_id not in gateway.sessions


async def test_webrtc_gateway_reconnection_logic(cert_pems: tuple[str, str]) -> None:
    """Test WebRTCGateway has proper reconnection backoff."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )

    # Check initial reconnect delay
    assert gateway._current_reconnect_delay == 10

    # Simulate multiple failed connections (without actually connecting)
    initial_delay = gateway._current_reconnect_delay
    gateway._current_reconnect_delay = min(
        gateway._current_reconnect_delay * 2, gateway._max_reconnect_delay
    )

    assert gateway._current_reconnect_delay == initial_delay * 2

    # Should not exceed max
    for _ in range(10):
        gateway._current_reconnect_delay = min(
            gateway._current_reconnect_delay * 2, gateway._max_reconnect_delay
        )

    assert gateway._current_reconnect_delay <= gateway._max_reconnect_delay


async def test_webrtc_gateway_handle_offer_without_session(cert_pems: tuple[str, str]) -> None:
    """Test WebRTCGateway handles offer for non-existent session gracefully."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )

    # Try to handle offer for non-existent session
    offer_data = {"sdp": "test-sdp", "type": "offer"}
    await gateway._handle_offer("nonexistent-session", offer_data)

    # Should not crash


async def test_webrtc_gateway_handle_ice_candidate_without_session(
    cert_pems: tuple[str, str],
) -> None:
    """Test WebRTCGateway handles ICE candidate for non-existent session gracefully."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )

    # Try to handle ICE candidate for non-existent session
    candidate_data = {
        "candidate": "candidate:1 1 UDP 1234 192.168.1.1 12345 typ host",
        "sdpMid": "0",
        "sdpMLineIndex": 0,
    }
    await gateway._handle_ice_candidate("nonexistent-session", candidate_data)

    # Should not crash


@pytest.mark.parametrize(
    "malicious_path",
    [
        "@evil.com",  # netloc becomes basic-auth creds, evil.com becomes the host
        "//evil.com",  # protocol-relative URL pointing at another host
        "@evil.com/foo",
        "//evil.com/foo",
    ],
)
async def test_http_proxy_request_cannot_change_host(
    cert_pems: tuple[str, str], malicious_path: str
) -> None:
    """An attacker-controlled proxy path must never change the target host (SSRF guard)."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    captured_url: dict[str, str] = {}

    def fake_request(_method: str, url: str, **_kwargs: object) -> AsyncMock:
        captured_url["url"] = url
        response = AsyncMock()
        response.status = 200
        response.headers = {}
        response.read = AsyncMock(return_value=b"")
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=response)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    mock_session.request = fake_request

    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
        local_ws_url="ws://localhost:8095/ws",
    )
    session = WebRTCSession(session_id="s1", pc=Mock())

    await gateway._handle_http_proxy_request(
        session, {"id": "1", "method": "GET", "path": malicious_path}
    )

    parsed = urlparse(captured_url["url"])
    assert parsed.hostname == "localhost"
    assert parsed.port == 8095
    assert parsed.username is None
    assert "evil.com" not in (parsed.netloc or "")


# ---- aiolibdatachannel loopback tests --------------------------------------
#
# These exercise the migrated gateway against real loopback PeerConnections
# (offerer = browser role, answerer = gateway role) rather than mocking the
# WebRTC layer. ICE servers are stubbed to [] so gathering completes on host
# candidates only (fast, offline).


@pytest.fixture(scope="session")
def cert_pems() -> tuple[str, str]:
    """Generate a throwaway DTLS cert/key as PEM strings for the gateway."""
    private_key, cert = _generate_certificate()
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem


def _sha256_fingerprint(cert_pem: str) -> str:
    """Compute the uppercase colon-separated SHA-256 fingerprint of a PEM certificate."""
    body = "".join(line for line in cert_pem.splitlines() if line and not line.startswith("-----"))
    der = base64.b64decode(body)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


class _FakeSignaling:
    """Signaling WebSocket stand-in that captures outbound JSON messages."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.messages.append(data)

    @property
    def answers(self) -> list[dict[str, Any]]:
        return [m for m in self.messages if m.get("type") == "answer"]


class _FakeLocalWS:
    """Minimal aiohttp WebSocket stand-in for channel-bridging tests."""

    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str | bytes] = []
        self._incoming: asyncio.Queue[SimpleNamespace | None] = asyncio.Queue()

    async def send_str(self, data: str) -> None:
        self.sent.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True
        self._incoming.put_nowait(None)

    def feed_text(self, data: str) -> None:
        """Queue a text message as if the local server sent it."""
        self._incoming.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=data))

    def __aiter__(self) -> AsyncIterator[SimpleNamespace]:
        return self

    async def __anext__(self) -> SimpleNamespace:
        msg = await self._incoming.get()
        if msg is None:
            raise StopAsyncIteration
        return msg


async def _wait_for(predicate: Callable[[], bool], timeout: float = 15.0) -> None:
    """Poll ``predicate`` until it is true or the timeout elapses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")


async def _connect_gateway_session(
    gateway: WebRTCGateway, session_id: str
) -> tuple[PeerConnection, DataChannel]:
    """
    Drive a browser-role offerer through the gateway until the ma-api channel opens.

    :return: Tuple of (offerer PeerConnection, opened ma-api DataChannel).
    """
    signaling = _FakeSignaling()
    gateway._signaling_ws = cast("aiohttp.ClientWebSocketResponse", signaling)
    offerer = PeerConnection(RTCConfiguration())
    dc = await offerer.create_data_channel("ma-api")
    offer = await offerer.create_offer()
    await gateway._create_session(session_id)
    await asyncio.wait_for(
        gateway._handle_offer(session_id, {"sdp": offer.sdp, "type": "offer"}), timeout=15
    )
    answer = signaling.answers[-1]
    await offerer.set_remote_description(answer["data"]["sdp"], "answer")
    await asyncio.wait_for(dc.wait_open(), timeout=15)
    return offerer, dc


async def test_handle_offer_answers_with_pinned_fingerprint(cert_pems: tuple[str, str]) -> None:
    """The answer SDP carries the DTLS fingerprint of the configured certificate (pinning)."""
    cert_pem, key_pem = cert_pems
    gateway = WebRTCGateway(
        http_session=Mock(),
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )
    signaling = _FakeSignaling()
    gateway._signaling_ws = cast("aiohttp.ClientWebSocketResponse", signaling)
    offerer = PeerConnection(RTCConfiguration())
    session_id = "fingerprint-session"
    try:
        await offerer.create_data_channel("ma-api")
        offer = await offerer.create_offer()
        with patch.object(gateway, "_get_fresh_ice_servers", AsyncMock(return_value=[])):
            await gateway._create_session(session_id)
            await asyncio.wait_for(
                gateway._handle_offer(session_id, {"sdp": offer.sdp, "type": "offer"}),
                timeout=15,
            )

        assert len(signaling.answers) == 1
        answer = signaling.answers[0]
        assert answer["sessionId"] == session_id
        assert answer["data"]["type"] == "answer"
        fingerprint_line = next(
            line for line in answer["data"]["sdp"].splitlines() if line.startswith("a=fingerprint:")
        )
        assert _sha256_fingerprint(cert_pem) in fingerprint_line
    finally:
        await gateway._close_session(session_id)
        await offerer.aclose()


async def test_ma_api_channel_bridges_to_local_ws(cert_pems: tuple[str, str]) -> None:
    """Messages flow both ways across the ma-api data channel and the local WebSocket."""
    cert_pem, key_pem = cert_pems
    fake_ws = _FakeLocalWS()
    local_ws = cast("aiohttp.ClientWebSocketResponse", fake_ws)
    http_session = Mock()
    http_session.ws_connect = AsyncMock(return_value=local_ws)
    gateway = WebRTCGateway(
        http_session=http_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )
    session_id = "bridge-session"
    with patch.object(gateway, "_get_fresh_ice_servers", AsyncMock(return_value=[])):
        offerer, dc = await _connect_gateway_session(gateway, session_id)
    try:
        await _wait_for(lambda: gateway.sessions[session_id].local_ws is local_ws)

        # browser -> local WebSocket
        await dc.send("from browser")
        await _wait_for(lambda: fake_ws.sent == ["from browser"])

        # local WebSocket -> browser
        fake_ws.feed_text("from local")
        msg = await asyncio.wait_for(dc.recv(), timeout=15)
        assert msg == "from local"
    finally:
        await gateway._close_session(session_id)
        await offerer.aclose()


def test_build_ice_servers_maps_dicts() -> None:
    """ICE server dicts map to one IceServer per url, preserving TURN credentials."""
    gateway = WebRTCGateway(
        http_session=Mock(),
        remote_id="TEST-REMOTE-ID",
        cert_pem="cert",
        key_pem="key",
    )
    servers: list[dict[str, Any]] = [
        {"urls": "stun:stun.example.com:3478"},
        {"urls": "turn:turn.example.com:3478", "username": "user", "credential": "pass"},
        {
            "urls": ["stun:a.example.com:3478", "turn:b.example.com:3478"],
            "username": "u2",
            "credential": "c2",
        },
    ]

    result = gateway._build_ice_servers(servers)

    assert all(isinstance(server, IceServer) for server in result)
    # the two-url entry fans out, so 1 + 1 + 2 = 4 IceServers
    assert len(result) == 4
    assert result[0] == IceServer(url="stun:stun.example.com:3478")
    assert result[1] == IceServer(
        url="turn:turn.example.com:3478", username="user", credential="pass"
    )
    # to_url() inlines the TURN credentials for libdatachannel
    assert result[1].to_url() == "turn:user:pass@turn.example.com:3478"
    # list urls share the entry's credentials
    assert result[2] == IceServer(url="stun:a.example.com:3478", username="u2", credential="c2")
    assert result[3] == IceServer(url="turn:b.example.com:3478", username="u2", credential="c2")


async def test_session_closes_when_ma_api_channel_closes(cert_pems: tuple[str, str]) -> None:
    """Closing the browser ma-api channel tears down the whole gateway session."""
    cert_pem, key_pem = cert_pems
    fake_ws = _FakeLocalWS()
    local_ws = cast("aiohttp.ClientWebSocketResponse", fake_ws)
    http_session = Mock()
    http_session.ws_connect = AsyncMock(return_value=local_ws)
    gateway = WebRTCGateway(
        http_session=http_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )
    session_id = "channel-close-session"
    with patch.object(gateway, "_get_fresh_ice_servers", AsyncMock(return_value=[])):
        offerer, dc = await _connect_gateway_session(gateway, session_id)
    try:
        await _wait_for(
            lambda: (
                gateway.sessions.get(session_id) is not None
                and gateway.sessions[session_id].local_ws is local_ws
            )
        )

        await dc.aclose()

        await _wait_for(lambda: session_id not in gateway.sessions)
        assert session_id not in gateway.sessions
    finally:
        await gateway._close_session(session_id)
        await offerer.aclose()


class _FakeDataChannel:
    """Data channel stand-in that captures outbound messages for proxy tests."""

    def __init__(self) -> None:
        self.is_open = True
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


def _proxy_gateway(cert_pems: tuple[str, str]) -> WebRTCGateway:
    cert_pem, key_pem = cert_pems
    return WebRTCGateway(
        http_session=Mock(),
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )


async def test_http_proxy_response_small_body_single_message(
    cert_pems: tuple[str, str],
) -> None:
    """A body within the chunk size is sent as one legacy http-proxy-response message."""
    gateway = _proxy_gateway(cert_pems)
    channel = _FakeDataChannel()
    session = cast("WebRTCSession", SimpleNamespace(data_channel=channel))
    body = b"\x00\x01\x02small-body"

    await gateway._send_http_proxy_response(session, "req-small", 200, {"X-Test": "y"}, body)

    assert len(channel.sent) == 1
    msg = json.loads(channel.sent[0])
    assert msg["type"] == "http-proxy-response"
    assert msg["id"] == "req-small"
    assert msg["status"] == 200
    assert msg["headers"] == {"X-Test": "y"}
    assert bytes.fromhex(msg["body"]) == body


async def test_http_proxy_response_large_body_chunked(cert_pems: tuple[str, str]) -> None:
    """A body over the chunk size is announced then streamed as reassemblable chunks."""
    gateway = _proxy_gateway(cert_pems)
    channel = _FakeDataChannel()
    session = cast("WebRTCSession", SimpleNamespace(data_channel=channel))
    # 2.5 chunks worth of data so the final chunk is a partial one.
    body = bytes(range(256)) * ((HTTP_PROXY_CHUNK_SIZE * 5) // 512)
    assert len(body) > HTTP_PROXY_CHUNK_SIZE

    await gateway._send_http_proxy_response(session, "req-big", 200, {}, body)

    expected_chunks = -(-len(body) // HTTP_PROXY_CHUNK_SIZE)
    start = json.loads(channel.sent[0])
    assert start["type"] == "http-proxy-response-start"
    assert start["id"] == "req-big"
    assert start["status"] == 200
    assert start["chunks"] == expected_chunks
    assert len(channel.sent) == expected_chunks + 1

    chunks = [json.loads(m) for m in channel.sent[1:]]
    assert [c["index"] for c in chunks] == list(range(expected_chunks))
    assert all(c["type"] == "http-proxy-response-chunk" for c in chunks)
    # Every serialized message must stay under the negotiated 256 KiB data-channel limit.
    assert all(len(m.encode()) < 256 * 1024 for m in channel.sent)

    reassembled = b"".join(bytes.fromhex(c["body"]) for c in chunks)
    assert reassembled == body


async def test_http_proxy_response_chunks_survive_real_data_channel(
    cert_pems: tuple[str, str],
) -> None:
    """A body too large for one message is chunked over a real libdatachannel session."""
    cert_pem, key_pem = cert_pems
    fake_ws = _FakeLocalWS()
    http_session = Mock()
    http_session.ws_connect = AsyncMock(
        return_value=cast("aiohttp.ClientWebSocketResponse", fake_ws)
    )
    gateway = WebRTCGateway(
        http_session=http_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )
    session_id = "chunk-wire-session"
    with patch.object(gateway, "_get_fresh_ice_servers", AsyncMock(return_value=[])):
        offerer, dc = await _connect_gateway_session(gateway, session_id)
    try:
        # ~293 KiB: as a single hex message this is ~586 KiB, well over the 256 KiB
        # data-channel limit, so it only arrives at all if chunking works.
        body = bytes((i * 7) % 256 for i in range(300_000))
        session = gateway.sessions[session_id]
        await gateway._send_http_proxy_response(
            session, "wire-req", 200, {"content-type": "image/jpeg"}, body
        )

        start = json.loads(await asyncio.wait_for(dc.recv(), timeout=15))
        assert start["type"] == "http-proxy-response-start"
        assert start["id"] == "wire-req"
        assert start["chunks"] > 1

        parts: list[str] = [""] * start["chunks"]
        for _ in range(start["chunks"]):
            chunk = json.loads(await asyncio.wait_for(dc.recv(), timeout=15))
            assert chunk["type"] == "http-proxy-response-chunk"
            parts[chunk["index"]] = chunk["body"]

        assert bytes.fromhex("".join(parts)) == body
    finally:
        await gateway._close_session(session_id)
        await offerer.aclose()
