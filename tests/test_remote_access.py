"""Tests for remote access feature."""

import asyncio
import base64
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from urllib.parse import urlparse

import aiohttp
import pytest
from aiolibdatachannel import (
    DataChannel,
    IceServer,
    LogLevel,
    PeerConnection,
    RTCConfiguration,
    RTCError,
)
from cryptography.hazmat.primitives import serialization

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.webserver.remote_access import (
    STARTUP_DELAY,
    TASK_ID_START_GATEWAY,
    RemoteAccessInfo,
    RemoteAccessManager,
)
from music_assistant.controllers.webserver.remote_access.gateway import (
    DATA_CHANNEL_CHUNK_SIZE,
    HTTP_PROXY_CONCURRENCY,
    WebRTCGateway,
    WebRTCSession,
    _is_usable_ice_url,
)
from music_assistant.helpers.webrtc_certificate import (
    _generate_certificate,
    _remote_id_from_certificate,
)

_GATEWAY_MODULE = "music_assistant.controllers.webserver.remote_access.gateway"


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


async def test_remote_access_gateway_uses_internal_sendspin_url(
    cert_pems: tuple[str, str],
) -> None:
    """Bridge the Sendspin data channel to the locally reachable Sendspin server."""
    internal_url = "ws://127.0.0.1:8927/sendspin"
    manager = _create_remote_access_manager()
    manager._remote_id = "TEST-REMOTE-ID"
    cast("Mock", manager.webserver).internal_base_url = "http://127.0.0.1:8095"
    cast("Mock", manager.webserver).internal_sendspin_url = internal_url

    with (
        patch(
            "music_assistant.controllers.webserver.remote_access"
            ".get_or_create_webrtc_certificate_pems",
            return_value=cert_pems,
        ),
        patch.object(manager, "_get_ha_cloud_status", new=AsyncMock(return_value=(False, None))),
        patch.object(WebRTCGateway, "start", new=AsyncMock()),
    ):
        await manager._start_gateway_locked()

    assert manager.gateway is not None
    assert manager.gateway.sendspin_url == internal_url


@pytest.mark.parametrize(
    ("internal_base_url", "expected"),
    [
        ("http://127.0.0.1:8095", "ws://127.0.0.1:8095/ws"),
        ("https://127.0.0.1:8095", "wss://127.0.0.1:8095/ws"),
    ],
)
async def test_remote_access_gateway_uses_internal_base_url(
    cert_pems: tuple[str, str],
    internal_base_url: str,
    expected: str,
) -> None:
    """Bridge the API data channel to the locally reachable webserver."""
    manager = _create_remote_access_manager()
    manager._remote_id = "TEST-REMOTE-ID"
    # an external base URL must never be dialed back into this host
    cast("Mock", manager.mass).webserver.base_url = "https://ma.example.com"
    cast("Mock", manager.webserver).internal_base_url = internal_base_url

    with (
        patch(
            "music_assistant.controllers.webserver.remote_access"
            ".get_or_create_webrtc_certificate_pems",
            return_value=cert_pems,
        ),
        patch.object(manager, "_get_ha_cloud_status", new=AsyncMock(return_value=(False, None))),
        patch.object(WebRTCGateway, "start", new=AsyncMock()),
    ):
        await manager._start_gateway_locked()

    assert manager.gateway is not None
    assert manager.gateway.local_ws_url == expected


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


async def test_webrtc_gateway_local_ice_servers_skip_non_udp_turn(
    cert_pems: tuple[str, str],
) -> None:
    """Test the local peer connection only gets ICE servers libjuice can use."""
    cert_pem, key_pem = cert_pems
    cred = {"username": "u", "credential": "p"}
    # shape of what HA Cloud hands us: one UDP TURN url plus TCP/TLS variants
    ha_cloud_ice_servers = [
        {"urls": "stun:stun.cloudflare.com:3478"},
        {"urls": "turn:turn.cloudflare.com:3478?transport=udp", **cred},
        {"urls": "turn:turn.cloudflare.com:53", **cred},
        {"urls": "turn:turn.cloudflare.com:3478?transport=tcp", **cred},
        {"urls": "turns:turn.cloudflare.com:5349?transport=tcp", **cred},
        {"urls": "turns:turn.cloudflare.com:443?transport=tcp", **cred},
    ]

    gateway = WebRTCGateway(
        http_session=Mock(),
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
        ice_servers=ha_cloud_ice_servers,
    )

    assert [server.url for server in gateway._build_ice_servers(ha_cloud_ice_servers)] == [
        "stun:stun.cloudflare.com:3478",
        "turn:turn.cloudflare.com:3478?transport=udp",
        "turn:turn.cloudflare.com:53",
    ]
    # remote clients still get the unfiltered list, since browsers do support TCP/TLS TURN
    assert gateway.ice_servers == ha_cloud_ice_servers


@pytest.mark.parametrize(
    ("url", "usable"),
    [
        ("stun:stun.example.com:3478", True),
        ("turn:turn.example.com:3478", True),
        ("turn:turn.example.com:3478?transport=udp", True),
        # the transport parameter wins over the scheme, matching rtc::IceServer
        ("turns:turn.example.com:5349?transport=udp", True),
        ("turn:turn.example.com:3478?transport=tcp", False),
        ("turns:turn.example.com:5349", False),
        ("turn:turn.example.com:3478?transport=tls", False),
        ("https://turn.example.com", False),
    ],
)
def test_is_usable_ice_url(url: str, usable: bool) -> None:
    """Test only ICE server urls libjuice can actually use are kept."""
    assert _is_usable_ice_url(url) is usable


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


@pytest.mark.parametrize(
    ("logger_level", "expected_rtc_level"),
    [
        (VERBOSE_LOG_LEVEL, LogLevel.VERBOSE),
        (logging.DEBUG, LogLevel.WARNING),
        (logging.INFO, LogLevel.ERROR),
        (logging.WARNING, LogLevel.ERROR),
    ],
)
async def test_webrtc_gateway_native_log_level(
    cert_pems: tuple[str, str], logger_level: int, expected_rtc_level: LogLevel
) -> None:
    """Test native libdatachannel logging is capped at ERROR unless debugging."""
    cert_pem, key_pem = cert_pems
    gateway = WebRTCGateway(
        http_session=Mock(),
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )
    gateway.logger = logging.getLogger("test_webrtc_native_log_level")
    gateway.logger.setLevel(logger_level)

    with (
        patch.object(gateway, "_run", new_callable=AsyncMock),
        patch(
            "music_assistant.controllers.webserver.remote_access.gateway.install_python_logger"
        ) as install_logger,
    ):
        await gateway.start()
        await gateway.stop()

    install_logger.assert_called_once_with(gateway.logger, level=expected_rtc_level)
    assert not gateway.logger.filters


async def test_webrtc_gateway_drops_benign_turn_warning_at_debug(
    cert_pems: tuple[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """Test the benign Cloudflare CreatePermission warning is dropped at DEBUG level."""
    cert_pem, key_pem = cert_pems
    gateway = WebRTCGateway(
        http_session=Mock(),
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )
    gateway.logger = logging.getLogger("test_webrtc_benign_turn_warning")
    gateway.logger.setLevel(logging.DEBUG)

    with (
        patch.object(gateway, "_run", new_callable=AsyncMock),
        patch("music_assistant.controllers.webserver.remote_access.gateway.install_python_logger"),
    ):
        await gateway.start()
        with caplog.at_level(logging.DEBUG, logger="test_webrtc_benign_turn_warning"):
            gateway.logger.warning(
                "rtc::impl::IceTransport::LogCallback@390: "
                "juice: Got TURN CreatePermission error response, code=0"
            )
            gateway.logger.warning("juice: Lost connectivity")
        await gateway.stop()

    messages = [record.getMessage() for record in caplog.records if "juice" in record.getMessage()]
    assert messages == ["juice: Lost connectivity"]
    # stop() must remove the filter again
    assert not gateway.logger.filters


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

    await gateway._handle_http_proxy_request(
        None, {"id": "1", "method": "GET", "path": malicious_path}
    )

    parsed = urlparse(captured_url["url"])
    assert parsed.hostname == "localhost"
    assert parsed.port == 8095
    assert parsed.username is None
    assert "evil.com" not in (parsed.netloc or "")


async def test_http_proxy_request_keeps_the_unverified_dial_on_this_host(
    cert_pems: tuple[str, str],
) -> None:
    """The proxy must not carry its unverified TLS dial off this host."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    captured_kwargs: dict[str, object] = {}

    def fake_request(_method: str, _url: str, **kwargs: object) -> AsyncMock:
        captured_kwargs.update(kwargs)
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
        local_ws_url="wss://127.0.0.1:8095/ws",
    )

    await gateway._handle_http_proxy_request(None, {"id": "1", "method": "GET", "path": "/info"})

    assert captured_kwargs["ssl"] is False
    assert captured_kwargs["allow_redirects"] is False


async def test_local_websocket_dial_skips_certificate_verification(
    cert_pems: tuple[str, str],
) -> None:
    """Reach the local API on the bind address, which no certificate is issued for."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    captured_kwargs: dict[str, object] = {}

    async def fake_ws_connect(_url: str, **kwargs: object) -> AsyncMock:
        captured_kwargs.update(kwargs)
        return AsyncMock()

    mock_session.ws_connect = fake_ws_connect

    gateway = WebRTCGateway(
        http_session=mock_session,
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
        local_ws_url="wss://127.0.0.1:8095/ws",
    )
    session = WebRTCSession(session_id="s1", pc=Mock())
    channel = MagicMock()
    channel.wait_open = AsyncMock()

    with patch.object(gateway, "_schedule_close"):
        await gateway._bridge_ma_api(session, channel)

    assert captured_kwargs["ssl"] is False


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

    def feed_bytes(self, data: bytes) -> None:
        """Queue a binary message as if the local server sent it."""
        self._incoming.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=data))

    def __aiter__(self) -> AsyncIterator[SimpleNamespace]:
        return self

    async def __anext__(self) -> SimpleNamespace:
        msg = await self._incoming.get()
        if msg is None:
            raise StopAsyncIteration
        return msg


class _FakeBidiChannel:
    """
    Async-iterable data-channel stand-in for bridging tests.

    Drives the gateway's channel->local pump without a real WebRTC handshake: feed
    inbound messages with :meth:`feed`, end the stream with :meth:`close`, and read
    what the gateway sent back on ``sent``.
    """

    def __init__(self, label: str = "ma-api", max_message_size: int = 256 * 1024) -> None:
        self.label = label
        self.is_open = True
        self.is_closed = False
        self.closed = False
        self.max_message_size = max_message_size
        self.sent: list[str | bytes] = []
        self._inbound: asyncio.Queue[str | bytes | None] = asyncio.Queue()

    async def wait_open(self) -> None:
        return

    async def send(self, data: str | bytes) -> None:
        # a real send always suspends, which is what lets concurrent senders interleave
        await asyncio.sleep(0)
        self.sent.append(data)

    def feed(self, message: str | bytes) -> None:
        """Queue an inbound message as if the browser sent it over the channel."""
        self._inbound.put_nowait(message)

    def close(self) -> None:
        self.closed = True
        self.is_closed = True
        self.is_open = False
        self._inbound.put_nowait(None)

    async def aclose(self) -> None:
        self.close()

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self

    async def __anext__(self) -> str | bytes:
        message = await self._inbound.get()
        if message is None:
            raise StopAsyncIteration
        return message


class _FakeHttpSession:
    """ClientSession stand-in handing out one fake WebSocket per dialed url."""

    def __init__(self) -> None:
        self.dialed: list[str] = []
        self.dial_kwargs: list[dict[str, Any]] = []
        self.websockets: dict[str, _FakeLocalWS] = {}
        self.requested: list[str] = []
        self.response_body = b""
        self.bodies: dict[str, bytes] = {}

    async def ws_connect(self, url: str, **kwargs: Any) -> _FakeLocalWS:
        self.dialed.append(url)
        self.dial_kwargs.append(kwargs)
        local_ws = _FakeLocalWS()
        self.websockets[url] = local_ws
        return local_ws

    def request(self, _method: str, url: str, **_kwargs: Any) -> AsyncMock:
        """Serve this url's entry in ``bodies``, falling back to ``response_body``."""
        self.requested.append(url)
        response = AsyncMock()
        response.status = 200
        response.headers = {"Content-Type": "image/jpeg"}
        response.read = AsyncMock(return_value=self.bodies.get(url, self.response_body))
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=response)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx


class _FakePeerConnection:
    """PeerConnection stand-in that runs gateway-spawned pumps as asyncio tasks."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []
        self._incoming: asyncio.Queue[_FakeBidiChannel] = asyncio.Queue()

    def spawn_task(self, coro: Coroutine[Any, Any, None]) -> None:
        self._tasks.append(asyncio.ensure_future(coro))

    def offer_channel(self, channel: _FakeBidiChannel) -> None:
        """Offer a data channel as if the browser had opened it."""
        self._incoming.put_nowait(channel)

    async def incoming_data_channels(self) -> AsyncIterator[DataChannel]:
        while True:
            yield cast("DataChannel", await self._incoming.get())

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        # await cancellation so no pump task lingers past teardown
        await asyncio.gather(*self._tasks, return_exceptions=True)


def _register_bridge_session(
    gateway: WebRTCGateway, session_id: str, channel: _FakeBidiChannel
) -> WebRTCSession:
    """Register a session backed by a fake PeerConnection and ma-api channel."""
    session = WebRTCSession(
        session_id=session_id,
        pc=cast("PeerConnection", _FakePeerConnection()),
        data_channel=cast("DataChannel", channel),
    )
    gateway.sessions[session_id] = session
    return session


def _register_routed_session(
    gateway: WebRTCGateway, session_id: str
) -> tuple[WebRTCSession, _FakePeerConnection]:
    """Register a session that routes the data channels offered to its PeerConnection."""
    pc = _FakePeerConnection()
    session = WebRTCSession(session_id=session_id, pc=cast("PeerConnection", pc))
    gateway.sessions[session_id] = session
    pc.spawn_task(gateway._accept_channels(session))
    return session, pc


async def _wait_for(predicate: Callable[[], bool], timeout: float = 15.0) -> None:
    """Poll ``predicate`` until it is true or the timeout elapses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")


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
    channel = _FakeBidiChannel()
    session = _register_bridge_session(gateway, "bridge-session", channel)
    bridge = asyncio.ensure_future(gateway._bridge_ma_api(session, cast("DataChannel", channel)))
    try:
        await _wait_for(lambda: session.local_ws is not None)

        # browser -> local WebSocket
        channel.feed("from browser")
        await _wait_for(lambda: fake_ws.sent == ["from browser"])

        # local WebSocket -> browser
        fake_ws.feed_text("from local")
        await _wait_for(lambda: channel.sent == ["from local"])
    finally:
        channel.close()
        await asyncio.wait_for(bridge, timeout=5)
        await _wait_for(lambda: "bridge-session" not in gateway.sessions)


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
    channel = _FakeBidiChannel()
    session = _register_bridge_session(gateway, "channel-close-session", channel)
    bridge = asyncio.ensure_future(gateway._bridge_ma_api(session, cast("DataChannel", channel)))
    await _wait_for(lambda: session.local_ws is not None)

    # the browser closes the ma-api channel -> the whole session is torn down
    channel.close()

    await _wait_for(lambda: "channel-close-session" not in gateway.sessions)
    assert "channel-close-session" not in gateway.sessions
    await asyncio.wait_for(bridge, timeout=5)


# ---- channel routing -------------------------------------------------------

LOCAL_WS_URL = "ws://127.0.0.1:8095/ws"
SENDSPIN_URL = "ws://127.0.0.1:8927/sendspin"


def _routing_gateway(
    cert_pems: tuple[str, str],
    http_session: _FakeHttpSession,
    local_ws_url: str = LOCAL_WS_URL,
    set_sendspin_player_callback: Callable[[str, str], None] | None = None,
) -> WebRTCGateway:
    """Create a gateway whose local WebSockets are all served by the fake HTTP session."""
    cert_pem, key_pem = cert_pems
    return WebRTCGateway(
        http_session=cast("aiohttp.ClientSession", http_session),
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
        local_ws_url=local_ws_url,
        sendspin_url=SENDSPIN_URL,
        set_sendspin_player_callback=set_sendspin_player_callback,
    )


async def test_sendspin_channel_bridges_to_the_sendspin_server(cert_pems: tuple[str, str]) -> None:
    """A sendspin channel reaches the internal sendspin server, web player id and all."""
    http_session = _FakeHttpSession()
    announced_players: list[tuple[str, str]] = []
    gateway = _routing_gateway(
        cert_pems,
        http_session,
        set_sendspin_player_callback=lambda session_id, player_id: announced_players.append(
            (session_id, player_id)
        ),
    )
    session, pc = _register_routed_session(gateway, "sendspin-session")
    channel = _FakeBidiChannel(label="sendspin")
    pc.offer_channel(channel)
    try:
        await _wait_for(lambda: SENDSPIN_URL in http_session.websockets)
        local_ws = http_session.websockets[SENDSPIN_URL]

        # the first message announces the web player, and is forwarded verbatim
        auth = json.dumps({"type": "auth", "token": "t", "client_id": "web-player-1"})
        channel.feed(auth)
        await _wait_for(lambda: local_ws.sent == [auth])
        assert announced_players == [("sendspin-session", "web-player-1")]
        assert session.sendspin_player_id == "web-player-1"

        # audio keeps flowing in both directions, text and binary alike
        channel.feed(b"\x01\x02")
        await _wait_for(lambda: local_ws.sent == [auth, b"\x01\x02"])
        local_ws.feed_text('{"type":"hello"}')
        local_ws.feed_bytes(b"\x03\x04")
        await _wait_for(lambda: channel.sent == ['{"type":"hello"}', b"\x03\x04"])
    finally:
        await gateway._close_session("sendspin-session")


@pytest.mark.parametrize(
    ("local_ws_url", "expected_url"),
    [
        ("ws://127.0.0.1:8095/ws", "ws://127.0.0.1:8095/live_announcement"),
        # an https webserver is still dialed on its bind address, which no cert covers
        ("wss://127.0.0.1:8095/ws", "wss://127.0.0.1:8095/live_announcement"),
    ],
)
async def test_live_announcement_channel_bridges_to_the_webserver(
    cert_pems: tuple[str, str], local_ws_url: str, expected_url: str
) -> None:
    """A live announcement channel reaches the webserver route that takes the audio."""
    http_session = _FakeHttpSession()
    gateway = _routing_gateway(cert_pems, http_session, local_ws_url=local_ws_url)
    session, pc = _register_routed_session(gateway, "announce-session")
    channel = _FakeBidiChannel(label="live_announcement")
    pc.offer_channel(channel)
    try:
        await _wait_for(lambda: expected_url in http_session.websockets)
        local_ws = http_session.websockets[expected_url]
        assert http_session.dial_kwargs == [{"ssl": False}]

        # the client authenticates on the route itself, so its handshake passes through
        handshake = [
            json.dumps({"type": "auth", "token": "t"}),
            json.dumps({"type": "start", "player_id": "player1", "sample_rate": 16000}),
        ]
        for message in handshake:
            channel.feed(message)
        await _wait_for(lambda: local_ws.sent == handshake)
        # the sendspin snoop belongs to the sendspin bridge only
        assert session.sendspin_player_id is None

        # spoken audio goes up, the route's replies come back
        channel.feed(b"\x00\x01")
        await _wait_for(lambda: local_ws.sent == [*handshake, b"\x00\x01"])
        local_ws.feed_text('{"type":"started"}')
        await _wait_for(lambda: channel.sent == ['{"type":"started"}'])
    finally:
        await gateway._close_session("announce-session")


@pytest.mark.parametrize("closed_by", ["browser", "local"])
async def test_closing_a_bridged_channel_leaves_the_api_session_up(
    cert_pems: tuple[str, str], closed_by: str
) -> None:
    """Losing a bridged WebSocket tears down that bridge only, never the API session."""
    http_session = _FakeHttpSession()
    gateway = _routing_gateway(cert_pems, http_session)
    session, pc = _register_routed_session(gateway, "mixed-session")
    api_channel = _FakeBidiChannel()
    sendspin_channel = _FakeBidiChannel(label="sendspin")
    pc.offer_channel(api_channel)
    pc.offer_channel(sendspin_channel)
    try:
        await _wait_for(
            lambda: session.local_ws is not None and SENDSPIN_URL in http_session.dialed
        )
        sendspin_ws = http_session.websockets[SENDSPIN_URL]

        if closed_by == "browser":
            sendspin_channel.close()
        else:
            await sendspin_ws.close()
        await _wait_for(lambda: not session.channels)

        assert sendspin_ws.closed is True
        assert sendspin_channel.closed is True
        # the API session is untouched: still registered, still bridged, channel still open
        assert "mixed-session" in gateway.sessions
        assert session.local_ws is not None
        assert cast("_FakeLocalWS", session.local_ws).closed is False
        assert api_channel.closed is False
    finally:
        await gateway._close_session("mixed-session")


async def test_the_first_channel_is_the_api_channel_whatever_its_label(
    cert_pems: tuple[str, str],
) -> None:
    """Clients may label their API channel freely, so the first channel bridges to the API."""
    http_session = _FakeHttpSession()
    gateway = _routing_gateway(cert_pems, http_session)
    session, pc = _register_routed_session(gateway, "labelled-session")
    channel = _FakeBidiChannel(label="ma-api-v2")
    pc.offer_channel(channel)
    try:
        await _wait_for(lambda: session.local_ws is not None)
        assert session.data_channel is cast("DataChannel", channel)
        assert http_session.dialed == [f"{LOCAL_WS_URL}?webrtc_session_id=labelled-session"]
    finally:
        await gateway._close_session("labelled-session")


async def test_unknown_channel_label_cannot_replace_the_api_channel(
    cert_pems: tuple[str, str],
) -> None:
    """A channel this server has no route for is refused instead of hijacking the session."""
    http_session = _FakeHttpSession()
    gateway = _routing_gateway(cert_pems, http_session)
    session, pc = _register_routed_session(gateway, "unknown-session")
    api_channel = _FakeBidiChannel()
    unknown_channel = _FakeBidiChannel(label="channel-from-the-future")
    pc.offer_channel(api_channel)
    try:
        await _wait_for(lambda: session.local_ws is not None)
        pc.offer_channel(unknown_channel)
        await _wait_for(lambda: unknown_channel.closed)

        assert session.data_channel is cast("DataChannel", api_channel)
        assert "unknown-session" in gateway.sessions
        # only the API channel was ever bridged
        assert http_session.dialed == [f"{LOCAL_WS_URL}?webrtc_session_id=unknown-session"]
    finally:
        await gateway._close_session("unknown-session")


def _proxy_request(request_id: str, path: str) -> str:
    """Build the http-proxy-request message a client sends for a proxied path."""
    return json.dumps(
        {"type": "http-proxy-request", "id": request_id, "method": "GET", "path": path}
    )


def _body_delivered(sent: list[str | bytes], size: int) -> bool:
    """Return whether the binary frames sent so far add up to a whole body of ``size``."""
    return sum(len(m) for m in sent if isinstance(m, bytes)) >= size


def _read_proxy_response(sent: list[str | bytes]) -> tuple[dict[str, Any], bytes]:
    """
    Read the binary-framed response a client would reassemble from the proxy channel.

    :param sent: Messages the gateway sent, starting at the response's JSON header.
    """
    header = json.loads(cast("str", sent[0]))
    body = b""
    for message in sent[1:]:
        assert isinstance(message, bytes), f"expected a binary body frame, got {message!r}"
        body += message
        if len(body) >= header["size"]:
            break
    assert len(body) == header["size"]
    return header, body


async def test_http_proxy_channel_answers_on_its_own_channel(cert_pems: tuple[str, str]) -> None:
    """A proxied request on the http proxy channel is answered there, not on the API channel."""
    http_session = _FakeHttpSession()
    http_session.response_body = b"\xff\xd8jpeg-bytes"
    gateway = _routing_gateway(cert_pems, http_session)
    session, pc = _register_routed_session(gateway, "proxy-session")
    api_channel = _FakeBidiChannel()
    proxy_channel = _FakeBidiChannel(label="http_proxy")
    pc.offer_channel(api_channel)
    pc.offer_channel(proxy_channel)
    try:
        await _wait_for(lambda: session.local_ws is not None)

        proxy_channel.feed(_proxy_request("img-1", "/imageproxy/abc"))
        await _wait_for(lambda: len(proxy_channel.sent) >= 2)

        assert http_session.requested == ["http://127.0.0.1:8095/imageproxy/abc"]
        response, body = _read_proxy_response(proxy_channel.sent)
        assert response["type"] == "http-proxy-response"
        assert response["id"] == "img-1"
        assert response["status"] == 200
        # the body rides as raw binary, so it costs its own size on the wire and no more
        assert body == b"\xff\xd8jpeg-bytes"
        assert "body" not in response
        # the image never touches the API channel, nor the local API WebSocket
        assert api_channel.sent == []
        assert cast("_FakeLocalWS", session.local_ws).sent == []
    finally:
        await gateway._close_session("proxy-session")


async def test_http_proxy_request_on_the_api_channel_is_still_answered(
    cert_pems: tuple[str, str],
) -> None:
    """Clients that predate the http proxy channel keep proxying over the API channel."""
    http_session = _FakeHttpSession()
    http_session.response_body = b"legacy-body"
    gateway = _routing_gateway(cert_pems, http_session)
    session, pc = _register_routed_session(gateway, "legacy-session")
    api_channel = _FakeBidiChannel()
    pc.offer_channel(api_channel)
    try:
        await _wait_for(lambda: session.local_ws is not None)

        api_channel.feed(_proxy_request("img-2", "/imageproxy/def"))
        await _wait_for(lambda: bool(api_channel.sent))

        assert http_session.requested == ["http://127.0.0.1:8095/imageproxy/def"]
        response = json.loads(cast("str", api_channel.sent[0]))
        assert response["type"] == "http-proxy-response"
        assert response["id"] == "img-2"
        assert bytes.fromhex(response["body"]) == b"legacy-body"
        # the proxy request is served here, never forwarded to the local API WebSocket
        assert cast("_FakeLocalWS", session.local_ws).sent == []
    finally:
        await gateway._close_session("legacy-session")


async def test_http_proxy_channel_reports_a_failed_fetch_on_its_own_channel(
    cert_pems: tuple[str, str],
) -> None:
    """A fetch that raises still answers the client, on the channel it asked over."""
    http_session = _FakeHttpSession()
    http_session.request = Mock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    gateway = _routing_gateway(cert_pems, http_session)
    session, pc = _register_routed_session(gateway, "proxy-error-session")
    api_channel = _FakeBidiChannel()
    proxy_channel = _FakeBidiChannel(label="http_proxy")
    pc.offer_channel(api_channel)
    pc.offer_channel(proxy_channel)
    try:
        await _wait_for(lambda: session.local_ws is not None)

        proxy_channel.feed(_proxy_request("img-3", "/imageproxy/boom"))
        await _wait_for(lambda: len(proxy_channel.sent) >= 2)

        response, body = _read_proxy_response(proxy_channel.sent)
        assert response["id"] == "img-3"
        assert response["status"] == 500
        assert b"boom" in body
        assert api_channel.sent == []
    finally:
        await gateway._close_session("proxy-error-session")


async def test_http_proxy_channel_splits_a_large_body_into_binary_frames(
    cert_pems: tuple[str, str],
) -> None:
    """A body past the channel's message limit arrives as raw frames that concatenate back."""
    http_session = _FakeHttpSession()
    http_session.response_body = bytes(range(256)) * 2048  # 512 KiB
    gateway = _routing_gateway(cert_pems, http_session)
    session, pc = _register_routed_session(gateway, "proxy-large-session")
    proxy_channel = _FakeBidiChannel(label="http_proxy")
    pc.offer_channel(proxy_channel)
    try:
        await _wait_for(lambda: "http_proxy" in session.channels)

        proxy_channel.feed(_proxy_request("img-5", "/imageproxy/large"))
        await _wait_for(
            lambda: _body_delivered(proxy_channel.sent, len(http_session.response_body))
        )

        response, body = _read_proxy_response(proxy_channel.sent)
        assert response["size"] == len(http_session.response_body)
        assert body == http_session.response_body
        frames = proxy_channel.sent[1:]
        assert all(len(frame) <= proxy_channel.max_message_size for frame in frames)
        # the wire cost is the body itself, not the ~2.7x a hex-in-base64 response took
        assert sum(len(frame) for frame in frames) == len(http_session.response_body)
    finally:
        await gateway._close_session("proxy-large-session")


async def test_http_proxy_channel_honours_the_negotiated_message_limit(
    cert_pems: tuple[str, str],
) -> None:
    """A peer that advertises a small limit gets frames it can actually accept."""
    http_session = _FakeHttpSession()
    http_session.response_body = b"x" * (200 * 1024)
    gateway = _routing_gateway(cert_pems, http_session)
    session, pc = _register_routed_session(gateway, "proxy-small-frames-session")
    # what a peer that advertises no a=max-message-size in its SDP is assumed to accept
    proxy_channel = _FakeBidiChannel(label="http_proxy", max_message_size=64 * 1024)
    pc.offer_channel(proxy_channel)
    try:
        await _wait_for(lambda: "http_proxy" in session.channels)

        proxy_channel.feed(_proxy_request("img-6", "/imageproxy/small-frames"))
        await _wait_for(
            lambda: _body_delivered(proxy_channel.sent, len(http_session.response_body))
        )

        _, body = _read_proxy_response(proxy_channel.sent)
        assert body == http_session.response_body
        assert all(len(frame) <= 64 * 1024 for frame in proxy_channel.sent[1:])
    finally:
        await gateway._close_session("proxy-small-frames-session")


async def test_http_proxy_channel_never_interleaves_two_responses(
    cert_pems: tuple[str, str],
) -> None:
    """Body frames carry no request id, so each response must reach the client in one run."""
    http_session = _FakeHttpSession()
    http_session.bodies = {
        "http://127.0.0.1:8095/imageproxy/one": b"1" * (300 * 1024),
        "http://127.0.0.1:8095/imageproxy/two": b"2" * (300 * 1024),
    }
    gateway = _routing_gateway(cert_pems, http_session)
    session, pc = _register_routed_session(gateway, "proxy-concurrent-session")
    proxy_channel = _FakeBidiChannel(label="http_proxy")
    pc.offer_channel(proxy_channel)
    try:
        await _wait_for(lambda: "http_proxy" in session.channels)

        proxy_channel.feed(_proxy_request("img-one", "/imageproxy/one"))
        proxy_channel.feed(_proxy_request("img-two", "/imageproxy/two"))
        await _wait_for(lambda: sum(isinstance(m, str) for m in proxy_channel.sent) == 2)
        await _wait_for(lambda: len(proxy_channel.sent) >= 6)

        # split at the second header: each response owns an unbroken run of body frames
        second = next(i for i, m in enumerate(proxy_channel.sent) if i and isinstance(m, str))
        first_header, first_body = _read_proxy_response(proxy_channel.sent[:second])
        second_header, second_body = _read_proxy_response(proxy_channel.sent[second:])
        assert {first_header["id"], second_header["id"]} == {"img-one", "img-two"}
        # each body is one repeated byte, so any interleaving shows up as a mixed run
        filler = {"img-one": ord("1"), "img-two": ord("2")}
        assert set(first_body) == {filler[first_header["id"]]}
        assert set(second_body) == {filler[second_header["id"]]}
        assert len(first_body) == len(second_body) == 300 * 1024
    finally:
        await gateway._close_session("proxy-concurrent-session")


@pytest.mark.parametrize(
    "junk",
    [
        "not json at all",
        json.dumps(["not", "a", "dict"]),
        json.dumps({"type": "something-else"}),
        b"\x00\x01binary",
    ],
)
async def test_http_proxy_channel_survives_junk(
    cert_pems: tuple[str, str], junk: str | bytes
) -> None:
    """Anything that is not a proxy request is ignored without killing the channel."""
    http_session = _FakeHttpSession()
    http_session.response_body = b"still-here"
    gateway = _routing_gateway(cert_pems, http_session)
    session, pc = _register_routed_session(gateway, "proxy-junk-session")
    proxy_channel = _FakeBidiChannel(label="http_proxy")
    pc.offer_channel(proxy_channel)
    try:
        await _wait_for(lambda: "http_proxy" in session.channels)

        proxy_channel.feed(junk)
        proxy_channel.feed(_proxy_request("img-4", "/imageproxy/ok"))
        await _wait_for(lambda: len(proxy_channel.sent) >= 2)

        response, body = _read_proxy_response(proxy_channel.sent)
        assert response["id"] == "img-4"
        assert body == b"still-here"
    finally:
        await gateway._close_session("proxy-junk-session")


async def test_closing_the_http_proxy_channel_leaves_the_api_session_up(
    cert_pems: tuple[str, str],
) -> None:
    """Losing the http proxy channel tears down that channel only, never the API session."""
    http_session = _FakeHttpSession()
    gateway = _routing_gateway(cert_pems, http_session)
    session, pc = _register_routed_session(gateway, "proxy-close-session")
    api_channel = _FakeBidiChannel()
    proxy_channel = _FakeBidiChannel(label="http_proxy")
    pc.offer_channel(api_channel)
    pc.offer_channel(proxy_channel)
    try:
        await _wait_for(lambda: "http_proxy" in session.channels)

        proxy_channel.close()
        await _wait_for(lambda: not session.channels)

        assert "proxy-close-session" in gateway.sessions
        assert session.local_ws is not None
        assert api_channel.closed is False
    finally:
        await gateway._close_session("proxy-close-session")


async def test_a_second_http_proxy_channel_is_refused(cert_pems: tuple[str, str]) -> None:
    """A duplicate label is refused so the running handler is never left untracked."""
    http_session = _FakeHttpSession()
    gateway = _routing_gateway(cert_pems, http_session)
    session, pc = _register_routed_session(gateway, "duplicate-session")
    first = _FakeBidiChannel(label="http_proxy")
    second = _FakeBidiChannel(label="http_proxy")
    pc.offer_channel(first)
    try:
        await _wait_for(lambda: "http_proxy" in session.channels)
        pc.offer_channel(second)
        await _wait_for(lambda: second.closed)

        assert first.closed is False
        assert session.channels["http_proxy"].channel is cast("DataChannel", first)
    finally:
        await gateway._close_session("duplicate-session")


class _StallingProxyChannel(_FakeBidiChannel):
    """
    Proxy-channel stand-in whose sends park until the test lets them through.

    Stands in for a client that stopped draining what it asked for: a real ``send`` then
    waits on the channel's drain event, which is what parks the gateway's write-back.

    :param stall_after: Let this many messages through before parking, matching a real
        channel that only waits once its buffer holds something.
    """

    def __init__(self, stall_after: int = 0) -> None:
        super().__init__(label="http_proxy")
        self.drained = asyncio.Event()
        self.abandoned = 0
        self._stall_after = stall_after

    async def send(self, data: str | bytes) -> None:
        if len(self.sent) >= self._stall_after:
            try:
                await self.drained.wait()
            except asyncio.CancelledError:
                self.abandoned += 1
                raise
        await super().send(data)


async def test_a_client_that_stops_draining_does_not_hold_the_proxy_channel(
    cert_pems: tuple[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """A response the client never takes is abandoned instead of parking the send lock."""
    gateway = _proxy_gateway(cert_pems)
    gateway.logger = logging.getLogger("test_webrtc_stalled_send")
    # the header goes out and the body frame is what parks, as on a channel that only waits
    # once its buffer holds something
    channel = _StallingProxyChannel(stall_after=1)
    send_lock = asyncio.Lock()

    with (
        patch(f"{_GATEWAY_MODULE}.HTTP_PROXY_SEND_TIMEOUT", 0.05),
        caplog.at_level(logging.WARNING, logger="test_webrtc_stalled_send"),
    ):
        await asyncio.wait_for(
            gateway._send_http_proxy_response(
                cast("DataChannel", channel), "img-stalled", 200, {}, b"art-bytes", send_lock
            ),
            timeout=5,
        )

    # the reply ends short of its announced size rather than half-way into a frame
    assert json.loads(cast("str", channel.sent[0]))["size"] == len(b"art-bytes")
    assert channel.sent[1:] == []
    assert not send_lock.locked()
    assert "Timeout sending proxy response img-stalled" in caplog.text


async def test_a_stalled_response_does_not_block_the_next_one(cert_pems: tuple[str, str]) -> None:
    """One wedged response must not stop the rest of the art for the life of the session."""
    http_session = _FakeHttpSession()
    http_session.response_body = b"\xff\xd8second-image"
    gateway = _routing_gateway(cert_pems, http_session)
    session, pc = _register_routed_session(gateway, "proxy-stalled-session")
    proxy_channel = _StallingProxyChannel(stall_after=1)
    pc.offer_channel(proxy_channel)
    try:
        await _wait_for(lambda: "http_proxy" in session.channels)

        with patch(f"{_GATEWAY_MODULE}.HTTP_PROXY_SEND_TIMEOUT", 0.05):
            proxy_channel.feed(_proxy_request("img-stalls", "/imageproxy/stalls"))
            await _wait_for(lambda: proxy_channel.abandoned >= 1)

        proxy_channel.drained.set()
        proxy_channel.feed(_proxy_request("img-next", "/imageproxy/next"))
        await _wait_for(lambda: len(proxy_channel.sent) >= 3)

        # the abandoned reply is the header alone; the next one follows it whole
        assert json.loads(cast("str", proxy_channel.sent[0]))["id"] == "img-stalls"
        response, body = _read_proxy_response(proxy_channel.sent[1:])
        assert response["id"] == "img-next"
        assert body == b"\xff\xd8second-image"
    finally:
        await gateway._close_session("proxy-stalled-session")


async def test_a_stalled_response_frees_its_slot_for_other_requests(
    cert_pems: tuple[str, str],
) -> None:
    """Wedged write-backs must not leave the gateway-wide proxy budget exhausted."""
    http_session = _FakeHttpSession()
    http_session.response_body = b"art-bytes"
    gateway = _routing_gateway(cert_pems, http_session)
    channel = _StallingProxyChannel()
    send_lock = asyncio.Lock()

    with patch(f"{_GATEWAY_MODULE}.HTTP_PROXY_SEND_TIMEOUT", 0.05):
        # take the whole budget, so a single slot left behind is enough to fail this
        requests = [
            asyncio.ensure_future(
                gateway._handle_http_proxy_request(
                    cast("DataChannel", channel),
                    {"id": f"img-{index}", "method": "GET", "path": f"/imageproxy/{index}"},
                    send_lock,
                )
            )
            for index in range(HTTP_PROXY_CONCURRENCY)
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*requests), timeout=15)
        finally:
            for request in requests:
                request.cancel()

    for _ in range(HTTP_PROXY_CONCURRENCY):
        await asyncio.wait_for(gateway._http_proxy_semaphore.acquire(), timeout=5)


async def test_http_proxy_fetch_carries_an_explicit_timeout(cert_pems: tuple[str, str]) -> None:
    """The proxied fetch must be bounded rather than left on aiohttp's five minute default."""
    cert_pem, key_pem = cert_pems
    mock_session = Mock()
    captured_kwargs: dict[str, Any] = {}

    def fake_request(_method: str, _url: str, **kwargs: Any) -> AsyncMock:
        captured_kwargs.update(kwargs)
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
    )

    await gateway._handle_http_proxy_request(None, {"id": "1", "method": "GET", "path": "/info"})

    timeout = cast("aiohttp.ClientTimeout", captured_kwargs["timeout"])
    assert timeout.total is not None
    assert timeout.total < 300


async def test_a_timed_out_fetch_answers_the_client(cert_pems: tuple[str, str]) -> None:
    """A fetch that runs out of time still answers, so the client is not left waiting."""

    def timing_out_request(_method: str, _url: str, **_kwargs: Any) -> AsyncMock:
        # aiohttp hands out its context manager first and only raises once the body is read
        response = AsyncMock()
        response.status = 200
        response.headers = {}
        response.read = AsyncMock(side_effect=TimeoutError)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=response)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    http_session = _FakeHttpSession()
    http_session.request = timing_out_request  # type: ignore[assignment]
    gateway = _routing_gateway(cert_pems, http_session)
    session, pc = _register_routed_session(gateway, "proxy-timeout-session")
    proxy_channel = _FakeBidiChannel(label="http_proxy")
    pc.offer_channel(proxy_channel)
    try:
        await _wait_for(lambda: "http_proxy" in session.channels)

        proxy_channel.feed(_proxy_request("img-slow", "/imageproxy/slow"))
        await _wait_for(lambda: len(proxy_channel.sent) >= 2)

        response, body = _read_proxy_response(proxy_channel.sent)
        assert response["id"] == "img-slow"
        assert response["status"] == 504
        assert body == b"Gateway Timeout"
    finally:
        await gateway._close_session("proxy-timeout-session")


class _FakeDataChannel:
    """
    Data channel stand-in that captures outbound messages for proxy tests.

    :param close_after: Close the channel once this many messages have been sent.
    """

    def __init__(self, max_message_size: int = 256 * 1024, close_after: int | None = None) -> None:
        self.is_closed = False
        self.max_message_size = max_message_size
        self.sent: list[str] = []
        # the gateway consults the channel once per outbound message, so this counts how far
        # a send loop got even when the messages themselves are discarded
        self.open_checks = 0
        self._is_open = True
        self._close_after = close_after

    @property
    def is_open(self) -> bool:
        self.open_checks += 1
        return self._is_open

    async def send(self, data: str) -> None:
        self.sent.append(data)
        if self._close_after is not None and len(self.sent) >= self._close_after:
            self._is_open = False
            self.is_closed = True


def _proxy_gateway(cert_pems: tuple[str, str]) -> WebRTCGateway:
    cert_pem, key_pem = cert_pems
    return WebRTCGateway(
        http_session=Mock(),
        remote_id="TEST-REMOTE-ID",
        cert_pem=cert_pem,
        key_pem=key_pem,
    )


def _reassemble_chunks(frames: list[str]) -> str:
    """Reassemble __chunk__ frames back into the original text (base64 -> bytes -> utf-8)."""
    parsed = [json.loads(f) for f in frames]
    assert all(f["type"] == "__chunk__" for f in parsed)
    assert len({f["id"] for f in parsed}) == 1
    count = parsed[0]["count"]
    parts: list[bytes] = [b""] * count
    for frame in parsed:
        parts[frame["seq"]] = base64.b64decode(frame["b64"])
    return b"".join(parts).decode()


async def test_http_proxy_response_small_body_single_message(
    cert_pems: tuple[str, str],
) -> None:
    """A body within the chunk size is sent as one http-proxy-response message."""
    gateway = _proxy_gateway(cert_pems)
    channel = _FakeDataChannel()
    body = b"\x00\x01\x02small-body"

    await gateway._send_http_proxy_response(
        cast("DataChannel", channel), "req-small", 200, {"X-Test": "y"}, body
    )

    assert len(channel.sent) == 1
    msg = json.loads(channel.sent[0])
    assert msg["type"] == "http-proxy-response"
    assert msg["id"] == "req-small"
    assert msg["status"] == 200
    assert msg["headers"] == {"X-Test": "y"}
    assert bytes.fromhex(msg["body"]) == body


async def test_http_proxy_response_large_body_chunked(cert_pems: tuple[str, str]) -> None:
    """A large HTTP-proxy response is split into base64 chunk frames the client reassembles."""
    gateway = _proxy_gateway(cert_pems)
    channel = _FakeDataChannel()
    # big body -> big JSON message
    body = bytes(range(256)) * ((DATA_CHANNEL_CHUNK_SIZE * 5) // 512)

    await gateway._send_http_proxy_response(cast("DataChannel", channel), "req-big", 200, {}, body)

    assert len(channel.sent) > 1
    assert all(json.loads(m)["type"] == "__chunk__" for m in channel.sent)
    # every serialized frame must stay under the negotiated 256 KiB data-channel limit
    assert all(len(m.encode()) < 256 * 1024 for m in channel.sent)

    reassembled = json.loads(_reassemble_chunks(channel.sent))
    assert reassembled["type"] == "http-proxy-response"
    assert reassembled["id"] == "req-big"
    assert reassembled["status"] == 200
    assert bytes.fromhex(reassembled["body"]) == body


async def test_send_chunked_small_message_passthrough(cert_pems: tuple[str, str]) -> None:
    """A message within the limit is sent verbatim, not chunked."""
    gateway = _proxy_gateway(cert_pems)
    channel = _FakeDataChannel()
    await gateway._send_chunked(cast("DataChannel", channel), '{"event":"player_updated"}')
    assert channel.sent == ['{"event":"player_updated"}']


async def test_send_chunked_large_message_chunked(cert_pems: tuple[str, str]) -> None:
    """A large message is chunked and reassembles byte-identically (multibyte-safe)."""
    gateway = _proxy_gateway(cert_pems)
    channel = _FakeDataChannel()
    # multibyte payload so chunk boundaries fall mid-character, exercising the byte-level split
    text = '{"data":"' + "音楽" * DATA_CHANNEL_CHUNK_SIZE + '"}'

    await gateway._send_chunked(cast("DataChannel", channel), text)

    assert len(channel.sent) > 1
    assert all(len(m.encode()) < 256 * 1024 for m in channel.sent)
    assert _reassemble_chunks(channel.sent) == text


async def test_send_chunked_keeps_the_framing_released_clients_expect(
    cert_pems: tuple[str, str],
) -> None:
    """Against the 256 KiB limit every browser advertises, the frames must not move."""
    gateway = _proxy_gateway(cert_pems)
    channel = _FakeDataChannel()
    text = '{"event":"' + "x" * (DATA_CHANNEL_CHUNK_SIZE * 2 + 5000) + '"}'
    data = text.encode()

    await gateway._send_chunked(cast("DataChannel", channel), text)

    # the exact wire format the bundled frontend and the mobile app reassemble: 64 KiB pieces,
    # numbered from zero within a group id that counts up per message
    assert channel.sent == [
        json.dumps(
            {
                "type": "__chunk__",
                "id": 1,
                "seq": seq,
                "count": 3,
                "b64": base64.b64encode(
                    data[seq * DATA_CHANNEL_CHUNK_SIZE : (seq + 1) * DATA_CHANNEL_CHUNK_SIZE]
                ).decode(),
            }
        )
        for seq in range(3)
    ]
    # a full piece has always serialised well past 64 KiB, which only a peer advertising no
    # limit of its own would reject
    assert len(channel.sent[0].encode()) == 87447


async def test_send_chunked_honours_the_negotiated_message_limit(
    cert_pems: tuple[str, str],
) -> None:
    """A peer that advertises no limit in its SDP gets frames it can actually accept."""
    gateway = _proxy_gateway(cert_pems)
    # what libdatachannel assumes when the peer advertises no a=max-message-size
    channel = _FakeDataChannel(max_message_size=64 * 1024)
    text = '{"data":"' + "音楽" * DATA_CHANNEL_CHUNK_SIZE + '"}'

    await gateway._send_chunked(cast("DataChannel", channel), text)

    assert len(channel.sent) > 1
    assert all(len(m.encode()) <= channel.max_message_size for m in channel.sent)
    assert _reassemble_chunks(channel.sent) == text


async def test_send_chunked_passthrough_stops_at_the_negotiated_limit(
    cert_pems: tuple[str, str],
) -> None:
    """A message past a small peer's limit is chunked rather than sent whole."""
    gateway = _proxy_gateway(cert_pems)
    channel = _FakeDataChannel(max_message_size=16 * 1024)
    text = '{"data":"' + "x" * (32 * 1024) + '"}'

    await gateway._send_chunked(cast("DataChannel", channel), text)

    assert len(channel.sent) > 1
    assert all(len(m.encode()) <= channel.max_message_size for m in channel.sent)
    assert _reassemble_chunks(channel.sent) == text


async def test_send_chunked_stops_framing_once_the_channel_closes(
    cert_pems: tuple[str, str],
) -> None:
    """A channel that goes away mid-message stops the loop instead of encoding the rest."""
    gateway = _proxy_gateway(cert_pems)
    channel = _FakeDataChannel(close_after=1)
    text = '{"data":"' + "x" * (DATA_CHANNEL_CHUNK_SIZE * 10) + '"}'

    await gateway._send_chunked(cast("DataChannel", channel), text)

    assert len(channel.sent) == 1
    # the guard and the send of the first piece, then the guard again: the other ten pieces
    # are never framed
    assert channel.open_checks == 3


async def test_dropped_message_is_logged_with_its_size_on_the_wire(
    cert_pems: tuple[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """The size in the drop warning counts bytes, so it can be read against the channel limit."""
    gateway = _proxy_gateway(cert_pems)
    gateway.logger = logging.getLogger("test_webrtc_dropped_message_size")
    channel = Mock()
    channel.is_open = True
    channel.send = AsyncMock(side_effect=RTCError("message too large"))
    # 100 characters, 300 bytes once encoded
    text = "音" * 100

    with caplog.at_level(logging.WARNING, logger="test_webrtc_dropped_message_size"):
        await gateway._send_on_channel(cast("DataChannel", channel), text)

    assert "Dropping 300-byte data channel message" in caplog.text


async def test_ma_api_channel_chunks_within_the_negotiated_limit(
    cert_pems: tuple[str, str],
) -> None:
    """A large API event reaches a peer that advertises no limit instead of being dropped."""
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
    # what libdatachannel assumes when the peer advertises no a=max-message-size
    channel = _FakeBidiChannel(max_message_size=64 * 1024)
    session = _register_bridge_session(gateway, "small-limit-session", channel)
    bridge = asyncio.ensure_future(gateway._bridge_ma_api(session, cast("DataChannel", channel)))
    try:
        await _wait_for(lambda: session.local_ws is not None)

        event = json.dumps({"event": "queue_updated", "data": "x" * (200 * 1024)})
        fake_ws.feed_text(event)
        await _wait_for(
            lambda: bool(channel.sent) and len(channel.sent) == json.loads(channel.sent[0])["count"]
        )

        frames = cast("list[str]", channel.sent)
        assert all(len(frame.encode()) <= channel.max_message_size for frame in frames)
        assert _reassemble_chunks(frames) == event
    finally:
        channel.close()
        await asyncio.wait_for(bridge, timeout=5)
        await _wait_for(lambda: "small-limit-session" not in gateway.sessions)
