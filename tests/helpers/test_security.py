"""Tests for the outbound URL guard in the security helpers."""

from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.security import ensure_safe_outbound_url

RESOLVER = "music_assistant.helpers.security.resolve_hostname"


@pytest.mark.parametrize(
    ("url", "resolved"),
    [
        ("http://example.com/a.mp3", ["93.184.215.14"]),
        ("http://192.168.1.10:8123/x", None),
        ("http://10.0.0.5/", None),
        ("https://[fd00::1]/", None),
        ("http://homeassistant.local:8123/api/tts_proxy/x.mp3", ["192.168.1.20"]),
    ],
)
async def test_allowed_urls(url: str, resolved: list[str] | None) -> None:
    """Public and local network hosts are allowed."""
    with patch(RESOLVER, AsyncMock(return_value=resolved)):
        await ensure_safe_outbound_url(MagicMock(), url)


@pytest.mark.parametrize(
    ("url", "resolved", "message"),
    [
        ("http://127.0.0.1/", None, "blocked address"),
        ("http://localhost/", ["127.0.0.1"], "blocked address"),
        ("http://169.254.169.254/latest/meta-data", None, "blocked address"),
        ("http://0.0.0.0/", None, "blocked address"),
        ("http://[::1]/", None, "blocked address"),
        ("http://[::ffff:127.0.0.1]/", None, "blocked address"),
        ("http://224.0.0.1/", None, "blocked address"),
        ("ftp://example.com/", None, "Only http"),
        ("file:///etc/passwd", None, "Only http"),
        ("http:///nohost", None, "Invalid URL"),
        ("http://mixed.example.com/", ["93.184.215.14", "127.0.0.1"], "blocked address"),
        ("http://empty.example.com/", [], "could not be resolved"),
    ],
)
async def test_blocked_urls(url: str, resolved: list[str] | None, message: str) -> None:
    """Loopback, link-local, unspecified, multicast and non-http URLs are refused."""
    with (
        patch(RESOLVER, AsyncMock(return_value=resolved)),
        pytest.raises(InvalidDataError, match=message),
    ):
        await ensure_safe_outbound_url(MagicMock(), url)


async def test_unresolvable_hostname_is_refused() -> None:
    """A hostname that does not resolve is refused without echoing details."""
    with (
        patch(RESOLVER, AsyncMock(side_effect=OSError(None, "Domain name not found"))),
        pytest.raises(InvalidDataError, match="URL could not be resolved"),
    ):
        await ensure_safe_outbound_url(MagicMock(), "http://nonexistent.invalid/")


@pytest.mark.parametrize(
    ("url", "blocked"),
    [("http://192.168.1.10/", False), ("http://127.0.0.1/", True), ("http://[::1]/", True)],
)
async def test_ip_literal_skips_resolver(url: str, blocked: bool) -> None:
    """An IP literal is checked directly, without a DNS lookup."""
    resolver = AsyncMock()
    expectation = pytest.raises(InvalidDataError) if blocked else nullcontext()
    with patch(RESOLVER, resolver), expectation:
        await ensure_safe_outbound_url(MagicMock(), url)
    resolver.assert_not_called()
