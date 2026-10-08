"""Tests for the outbound URL guard and endpoint helpers in the security helpers."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.security import (
    ensure_safe_outbound_url,
    provider_configured_endpoints,
)

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
    ("key", "value", "expected"),
    [
        ("url", "http://192.168.1.10:8096", {("192.168.1.10", 8096)}),
        ("url", "https://Jellyfin.Example.com/base", {("jellyfin.example.com", 443)}),
        ("url", "http://localhost", {("localhost", 80)}),
        ("host", "192.168.1.10:8096", {("192.168.1.10", 8096)}),
        ("host", "nas.local", {("nas.local", 80), ("nas.local", 443)}),
        ("ip_address", "fd00::1", {("fd00::1", 80), ("fd00::1", 443)}),
        ("library_id", "http://10.0.0.5:32400", {("10.0.0.5", 32400)}),
        ("host", "not a host", set()),
        ("host", "nas.local/music", set()),
        ("library_id", "nas.local", set()),
        ("url", "", set()),
    ],
)
def test_provider_configured_endpoints(
    key: str, value: str, expected: set[tuple[str, int]]
) -> None:
    """URL values and bare host values of host-like keys are collected."""
    provider = _fake_provider([(key, ConfigEntryType.STRING, value)])
    assert provider_configured_endpoints(provider) == expected


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ({"url": "http://localhost:8096"}, {("localhost", 8096)}),
        ({"ip_address": "127.0.0.1", "port": 4533}, {("127.0.0.1", 4533)}),
        (
            {"local_server_ip": "192.168.1.5", "local_server_port": "32400"},
            {("192.168.1.5", 32400)},
        ),
        ({"baseURL": "http://navidrome.local", "port": 4533}, {("navidrome.local", 4533)}),
        ({"baseURL": "http://navidrome.local:4040", "port": 4533}, {("navidrome.local", 4040)}),
        ({"host": "nas.local", "port": "not-a-port"}, {("nas.local", 80), ("nas.local", 443)}),
        ({"url": "http://localhost:8096", "token": "http://10.0.0.9"}, {("localhost", 8096)}),
    ],
)
def test_provider_configured_endpoints_from_setup_data(
    setup: dict[str, object], expected: set[tuple[str, int]]
) -> None:
    """Host-like setup values are decrypted and paired with a sibling port key."""
    provider = _fake_provider([], setup)
    assert provider_configured_endpoints(provider) == expected
    read_keys = {call.args[0] for call in provider.get_setup_value.call_args_list}
    assert "token" not in read_keys


def _fake_provider(
    entries: list[tuple[str, ConfigEntryType, str]], setup: dict[str, object] | None = None
) -> MagicMock:
    """Return a provider with the given (key, type, stored value) entries and setup values."""
    setup = setup or {}
    raw = {
        "type": "music",
        "domain": "jellyfin",
        "instance_id": "jellyfin--1",
        "values": {key: value for key, _, value in entries},
        "setup_data": {key: f"_encrypted_{key}" for key in setup},
    }
    config_entries = [ConfigEntry(key=key, type=type_, label=key) for key, type_, _ in entries]
    provider = MagicMock()
    provider.config = cast("ProviderConfig", ProviderConfig.parse(config_entries, raw))
    provider.get_setup_value = MagicMock(side_effect=setup.get)
    return provider
