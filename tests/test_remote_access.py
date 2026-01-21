"""Tests for remote access feature."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection
from aiortc.rtcdtlstransport import RTCCertificate

from music_assistant.controllers.webserver.remote_access import RemoteAccessInfo
from music_assistant.controllers.webserver.remote_access.gateway import (
    WebRTCGateway,
    WebRTCSession,
)
from music_assistant.helpers.webrtc_certificate import (
    _generate_certificate,
    create_peer_connection_with_certificate,
    get_remote_id_from_certificate,
)


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


async def test_get_remote_id_from_certificate(mock_certificate: Mock) -> None:
    """Test remote ID generation from certificate fingerprint."""
    remote_id = get_remote_id_from_certificate(mock_certificate)

    # Should be base32 encoded, uppercase, no padding
    assert remote_id.isalnum()
    assert remote_id == remote_id.upper()
    # 128 bits = 16 bytes -> 26 base32 chars (without padding)
    assert len(remote_id) == 26


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
    """Test that create_peer_connection_with_certificate correctly sets the custom certificate.

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
