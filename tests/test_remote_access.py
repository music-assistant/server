"""Tests for remote access feature."""

from unittest.mock import AsyncMock, Mock, patch

from aiortc import RTCConfiguration, RTCPeerConnection

from music_assistant.controllers.webserver.remote_access import RemoteAccessInfo
from music_assistant.controllers.webserver.remote_access.gateway import (
    WebRTCGateway,
    WebRTCSession,
    generate_remote_id,
)


async def test_generate_remote_id() -> None:
    """Test remote ID generation format."""
    remote_id = generate_remote_id()
    assert remote_id.startswith("MA-")
    parts = remote_id.split("-")
    assert len(parts) == 3
    assert parts[0] == "MA"
    assert len(parts[1]) == 4
    assert len(parts[2]) == 4
    # Ensure it's alphanumeric
    assert parts[1].isalnum()
    assert parts[2].isalnum()


async def test_remote_access_info_dataclass() -> None:
    """Test RemoteAccessInfo dataclass."""
    info = RemoteAccessInfo(
        enabled=True,
        running=True,
        connected=False,
        remote_id="MA-TEST-1234",
        using_ha_cloud=False,
        signaling_url="wss://signaling.music-assistant.io/ws",
    )

    assert info.enabled is True
    assert info.running is True
    assert info.connected is False
    assert info.remote_id == "MA-TEST-1234"
    assert info.using_ha_cloud is False
    assert info.signaling_url == "wss://signaling.music-assistant.io/ws"


async def test_webrtc_gateway_initialization() -> None:
    """Test WebRTCGateway initializes correctly."""
    mock_session = Mock()
    gateway = WebRTCGateway(
        http_session=mock_session,
        signaling_url="wss://test.example.com/ws",
        local_ws_url="ws://localhost:8095/ws",
        remote_id="MA-TEST-1234",
    )

    assert gateway.remote_id == "MA-TEST-1234"
    assert gateway.signaling_url == "wss://test.example.com/ws"
    assert gateway.local_ws_url == "ws://localhost:8095/ws"
    assert gateway.is_running is False
    assert gateway.is_connected is False
    assert len(gateway.ice_servers) > 0


async def test_webrtc_gateway_custom_ice_servers() -> None:
    """Test WebRTCGateway accepts custom ICE servers."""
    mock_session = Mock()
    custom_ice_servers = [
        {"urls": "stun:custom.stun.server:3478"},
        {"urls": "turn:custom.turn.server:3478", "username": "user", "credential": "pass"},
    ]

    gateway = WebRTCGateway(
        http_session=mock_session,
        ice_servers=custom_ice_servers,
    )

    assert gateway.ice_servers == custom_ice_servers


async def test_webrtc_gateway_start_stop() -> None:
    """Test WebRTCGateway start and stop."""
    mock_session = Mock()
    gateway = WebRTCGateway(http_session=mock_session)

    # Mock the _run method to avoid actual connection
    with patch.object(gateway, "_run", new_callable=AsyncMock):
        await gateway.start()
        assert gateway.is_running is True
        assert gateway._run_task is not None

        await gateway.stop()
        assert gateway.is_running is False


async def test_webrtc_gateway_generate_remote_id() -> None:
    """Test that WebRTCGateway generates a remote ID if not provided."""
    mock_session = Mock()
    gateway = WebRTCGateway(http_session=mock_session)

    assert gateway.remote_id is not None
    assert gateway.remote_id.startswith("MA-")


async def test_webrtc_gateway_handle_registration_message() -> None:
    """Test WebRTCGateway handles registration confirmation."""
    mock_session = Mock()
    gateway = WebRTCGateway(http_session=mock_session, remote_id="MA-TEST-1234")

    # Mock signaling WebSocket
    gateway._signaling_ws = Mock()

    message = {"type": "registered", "remoteId": "MA-TEST-1234"}
    await gateway._handle_signaling_message(message)

    # Should log but not crash


async def test_webrtc_gateway_handle_ping_pong() -> None:
    """Test WebRTCGateway handles ping/pong messages."""
    mock_session = Mock()
    gateway = WebRTCGateway(http_session=mock_session)

    # Mock signaling WebSocket
    mock_ws = AsyncMock()
    gateway._signaling_ws = mock_ws

    # Test ping
    await gateway._handle_signaling_message({"type": "ping"})
    mock_ws.send_json.assert_called_once_with({"type": "pong"})

    # Test pong
    mock_ws.reset_mock()
    await gateway._handle_signaling_message({"type": "pong"})
    # Should not send anything back
    mock_ws.send_json.assert_not_called()


async def test_webrtc_gateway_handle_error_message() -> None:
    """Test WebRTCGateway handles error messages."""
    mock_session = Mock()
    gateway = WebRTCGateway(http_session=mock_session)

    message = {"type": "error", "message": "Test error"}
    # Should log error but not crash
    await gateway._handle_signaling_message(message)


async def test_webrtc_gateway_create_session() -> None:
    """Test WebRTCGateway creates sessions for clients."""
    mock_session = Mock()
    gateway = WebRTCGateway(http_session=mock_session)

    session_id = "test-session-123"
    await gateway._create_session(session_id)

    assert session_id in gateway.sessions
    assert gateway.sessions[session_id].session_id == session_id
    assert gateway.sessions[session_id].peer_connection is not None

    # Cleanup
    await gateway._close_session(session_id)


async def test_webrtc_gateway_close_session() -> None:
    """Test WebRTCGateway closes sessions properly."""
    mock_session = Mock()
    gateway = WebRTCGateway(http_session=mock_session)

    session_id = "test-session-456"
    await gateway._create_session(session_id)
    assert session_id in gateway.sessions

    await gateway._close_session(session_id)
    assert session_id not in gateway.sessions


async def test_webrtc_gateway_close_nonexistent_session() -> None:
    """Test WebRTCGateway handles closing non-existent session gracefully."""
    mock_session = Mock()
    gateway = WebRTCGateway(http_session=mock_session)

    # Should not raise an error
    await gateway._close_session("nonexistent-session")


async def test_webrtc_gateway_default_ice_servers() -> None:
    """Test WebRTCGateway uses default ICE servers."""
    mock_session = Mock()
    gateway = WebRTCGateway(http_session=mock_session)

    assert len(gateway.ice_servers) > 0
    # Should have at least one STUN server
    assert any("stun:" in server["urls"] for server in gateway.ice_servers)


async def test_webrtc_gateway_handle_client_connected() -> None:
    """Test WebRTCGateway handles client-connected message."""
    mock_session = Mock()
    gateway = WebRTCGateway(http_session=mock_session)

    message = {"type": "client-connected", "sessionId": "test-session"}
    await gateway._handle_signaling_message(message)

    # Session should be created
    assert "test-session" in gateway.sessions

    # Cleanup
    await gateway._close_session("test-session")


async def test_webrtc_gateway_handle_client_disconnected() -> None:
    """Test WebRTCGateway handles client-disconnected message."""
    mock_session = Mock()
    gateway = WebRTCGateway(http_session=mock_session)

    # Create a session first
    session_id = "test-disconnect-session"
    await gateway._create_session(session_id)
    assert session_id in gateway.sessions

    # Handle disconnect
    message = {"type": "client-disconnected", "sessionId": session_id}
    await gateway._handle_signaling_message(message)

    # Session should be removed
    assert session_id not in gateway.sessions


async def test_webrtc_gateway_reconnection_logic() -> None:
    """Test WebRTCGateway has proper reconnection backoff."""
    mock_session = Mock()
    gateway = WebRTCGateway(http_session=mock_session)

    # Check initial reconnect delay
    assert gateway._current_reconnect_delay == 5

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


async def test_webrtc_gateway_handle_offer_without_session() -> None:
    """Test WebRTCGateway handles offer for non-existent session gracefully."""
    mock_session = Mock()
    gateway = WebRTCGateway(http_session=mock_session)

    # Try to handle offer for non-existent session
    offer_data = {"sdp": "test-sdp", "type": "offer"}
    await gateway._handle_offer("nonexistent-session", offer_data)

    # Should not crash


async def test_webrtc_gateway_handle_ice_candidate_without_session() -> None:
    """Test WebRTCGateway handles ICE candidate for non-existent session gracefully."""
    mock_session = Mock()
    gateway = WebRTCGateway(http_session=mock_session)

    # Try to handle ICE candidate for non-existent session
    candidate_data = {
        "candidate": "candidate:1 1 UDP 1234 192.168.1.1 12345 typ host",
        "sdpMid": "0",
        "sdpMLineIndex": 0,
    }
    await gateway._handle_ice_candidate("nonexistent-session", candidate_data)

    # Should not crash
