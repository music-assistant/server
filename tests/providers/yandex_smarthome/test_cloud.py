"""Tests for provider/cloud.py — CloudManager WebSocket and registration helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from ya_dialogs_api import SecretStr

from music_assistant.providers.yandex_smarthome.cloud import (
    CloudManager,
    get_cloud_otp,
    register_cloud_instance,
)
from music_assistant.providers.yandex_smarthome.constants import CLOUD_RECONNECT_MIN
from music_assistant.providers.yandex_smarthome.schema import CloudRequest

# ---------------------------------------------------------------------------
# CloudManager tests
# ---------------------------------------------------------------------------


class TestCloudManager:
    """Tests for CloudManager WebSocket client."""

    def _make_manager(self, on_request: AsyncMock | None = None) -> CloudManager:
        """Create make manager helper."""
        session = MagicMock(spec=aiohttp.ClientSession)
        if on_request is None:
            on_request = AsyncMock(return_value={"request_id": "r1", "payload": {}})
        return CloudManager(
            session=session,
            connection_token=SecretStr("test-token"),
            on_request=on_request,
        )

    def test_initial_state(self) -> None:
        """Test initial state."""
        mgr = self._make_manager()
        assert mgr.connected is False
        assert mgr._running is False

    def test_connected_property(self) -> None:
        """Test connected property."""
        mgr = self._make_manager()
        assert mgr.connected is False

        # Simulate connected WS
        ws = MagicMock()
        ws.closed = False
        mgr._ws = ws
        assert mgr.connected is True

        # Simulate closed WS
        ws.closed = True  # type: ignore[unreachable]
        assert mgr.connected is False

    @pytest.mark.asyncio
    async def test_handle_message_calls_callback(self) -> None:
        """Test handle message calls callback."""
        callback = AsyncMock(return_value={"request_id": "r1", "payload": {}})
        mgr = self._make_manager(on_request=callback)

        ws = AsyncMock()
        data = {"request_id": "r1", "action": "/v1.0/user/devices", "message": {}}
        await mgr._handle_message(ws, data)

        callback.assert_awaited_once()
        args = callback.call_args[0][0]
        assert isinstance(args, CloudRequest)
        assert args.request_id == "r1"
        assert args.action == "/v1.0/user/devices"
        ws.send_json.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_message_exception_logged(self) -> None:
        """Test handle message exception sends error response."""
        callback = AsyncMock(side_effect=RuntimeError("boom"))
        mgr = self._make_manager(on_request=callback)

        ws = AsyncMock()
        ws.closed = False
        # Should not raise
        await mgr._handle_message(ws, {"request_id": "r1", "action": "test"})
        # Should send error response so relay doesn't hang
        ws.send_json.assert_awaited_once_with(
            {"request_id": "r1", "payload": {"error": "INTERNAL_ERROR"}}
        )

    @pytest.mark.asyncio
    async def test_disconnect(self) -> None:
        """Test disconnect."""
        mgr = self._make_manager()
        mgr._running = True
        ws = AsyncMock()
        ws.closed = False
        mgr._ws = ws

        await mgr.disconnect()

        assert mgr._running is False
        ws.close.assert_awaited_once()
        assert mgr._ws is None

    @pytest.mark.asyncio
    async def test_disconnect_when_already_closed(self) -> None:
        """Test disconnect when already closed."""
        mgr = self._make_manager()
        mgr._running = True
        ws = AsyncMock()
        ws.closed = True
        mgr._ws = ws

        await mgr.disconnect()
        # Should not call close on already closed WS
        ws.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disconnect_when_no_ws(self) -> None:
        """Test disconnect when no ws."""
        mgr = self._make_manager()
        mgr._running = True
        # No WS at all
        await mgr.disconnect()
        assert mgr._running is False

    def test_reconnect_delay_reset_logic(self) -> None:
        """Verify that _reconnect_delay is set to min by default."""
        mgr = self._make_manager()
        assert mgr._reconnect_delay == CLOUD_RECONNECT_MIN


# ---------------------------------------------------------------------------
# Registration helpers
# ---------------------------------------------------------------------------


class TestRegisterCloudInstance:
    """Tests for register_cloud_instance helper."""

    @pytest.mark.asyncio
    async def test_register(self) -> None:
        """Test register."""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(
            return_value={
                "id": "inst-123",
                "password": "pwd-xyz",
                "connection_token": "tok-abc",
            }
        )

        session = MagicMock(spec=aiohttp.ClientSession)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = ctx

        result = await register_cloud_instance(session)
        assert result["id"] == "inst-123"
        assert result["password"] == "pwd-xyz"
        assert result["connection_token"] == "tok-abc"

    @pytest.mark.asyncio
    async def test_register_no_platform_param(self) -> None:
        """Test register no platform param."""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(
            return_value={
                "id": "inst-1",
                "password": "p",
                "connection_token": "t",
            }
        )

        session = MagicMock(spec=aiohttp.ClientSession)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = ctx

        await register_cloud_instance(session)
        # Standard cloud mode: no json body (compatible with yaha-cloud.ru)
        call_kwargs = session.post.call_args
        assert call_kwargs.kwargs.get("json") is None


class TestGetCloudOtp:
    """Tests for get_cloud_otp helper."""

    @pytest.mark.asyncio
    async def test_get_otp(self) -> None:
        """Test get otp."""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"code": "123456"})

        session = MagicMock(spec=aiohttp.ClientSession)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = ctx

        otp = await get_cloud_otp(session, "inst-123", SecretStr("tok-abc"))
        assert otp == "123456"

    @pytest.mark.asyncio
    async def test_get_otp_uses_post(self) -> None:
        """Test get otp uses post."""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"code": "999"})

        session = MagicMock(spec=aiohttp.ClientSession)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = ctx

        await get_cloud_otp(session, "inst-1", SecretStr("tok-1"))
        session.post.assert_called_once()
        assert "inst-1" in str(session.post.call_args)
