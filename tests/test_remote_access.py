"""Tests for remote access feature."""

import asyncio
import base64
import contextlib
import json
from collections.abc import Callable
from typing import cast
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlparse

import pytest
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection
from aiortc.rtcdtlstransport import RTCCertificate

from music_assistant.controllers.webserver.remote_access import (
    STARTUP_DELAY,
    TASK_ID_START_GATEWAY,
    RemoteAccessInfo,
    RemoteAccessManager,
)
from music_assistant.controllers.webserver.remote_access.gateway import (
    WebRTCGateway,
    WebRTCSession,
)
from music_assistant.helpers.webrtc_certificate import (
    _generate_certificate,
    _remote_id_from_certificate,
    create_peer_connection_with_certificate,
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


async def test_sendspin_handler_queues_message_before_setup_runs(
    mock_certificate: Mock,
) -> None:
    """Queue the first Sendspin message as soon as its data channel is announced."""
    gateway = WebRTCGateway(
        http_session=Mock(),
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )
    session_id = "sendspin-session"
    await gateway._create_session(session_id)
    session = gateway.sessions[session_id]
    callbacks: dict[str, Callable[..., None]] = {}
    channel = Mock()
    channel.label = "sendspin"

    def register_callback(
        event: str,
    ) -> Callable[[Callable[..., None]], Callable[..., None]]:
        def decorator(callback: Callable[..., None]) -> Callable[..., None]:
            callbacks[event] = callback
            return callback

        return decorator

    channel.on.side_effect = register_callback

    try:
        with patch.object(gateway, "_setup_sendspin_channel", new=AsyncMock()):
            session.peer_connection.emit("datachannel", channel)
            message = '{"type":"auth","client_id":"web-player"}'
            callbacks["message"](message)
            await asyncio.sleep(0)
            assert session.sendspin_queue.get_nowait() == message
    finally:
        await gateway._close_session(session_id)


async def test_api_handler_queues_message_before_setup_runs(
    mock_certificate: Mock,
) -> None:
    """Queue the first API message as soon as its data channel is announced."""
    gateway = WebRTCGateway(
        http_session=Mock(),
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )
    session_id = "api-session"
    await gateway._create_session(session_id)
    session = gateway.sessions[session_id]
    callbacks: dict[str, Callable[..., None]] = {}
    channel = Mock()
    channel.label = "ma-api"

    def register_callback(
        event: str,
    ) -> Callable[[Callable[..., None]], Callable[..., None]]:
        def decorator(callback: Callable[..., None]) -> Callable[..., None]:
            callbacks[event] = callback
            return callback

        return decorator

    channel.on.side_effect = register_callback

    try:
        with patch.object(gateway, "_setup_data_channel", new=AsyncMock()):
            session.peer_connection.emit("datachannel", channel)
            message = '{"command":"server/info"}'
            callbacks["message"](message)
            await asyncio.sleep(0)
            assert session.message_queue.get_nowait() == message
    finally:
        await gateway._close_session(session_id)


async def test_sendspin_setup_failure_stops_accepting_messages(
    mock_certificate: Mock,
) -> None:
    """Drop queued and future messages when the internal audio bridge cannot connect."""
    http_session = Mock()
    http_session.ws_connect = AsyncMock(side_effect=RuntimeError("connection failed"))
    gateway = WebRTCGateway(
        http_session=http_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )
    session = WebRTCSession(session_id="sendspin-session", peer_connection=Mock())
    callbacks: dict[str, Callable[..., None]] = {}
    channel = Mock()
    session.sendspin_channel = channel
    session.sendspin_channel_active = True

    def register_callback(
        event: str,
    ) -> Callable[[Callable[..., None]], Callable[..., None]]:
        def decorator(callback: Callable[..., None]) -> Callable[..., None]:
            callbacks[event] = callback
            return callback

        return decorator

    channel.on.side_effect = register_callback
    gateway._register_sendspin_channel_handlers(session)
    callbacks["message"]("queued before setup")
    await asyncio.sleep(0)
    assert session.sendspin_queue.qsize() == 1

    await gateway._setup_sendspin_channel(session)
    callbacks["message"]("sent after failure")
    await asyncio.sleep(0)

    assert session.sendspin_channel_active is False
    assert session.sendspin_queue.empty()
    channel.close.assert_called_once_with()


async def test_sendspin_close_during_setup_closes_internal_bridge(
    mock_certificate: Mock,
) -> None:
    """Close a late internal bridge when its remote data channel is already gone."""
    connect_started = asyncio.Event()
    allow_connect = asyncio.Event()
    internal_ws = AsyncMock()
    internal_ws.closed = False

    async def connect_internal(_url: str) -> AsyncMock:
        connect_started.set()
        await allow_connect.wait()
        return internal_ws

    http_session = Mock()
    http_session.ws_connect = AsyncMock(side_effect=connect_internal)
    gateway = WebRTCGateway(
        http_session=http_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )
    session = WebRTCSession(session_id="sendspin-session", peer_connection=Mock())
    callbacks: dict[str, Callable[..., None]] = {}
    channel = Mock()
    session.sendspin_channel = channel
    session.sendspin_channel_active = True

    def register_callback(
        event: str,
    ) -> Callable[[Callable[..., None]], Callable[..., None]]:
        def decorator(callback: Callable[..., None]) -> Callable[..., None]:
            callbacks[event] = callback
            return callback

        return decorator

    channel.on.side_effect = register_callback
    gateway._register_sendspin_channel_handlers(session)

    setup_task = asyncio.create_task(gateway._setup_sendspin_channel(session))
    await connect_started.wait()
    callbacks["close"]()
    allow_connect.set()
    await setup_task

    internal_ws.close.assert_awaited_once_with()
    assert session.sendspin_ws is None
    assert session.sendspin_to_local_task is None
    assert session.sendspin_from_local_task is None


async def test_sendspin_channel_close_cleans_forwarding_tasks(
    mock_certificate: Mock,
) -> None:
    """Cancel forwarding tasks when the remote audio data channel closes."""
    gateway = WebRTCGateway(
        http_session=Mock(),
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )
    session = WebRTCSession(session_id="sendspin-session", peer_connection=Mock())
    gateway.sessions[session.session_id] = session
    callbacks: dict[str, Callable[..., None]] = {}
    channel = Mock()
    session.sendspin_channel = channel
    session.sendspin_channel_active = True
    sendspin_ws = AsyncMock()
    sendspin_ws.closed = False
    session.sendspin_ws = sendspin_ws

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    to_local = asyncio.create_task(wait_forever())
    from_local = asyncio.create_task(wait_forever())
    session.sendspin_to_local_task = to_local
    session.sendspin_from_local_task = from_local

    def register_callback(
        event: str,
    ) -> Callable[[Callable[..., None]], Callable[..., None]]:
        def decorator(callback: Callable[..., None]) -> Callable[..., None]:
            callbacks[event] = callback
            return callback

        return decorator

    channel.on.side_effect = register_callback
    gateway._register_sendspin_channel_handlers(session)
    callbacks["close"]()
    for _ in range(20):
        await asyncio.sleep(0)
        if to_local.done() and from_local.done() and not gateway._background_tasks:
            break

    assert session.sendspin_channel_active is False
    assert to_local.cancelled()
    assert from_local.cancelled()
    assert vars(session)["sendspin_to_local_task"] is None
    assert vars(session)["sendspin_from_local_task"] is None
    assert session.sendspin_ws is None
    sendspin_ws.close.assert_awaited_once_with()
    gateway.sessions.pop(session.session_id)


async def test_sendspin_session_close_cancels_inflight_setup(
    mock_certificate: Mock,
) -> None:
    """Cancel a pending internal audio connection before closing its WebRTC session."""
    connect_started = asyncio.Event()

    async def connect_internal(_url: str) -> None:
        connect_started.set()
        await asyncio.Event().wait()

    http_session = Mock()
    http_session.ws_connect = AsyncMock(side_effect=connect_internal)
    gateway = WebRTCGateway(
        http_session=http_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )
    peer_connection = Mock()
    peer_connection.close = AsyncMock()
    session = WebRTCSession(
        session_id="sendspin-session",
        peer_connection=peer_connection,
    )
    session.sendspin_channel = Mock()
    session.sendspin_channel_active = True
    setup_task = asyncio.create_task(gateway._setup_sendspin_channel(session))
    session.sendspin_setup_task = setup_task
    gateway.sessions[session.session_id] = session
    await connect_started.wait()

    await gateway._close_session(session.session_id)

    assert setup_task.cancelled()
    assert vars(session)["sendspin_setup_task"] is None
    assert session.session_id not in vars(gateway)["sessions"]
    peer_connection.close.assert_awaited_once_with()


async def test_api_session_close_cancels_inflight_setup(
    mock_certificate: Mock,
) -> None:
    """Cancel a pending local API connection before closing its WebRTC session."""
    connect_started = asyncio.Event()

    async def connect_local(_url: str) -> None:
        connect_started.set()
        await asyncio.Event().wait()

    http_session = Mock()
    http_session.ws_connect = AsyncMock(side_effect=connect_local)
    gateway = WebRTCGateway(
        http_session=http_session,
        remote_id="TEST-REMOTE-ID",
        certificate=mock_certificate,
    )
    peer_connection = Mock()
    peer_connection.close = AsyncMock()
    session = WebRTCSession(
        session_id="api-session",
        peer_connection=peer_connection,
    )
    session.data_channel = Mock()
    session.data_channel_active = True
    setup_task = asyncio.create_task(gateway._setup_data_channel(session))
    session.data_channel_setup_task = setup_task
    gateway.sessions[session.session_id] = session
    await connect_started.wait()

    await gateway._close_session(session.session_id)

    assert setup_task.cancelled()
    assert vars(session)["data_channel_setup_task"] is None
    assert session.session_id not in vars(gateway)["sessions"]
    peer_connection.close.assert_awaited_once_with()


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


async def test_http_proxy_request_does_not_block_receive_loop(mock_certificate: Mock) -> None:
    """A slow proxied image fetch must not stall API messages behind it in the receive loop."""
    mock_session = Mock()
    release = asyncio.Event()

    def fake_request(_method: str, _url: str, **_kwargs: object) -> AsyncMock:
        response = AsyncMock()
        response.status = 200
        response.headers = {}
        response.read = AsyncMock(return_value=b"")

        async def blocking_aenter(*_args: object) -> AsyncMock:
            await release.wait()
            return response

        ctx = AsyncMock()
        ctx.__aenter__ = blocking_aenter
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
    session.local_ws = Mock()
    session.local_ws.closed = False
    session.local_ws.send_str = AsyncMock()
    session.data_channel = Mock()
    session.data_channel.readyState = "open"
    session.data_channel.send = Mock()

    proxy_message = json.dumps(
        {"type": "http-proxy-request", "id": "1", "method": "GET", "path": "/imageproxy/x"}
    )
    api_message = '{"command": "ping"}'
    session.message_queue.put_nowait(proxy_message)
    session.message_queue.put_nowait(api_message)

    forward_task = asyncio.create_task(gateway._forward_to_local(session))

    try:
        # The API message must get through while the proxy fetch is still blocked.
        for _ in range(20):
            if session.local_ws.send_str.await_args_list:
                break
            await asyncio.sleep(0.05)
        assert not release.is_set()
        session.local_ws.send_str.assert_awaited_with(api_message)

        release.set()

        for _ in range(20):
            if session.data_channel.send.call_args_list:
                break
            await asyncio.sleep(0.05)
        assert session.data_channel.send.call_args is not None
        response_data = json.loads(session.data_channel.send.call_args[0][0])
        assert response_data["id"] == "1"
    finally:
        # Unblock a still-pending fetch so cleanup can't hang on assertion failure
        release.set()
        session.local_ws.closed = True
        forward_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await forward_task
        for task in list(session.proxy_tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def test_close_data_channel_bridge_cancels_pending_proxy_tasks(
    mock_certificate: Mock,
) -> None:
    """Closing a session's bridge must cancel its still-running proxy fetches."""
    mock_session = Mock()
    release = asyncio.Event()

    def fake_request(_method: str, _url: str, **_kwargs: object) -> AsyncMock:
        async def blocking_aenter(*_args: object) -> AsyncMock:
            await release.wait()
            return AsyncMock()

        ctx = AsyncMock()
        ctx.__aenter__ = blocking_aenter
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
    session.local_ws = Mock()
    session.local_ws.closed = False
    session.local_ws.close = AsyncMock()
    session.data_channel = Mock()
    session.data_channel.readyState = "open"

    session.message_queue.put_nowait(
        json.dumps({"type": "http-proxy-request", "id": "1", "method": "GET", "path": "/img"})
    )
    session.forward_to_local_task = asyncio.create_task(gateway._forward_to_local(session))

    try:
        for _ in range(20):
            if session.proxy_tasks:
                break
            await asyncio.sleep(0.05)
        proxy_task = next(iter(session.proxy_tasks))

        await gateway._close_data_channel_bridge(session)

        assert proxy_task.cancelled()
        assert not session.proxy_tasks
    finally:
        release.set()
