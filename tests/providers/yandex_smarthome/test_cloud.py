"""Tests for provider/cloud.py — CloudManager WebSocket and registration helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from provider.cloud import CloudManager, get_cloud_otp, register_cloud_instance
from provider.schema import CloudRequest

# ---------------------------------------------------------------------------
# CloudManager tests
# ---------------------------------------------------------------------------


class TestCloudManager:
    def _make_manager(self, on_request: AsyncMock | None = None) -> CloudManager:
        session = MagicMock(spec=aiohttp.ClientSession)
        if on_request is None:
            on_request = AsyncMock(return_value={"request_id": "r1", "payload": {}})
        return CloudManager(
            session=session,
            connection_token="test-token",
            on_request=on_request,
        )

    def test_initial_state(self):
        mgr = self._make_manager()
        assert mgr.connected is False
        assert mgr._running is False

    def test_connected_property(self):
        mgr = self._make_manager()
        assert mgr.connected is False

        # Simulate connected WS
        ws = MagicMock()
        ws.closed = False
        mgr._ws = ws
        assert mgr.connected is True

        # Simulate closed WS
        ws.closed = True
        assert mgr.connected is False

    @pytest.mark.asyncio
    async def test_handle_message_calls_callback(self):
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
    async def test_handle_message_exception_logged(self):
        callback = AsyncMock(side_effect=RuntimeError("boom"))
        mgr = self._make_manager(on_request=callback)

        ws = AsyncMock()
        # Should not raise
        await mgr._handle_message(ws, {"request_id": "r1", "action": "test"})

    @pytest.mark.asyncio
    async def test_disconnect(self):
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
    async def test_disconnect_when_already_closed(self):
        mgr = self._make_manager()
        mgr._running = True
        ws = AsyncMock()
        ws.closed = True
        mgr._ws = ws

        await mgr.disconnect()
        # Should not call close on already closed WS
        ws.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disconnect_when_no_ws(self):
        mgr = self._make_manager()
        mgr._running = True
        # No WS at all
        await mgr.disconnect()
        assert mgr._running is False

    def test_reconnect_delay_reset_logic(self):
        """Verify that _reconnect_delay is set to min by default."""
        from provider.constants import CLOUD_RECONNECT_MIN

        mgr = self._make_manager()
        assert mgr._reconnect_delay == CLOUD_RECONNECT_MIN


# ---------------------------------------------------------------------------
# Registration helpers
# ---------------------------------------------------------------------------


class TestRegisterCloudInstance:
    @pytest.mark.asyncio
    async def test_register(self):
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
    async def test_register_no_platform_param(self):
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
    @pytest.mark.asyncio
    async def test_get_otp(self):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"code": "123456"})

        session = MagicMock(spec=aiohttp.ClientSession)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = ctx

        otp = await get_cloud_otp(session, "inst-123", "tok-abc")
        assert otp == "123456"

    @pytest.mark.asyncio
    async def test_get_otp_uses_post(self):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"code": "999"})

        session = MagicMock(spec=aiohttp.ClientSession)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = ctx

        await get_cloud_otp(session, "inst-1", "tok-1")
        session.post.assert_called_once()
        assert "inst-1" in str(session.post.call_args)
