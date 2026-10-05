"""Tests for the Sendspin management-call timeout and transport-error mapping."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

import music_assistant.providers.sendspin.provider as provider_module
from music_assistant.providers.sendspin.helpers import SecurityActionError
from music_assistant.providers.sendspin.provider import SendspinProvider

if TYPE_CHECKING:
    from aiosendspin.server.connection import SendspinConnection


class _FakeConnection:
    """Connection stand-in recording whether disconnect was awaited."""

    def __init__(self) -> None:
        self.disconnect_calls = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


async def test_management_call_returns_result() -> None:
    """A prompt reply passes straight through, leaving the connection untouched."""
    provider = SendspinProvider.__new__(SendspinProvider)
    conn = _FakeConnection()

    async def _reply() -> str:
        await asyncio.sleep(0)
        return "ok"

    result = await provider._management_call(cast("SendspinConnection", conn), _reply())
    assert result == "ok"
    assert conn.disconnect_calls == 0


async def test_management_call_timeout_disconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timed-out request drops the connection so its ordered channel can't desync the next."""
    monkeypatch.setattr(provider_module, "MANAGEMENT_REQUEST_TIMEOUT", 0.01)
    provider = SendspinProvider.__new__(SendspinProvider)
    conn = _FakeConnection()

    async def _hang() -> str:
        await asyncio.Event().wait()
        return ""

    with pytest.raises(SecurityActionError) as excinfo:
        await provider._management_call(cast("SendspinConnection", conn), _hang())
    assert excinfo.value.alert_key == "management_error_timeout"
    assert conn.disconnect_calls == 1


async def test_management_call_runtime_error_maps_without_disconnect() -> None:
    """A transport RuntimeError becomes a SecurityActionError and leaves the connection alone."""
    provider = SendspinProvider.__new__(SendspinProvider)
    conn = _FakeConnection()

    async def _boom() -> str:
        await asyncio.sleep(0)
        raise RuntimeError("connection is not active")

    with pytest.raises(SecurityActionError) as excinfo:
        await provider._management_call(cast("SendspinConnection", conn), _boom())
    assert excinfo.value.alert_key == "management_error_generic"
    assert excinfo.value.detail == "connection is not active"
    assert conn.disconnect_calls == 0
