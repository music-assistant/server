"""Tests for provider/webhook_probe.py — outgoing reachability probe."""

from __future__ import annotations

from typing import Any, Self
from unittest.mock import MagicMock

import aiohttp
import pytest

from music_assistant.providers.yandex_alice.webhook_probe import probe_webhook_reachability


def _patch_session_post(monkeypatch: pytest.MonkeyPatch, *, status: int) -> None:
    """Replace aiohttp.ClientSession with one whose POST returns the given status."""

    class _FakeResp:
        def __init__(self, code: int) -> None:
            self.status = code

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    class _FakeSession:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def post(self, *_args: Any, **_kwargs: Any) -> _FakeResp:
            return _FakeResp(status)

    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)


def _patch_session_raises(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    """Replace aiohttp.ClientSession so .post(...) raises *exc*."""

    class _FakeSession:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def post(self, *_args: Any, **_kwargs: Any) -> Any:
            class _Ctx:
                async def __aenter__(self) -> Any:
                    raise exc

                async def __aexit__(self, *_: object) -> None:
                    return None

            return _Ctx()

    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)


class TestPreflightValidation:
    """Pre-network input checks short-circuit before any HTTP call."""

    @pytest.mark.asyncio
    async def test_empty_base_url(self) -> None:
        """Empty external_base_url → fail with friendly hint."""
        ok, msg = await probe_webhook_reachability("", "secret")
        assert ok is False
        assert "External base URL is empty" in msg

    @pytest.mark.asyncio
    async def test_http_base_url(self) -> None:
        """http:// scheme → fail before any network call."""
        ok, msg = await probe_webhook_reachability("http://ma.example.com", "secret")
        assert ok is False
        assert "HTTPS" in msg

    @pytest.mark.asyncio
    async def test_empty_secret(self) -> None:
        """Empty webhook secret → fail."""
        ok, msg = await probe_webhook_reachability("https://ma.example.com", "")
        assert ok is False
        assert "secret" in msg.lower()


class TestStatusClassification:
    """HTTP status codes map to specific human-readable verdicts."""

    @pytest.mark.asyncio
    async def test_401_means_reachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 401 from sentinel skill_id → reachable."""
        _patch_session_post(monkeypatch, status=401)
        ok, msg = await probe_webhook_reachability("https://ma.example.com", "secret")
        assert ok is True
        assert "reachable" in msg.lower()
        assert "401" in msg

    @pytest.mark.asyncio
    async def test_200_also_reachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 200 — also reachable (rare but possible)."""
        _patch_session_post(monkeypatch, status=200)
        ok, msg = await probe_webhook_reachability("https://ma.example.com", "secret")
        assert ok is True
        assert "reachable" in msg.lower()

    @pytest.mark.asyncio
    async def test_404_no_route(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 404 → user has wrong webhook secret in config."""
        _patch_session_post(monkeypatch, status=404)
        ok, msg = await probe_webhook_reachability("https://ma.example.com", "secret")
        assert ok is False
        assert "404" in msg

    @pytest.mark.asyncio
    async def test_502_reverse_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 502 → reverse proxy / upstream issue."""
        _patch_session_post(monkeypatch, status=502)
        ok, msg = await probe_webhook_reachability("https://ma.example.com", "secret")
        assert ok is False
        assert "502" in msg


class TestNetworkErrors:
    """aiohttp exceptions are mapped to specific user-facing messages."""

    @pytest.mark.asyncio
    async def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """asyncio.TimeoutError → user-readable timeout message."""
        _patch_session_raises(monkeypatch, TimeoutError())
        ok, msg = await probe_webhook_reachability("https://ma.example.com", "secret")
        assert ok is False
        assert "imed out" in msg

    @pytest.mark.asyncio
    async def test_ssl_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """aiohttp.ClientSSLError → 'TLS / certificate' message."""
        connection = MagicMock()
        ssl_err = aiohttp.ClientSSLError(connection, OSError("bad cert"))
        _patch_session_raises(monkeypatch, ssl_err)
        ok, msg = await probe_webhook_reachability("https://ma.example.com", "secret")
        assert ok is False
        assert "TLS" in msg or "certificate" in msg.lower()
