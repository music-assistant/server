"""Tests for remote access feature."""

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlparse

import pytest
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection
from aiortc.rtcdtlstransport import RTCCertificate
from music_assistant_models.auth import User, UserRole

from music_assistant.controllers.webserver.remote_access import RemoteAccessInfo
from music_assistant.controllers.webserver.remote_access.gateway import (
    WebRTCGateway,
    WebRTCSession,
)
from music_assistant.helpers.webrtc_certificate import (
    _generate_certificate,
    _remote_id_from_certificate,
    create_peer_connection_with_certificate,
)


class _DataChannel:
    """Minimal RTCDataChannel test double."""

    def __init__(self) -> None:
        self.closed = False
        self.handlers: dict[str, object] = {}

    def on(self, event: str) -> object:
        """Register a channel event handler."""

        def decorator(handler: object) -> object:
            self.handlers[event] = handler
            return handler

        return decorator

    def close(self) -> None:
        """Close the test channel."""
        self.closed = True


def _remote_user(role: UserRole) -> User:
    """Create an authenticated remote-access user."""
    return User(user_id=role.value, username=role.value, role=role)


async def _forward_sendspin_messages(
    gateway: WebRTCGateway,
    session: WebRTCSession,
    messages: tuple[str | bytes, ...],
) -> list[str | bytes]:
    """Forward a finite set of Sendspin messages and return what reached the server."""
    forwarded: list[str | bytes] = []
    sendspin_ws = SimpleNamespace(closed=False)

    async def forward(message: str | bytes) -> None:
        forwarded.append(message)
        if len(forwarded) == len(messages):
            sendspin_ws.closed = True

    sendspin_ws.send_str = AsyncMock(side_effect=forward)
    sendspin_ws.send_bytes = AsyncMock(side_effect=forward)
    session.sendspin_ws = sendspin_ws
    for message in messages:
        session.sendspin_queue.put_nowait(message)

    await gateway._forward_sendspin_to_local(session)
    return forwarded


@pytest.fixture
def mock_certificate() -> Mock:
    """Create a mock RTCCertificate for testing."""
    cert = Mock()
    mock_fingerprint = Mock()
    mock_fingerprint.algorithm = "sha-256"
    mock_fingerprint.value = (
        "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:"
        "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"
    )
    cert.getFingerprints.return_value = [mock_fingerprint]
    return cert


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


async def test_remote_id_matches_aiortc_fingerprint() -> None:
    """The aiortc-free remote ID must match aiortc's own certificate fingerprint derivation."""
    private_key, cert = _generate_certificate()
    rtc_cert = RTCCertificate(key=private_key, cert=cert)
    fingerprint = next(fp.value for fp in rtc_cert.getFingerprints() if fp.algorithm == "sha-256")
    expected = (
        base64.b32encode(bytes.fromhex(fingerprint.replace(":", ""))[:16])
        .decode("ascii")
        .rstrip("=")
        .replace("2", "9")
    )
    assert _remote_id_from_certificate(cert) == expected


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


async def test_webrtc_gateway_initialization(mock_certificate: Mock) -> None:
    """Test WebRTCGateway initializes correctly."""
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
        signaling_url="wss://test.example.com/ws",
        local_ws_url="ws://localhost:8095/ws",
    )

    assert gateway._remote_id == "TEST-REMOTE-ID"
    assert gateway.signaling_url == "wss://test.example.com/ws"
    assert gateway.local_ws_url == "ws://localhost:8095/ws"
    assert gateway.is_running is False
    assert gateway.is_connected is False
    assert len(gateway.ice_servers) > 0


async def test_webrtc_gateway_custom_ice_servers(mock_certificate: Mock) -> None:
    """Test WebRTCGateway accepts custom ICE servers."""
    mock_session = Mock()
    custom_ice_servers = [
        {"urls": "stun:custom.stun.server:3478"},
        {"urls": "turn:custom.turn.server:3478", "username": "user", "credential": "pass"},
    ]

    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
        ice_servers=custom_ice_servers,
    )

    assert gateway.ice_servers == custom_ice_servers


async def test_webrtc_gateway_start_stop(mock_certificate: Mock) -> None:
    """Test WebRTCGateway start and stop."""
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )

    # Mock the _run method to avoid actual connection
    with patch.object(gateway, "_run", new_callable=AsyncMock):
        await gateway.start()
        assert gateway.is_running is True
        assert gateway._run_task is not None

        await gateway.stop()
        assert gateway.is_running is False


async def test_webrtc_gateway_handle_registration_message(mock_certificate: Mock) -> None:
    """Test WebRTCGateway handles registration confirmation."""
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )

    # Mock signaling WebSocket
    gateway._signaling_ws = Mock()

    message = {"type": "registered", "remoteId": "TEST-REMOTE-ID"}
    await gateway._handle_signaling_message(message)

    # Should log but not crash


async def test_webrtc_gateway_handle_error_message(mock_certificate: Mock) -> None:
    """Test WebRTCGateway handles error messages."""
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )

    message = {"type": "error", "message": "Test error"}
    # Should log error but not crash
    await gateway._handle_signaling_message(message)


async def test_webrtc_gateway_create_session(mock_certificate: Mock) -> None:
    """Test WebRTCGateway creates sessions for clients."""
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )

    session_id = "test-session-123"
    await gateway._create_session(session_id)

    assert session_id in gateway.sessions
    assert gateway.sessions[session_id].session_id == session_id
    assert gateway.sessions[session_id].peer_connection is not None

    # Cleanup
    await gateway._close_session(session_id)


async def test_webrtc_gateway_close_session(mock_certificate: Mock) -> None:
    """Test WebRTCGateway closes sessions properly."""
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )

    session_id = "test-session-456"
    await gateway._create_session(session_id)
    assert session_id in gateway.sessions

    await gateway._close_session(session_id)
    assert session_id not in gateway.sessions


async def test_webrtc_gateway_close_nonexistent_session(mock_certificate: Mock) -> None:
    """Test WebRTCGateway handles closing non-existent session gracefully."""
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )

    # Should not raise an error
    await gateway._close_session("nonexistent-session")


async def test_webrtc_gateway_default_ice_servers(mock_certificate: Mock) -> None:
    """Test WebRTCGateway uses default ICE servers."""
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )

    assert len(gateway.ice_servers) > 0
    # Should have at least one STUN server
    assert any("stun:" in server["urls"] for server in gateway.ice_servers)


async def test_webrtc_gateway_handle_client_connected(mock_certificate: Mock) -> None:
    """Test WebRTCGateway handles client-connected message."""
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )

    message = {"type": "client-connected", "sessionId": "test-session"}
    await gateway._handle_signaling_message(message)

    # Session should be created
    assert "test-session" in gateway.sessions

    # Cleanup
    await gateway._close_session("test-session")


async def test_webrtc_gateway_handle_client_disconnected(mock_certificate: Mock) -> None:
    """Test WebRTCGateway handles client-disconnected message."""
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )

    # Create a session first
    session_id = "test-disconnect-session"
    await gateway._create_session(session_id)
    assert session_id in gateway.sessions

    # Handle disconnect
    message = {"type": "client-disconnected", "sessionId": session_id}
    await gateway._handle_signaling_message(message)

    # Session should be removed
    assert session_id not in gateway.sessions


async def test_webrtc_gateway_reconnection_logic(mock_certificate: Mock) -> None:
    """Test WebRTCGateway has proper reconnection backoff."""
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
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


async def test_webrtc_gateway_session_data_structures() -> None:
    """Test WebRTCSession data structure."""
    config = RTCConfiguration()
    pc = RTCPeerConnection(configuration=config)

    session = WebRTCSession(session_id="test-123", peer_connection=pc)

    assert session.session_id == "test-123"
    assert session.peer_connection is pc
    assert session.data_channel is None
    assert session.local_ws is None
    assert session.message_queue is not None
    assert session.forward_to_local_task is None
    assert session.forward_from_local_task is None

    # Cleanup
    await pc.close()


async def test_webrtc_gateway_handle_offer_without_session(mock_certificate: Mock) -> None:
    """Test WebRTCGateway handles offer for non-existent session gracefully."""
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )

    # Try to handle offer for non-existent session
    offer_data = {"sdp": "test-sdp", "type": "offer"}
    await gateway._handle_offer("nonexistent-session", offer_data)

    # Should not crash


async def test_webrtc_gateway_handle_ice_candidate_without_session(mock_certificate: Mock) -> None:
    """Test WebRTCGateway handles ICE candidate for non-existent session gracefully."""
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )

    # Try to handle ICE candidate for non-existent session
    candidate_data = {
        "candidate": "candidate:1 1 UDP 1234 192.168.1.1 12345 typ host",
        "sdpMid": "0",
        "sdpMLineIndex": 0,
    }
    await gateway._handle_ice_candidate("nonexistent-session", candidate_data)

    # Should not crash


async def test_create_peer_connection_with_certificate() -> None:
    """
    Test that create_peer_connection_with_certificate correctly sets the custom certificate.

    This verifies the fragile name-mangled private attribute access works correctly
    and that our custom certificate fully replaces the auto-generated one, which is
    critical for DTLS pinning.
    """
    # First verify the name-mangled attribute exists on RTCPeerConnection.
    # If aiortc changes its internals, this will fail and alert us to update our code.
    pc = RTCPeerConnection()
    try:
        assert hasattr(pc, "_RTCPeerConnection__certificates")
    finally:
        await pc.close()

    # Now test our function correctly sets the certificate
    private_key, cert = _generate_certificate()
    certificate = RTCCertificate(key=private_key, cert=cert)
    config = RTCConfiguration(iceServers=[RTCIceServer(urls="stun:stun.example.com:3478")])

    pc = create_peer_connection_with_certificate(certificate, configuration=config)

    try:
        certificates = pc._RTCPeerConnection__certificates  # type: ignore[attr-defined]
        assert len(certificates) == 1
        assert certificates[0] is certificate
    finally:
        await pc.close()


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
    mock_certificate: Mock, malicious_path: str
) -> None:
    """An attacker-controlled proxy path must never change the target host (SSRF guard)."""
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
        certificate=mock_certificate,
        local_ws_url="ws://localhost:8095/ws",
    )
    session = WebRTCSession(session_id="s1", peer_connection=Mock())

    await gateway._handle_http_proxy_request(
        session, {"id": "1", "method": "GET", "path": malicious_path}
    )

    parsed = urlparse(captured_url["url"])
    assert parsed.hostname == "localhost"
    assert parsed.port == 8095
    assert parsed.username is None
    assert "evil.com" not in (parsed.netloc or "")


@pytest.mark.parametrize(
    ("role", "allowed_roles"),
    [
        (UserRole.GUEST, ("player@v1",)),
        (UserRole.USER, None),
        (UserRole.ADMIN, None),
        (UserRole.SERVICE, None),
    ],
)
async def test_webrtc_sendspin_setup_uses_authenticated_session_role(
    mock_certificate: Mock,
    role: UserRole,
    allowed_roles: tuple[str, ...] | None,
) -> None:
    """The WebRTC Sendspin policy comes from its authenticated API session."""
    http_session = Mock()
    sendspin_ws = SimpleNamespace(closed=True)
    http_session.ws_connect = AsyncMock(return_value=sendspin_ws)
    get_user = Mock(return_value=_remote_user(role))
    gateway = WebRTCGateway(
        http_session=http_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
        get_authenticated_user_callback=get_user,
    )
    channel = _DataChannel()
    session = WebRTCSession(
        session_id="authenticated-session",
        peer_connection=Mock(),
        sendspin_channel=channel,
    )

    await gateway._setup_sendspin_channel(session)
    tasks = [
        task
        for task in (session.sendspin_to_local_task, session.sendspin_from_local_task)
        if task is not None
    ]
    await asyncio.gather(*tasks)

    get_user.assert_called_once_with("authenticated-session")
    assert session.sendspin_allowed_roles == allowed_roles
    assert channel.closed is False


@pytest.mark.parametrize("resolver_available", [False, True], ids=["missing", "unauthenticated"])
async def test_webrtc_sendspin_missing_authenticated_session_fails_closed(
    mock_certificate: Mock,
    resolver_available: bool,
) -> None:
    """A Sendspin channel cannot connect without a matching authenticated API session."""
    http_session = Mock()
    http_session.ws_connect = AsyncMock()
    get_user = Mock(return_value=None) if resolver_available else None
    gateway = WebRTCGateway(
        http_session=http_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
        get_authenticated_user_callback=get_user,
    )
    channel = _DataChannel()
    session = WebRTCSession(
        session_id="unauthenticated-session",
        peer_connection=Mock(),
        sendspin_channel=channel,
    )

    await gateway._setup_sendspin_channel(session)

    assert channel.closed is True
    http_session.ws_connect.assert_not_awaited()
    if get_user is not None:
        get_user.assert_called_once_with("unauthenticated-session")


async def test_webrtc_guest_sendspin_hello_is_always_player_only(
    mock_certificate: Mock,
) -> None:
    """Guest first and repeated hellos are rewritten while other traffic stays unchanged."""
    set_player = Mock()
    gateway = WebRTCGateway(
        http_session=Mock(),
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
        set_sendspin_player_callback=set_player,
    )
    session = WebRTCSession(
        session_id="guest-session",
        peer_connection=Mock(),
        sendspin_allowed_roles=("player@v1",),
    )
    auth = '{"type":"auth","client_id":"remote-web-player"}'
    first_hello = json.dumps(
        {
            "type": "client/hello",
            "payload": {
                "supported_roles": ["player@v1", "controller@v1", "metadata@v1"],
                "player@v1_support": {"buffer_capacity": 1024},
            },
        },
        indent=2,
    )
    repeated_hello = json.dumps(
        {
            "type": "client/hello",
            "payload": {"supported_roles": ["metadata@v1"]},
        },
        indent=2,
    )
    non_hello = '{"type":"client/state","payload":{"volume":50}}'
    malformed = "{not-json"
    list_message = "[]"
    binary = b"\x00audio"

    forwarded = await _forward_sendspin_messages(
        gateway,
        session,
        (
            auth,
            first_hello,
            non_hello,
            repeated_hello,
            malformed,
            list_message,
            binary,
        ),
    )

    assert forwarded[0] == auth
    assert json.loads(forwarded[1])["payload"]["supported_roles"] == ["player@v1"]
    assert json.loads(forwarded[1])["payload"]["player@v1_support"] == {"buffer_capacity": 1024}
    assert forwarded[2] == non_hello
    assert json.loads(forwarded[3])["payload"]["supported_roles"] == ["player@v1"]
    assert forwarded[4:] == [malformed, list_message, binary]
    assert session.sendspin_player_id == "remote-web-player"
    set_player.assert_called_once_with("guest-session", "remote-web-player")


@pytest.mark.parametrize("role", [UserRole.USER, UserRole.ADMIN, UserRole.SERVICE])
async def test_webrtc_non_guest_sendspin_traffic_is_byte_identical(
    mock_certificate: Mock,
    role: UserRole,
) -> None:
    """Regular, admin and service WebRTC Sendspin traffic remains unchanged."""
    gateway = WebRTCGateway(
        http_session=Mock(),
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )
    raw_hello = json.dumps(
        {
            "type": "client/hello",
            "payload": {"supported_roles": ["player@v1", "metadata@v1"]},
        },
        indent=2,
    )
    session = WebRTCSession(
        session_id=f"{role.value}-session",
        peer_connection=Mock(),
        sendspin_allowed_roles=None,
    )

    forwarded = await _forward_sendspin_messages(gateway, session, (raw_hello,))

    assert forwarded == [raw_hello]
