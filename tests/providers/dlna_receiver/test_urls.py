"""Tests for URL validation and redaction helpers."""

from __future__ import annotations

import asyncio
import socket

import pytest

from music_assistant.providers.dlna_receiver.urls import (
    redact_url,
    validate_outbound_url,
    validate_stream_url,
)


@pytest.mark.parametrize(
    "uri",
    [
        "http://stream.example.com/audio.flac",
        "https://stream.example.com:8443/audio.mp3",
        "HTTP://Stream.Example.com/upper.flac",
        "http://10.0.0.5:8080/qobuz-stream?t=abc",
    ],
)
def test_validate_stream_url_accepts_http_and_https(uri: str) -> None:
    """Stream URLs with http/https schemes and a host are accepted as-is."""
    assert validate_stream_url(uri) == uri


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "file:///etc/passwd",
        "gopher://evil.example/1",
        "ftp://files.example.com/song.flac",
        "javascript:alert(1)",
        "data:audio/mp3;base64,AAAA",
        "ws://example.com/stream",
    ],
)
def test_validate_stream_url_rejects_non_http_schemes(uri: str) -> None:
    """Any non-http(s) scheme (or empty input) is rejected."""
    assert validate_stream_url(uri) is None


def test_validate_stream_url_rejects_missing_host() -> None:
    """A URL without a hostname is rejected even if the scheme is http(s)."""
    assert validate_stream_url("http:///no-host") is None
    assert validate_stream_url("https://") is None


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8",
        "192.168.1.10",
        "10.0.0.8",
        "172.16.4.2",
        "100.64.1.2",
        "2001:4860:4860::8888",
        "fd12:3456:789a::1",
    ],
)
async def test_validate_outbound_url_allows_public_and_lan_addresses(host: str) -> None:
    """Public, private, CGNAT, and ULA stream destinations remain usable."""
    bracketed = f"[{host}]" if ":" in host else host
    uri = f"http://{bracketed}/audio.flac"

    assert await validate_outbound_url(uri) == uri


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "127.0.0.1",
        "169.254.2.3",
        "224.0.0.1",
        "240.0.0.1",
        "::",
        "::1",
        "fe80::1",
        "ff02::1",
        "::ffff:127.0.0.1",
    ],
)
async def test_validate_outbound_url_rejects_unsafe_special_addresses(host: str) -> None:
    """Unsafe special-purpose destinations are rejected after IP normalization."""
    bracketed = f"[{host}]" if ":" in host else host

    assert await validate_outbound_url(f"http://{bracketed}/audio.flac") is None


async def test_validate_outbound_url_accepts_hostname_when_all_dns_answers_are_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostname is accepted only when resolution returns safe destinations."""
    loop = asyncio.get_running_loop()

    async def _resolve(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::20", 0, 0, 0)),
        ]

    monkeypatch.setattr(loop, "getaddrinfo", _resolve)

    assert await validate_outbound_url("http://nas.local/audio.flac") == (
        "http://nas.local/audio.flac"
    )


@pytest.mark.parametrize("answers", [[], ["192.168.1.20", "127.0.0.1"], ["::1"]])
async def test_validate_outbound_url_rejects_missing_or_unsafe_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
    answers: list[str],
) -> None:
    """No DNS answer or any unsafe answer rejects the entire hostname."""
    loop = asyncio.get_running_loop()

    async def _resolve(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        result: list[tuple[object, ...]] = []
        for answer in answers:
            if ":" in answer:
                result.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (answer, 0, 0, 0)))
            else:
                result.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (answer, 0)))
        return result

    monkeypatch.setattr(loop, "getaddrinfo", _resolve)

    assert await validate_outbound_url("http://mixed.local/audio.flac") is None


async def test_validate_outbound_url_rejects_dns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname that cannot be resolved is not a valid outbound destination."""
    loop = asyncio.get_running_loop()

    async def _resolve(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        raise socket.gaierror("not found")

    monkeypatch.setattr(loop, "getaddrinfo", _resolve)

    assert await validate_outbound_url("http://missing.local/audio.flac") is None


async def test_validate_outbound_url_rejects_invalid_idna_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolver IDNA encoding errors are handled as invalid destinations."""
    loop = asyncio.get_running_loop()

    async def _resolve(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        raise UnicodeEncodeError("idna", "\ud800", 0, 1, "invalid hostname")

    monkeypatch.setattr(loop, "getaddrinfo", _resolve)

    assert await validate_outbound_url("http://invalid.example/audio.flac") is None


def test_redact_url_strips_query_without_userinfo() -> None:
    """
    Query params are dropped even when there is no userinfo to mask.

    Signed URLs / bearer tokens commonly live in the query string; keeping
    them in logs would defeat the purpose of redact_url.
    """
    redacted = redact_url("http://example.com:8080/path?token=secret&sig=abc")
    assert "secret" not in redacted
    assert "sig" not in redacted
    assert "token" not in redacted
    assert redacted == "http://example.com:8080/path"


def test_redact_url_strips_fragment() -> None:
    """Fragment is dropped (may also contain sensitive data in some flows)."""
    redacted = redact_url("https://example.com/foo#access_token=secret")
    assert "secret" not in redacted
    assert redacted == "https://example.com/foo"


def test_redact_url_masks_user_and_password_and_drops_query() -> None:
    """user:pass@host is replaced with ***@host; query is dropped entirely."""
    redacted = redact_url("http://alice:secret@example.com:8080/stream?token=xyz")
    assert "alice" not in redacted
    assert "secret" not in redacted
    assert "xyz" not in redacted
    assert redacted == "http://***@example.com:8080/stream"


def test_redact_url_masks_user_only() -> None:
    """A bare user (no password) still triggers redaction."""
    redacted = redact_url("https://alice@example.com/foo")
    assert "alice" not in redacted
    assert redacted == "https://***@example.com/foo"


def test_redact_url_invalid_returns_placeholder() -> None:
    """A completely unparsable URL yields the sentinel placeholder."""
    # urlsplit is quite permissive; use a string that provokes ValueError.
    redacted = redact_url("http://[invalid-ipv6")
    assert redacted == "<invalid-url>"


def test_redact_url_invalid_port_returns_placeholder_without_leaking_secrets() -> None:
    """An invalid port cannot break or expose data from an error-reporting path."""
    redacted = redact_url("http://alice:secret@example.com:99999/path?token=signed")

    assert redacted == "<invalid-url>"


def test_redact_url_preserves_ipv6_brackets() -> None:
    """IPv6 hosts keep their brackets when userinfo is stripped."""
    redacted = redact_url("http://user:pass@[::1]:8080/x")
    assert redacted == "http://***@[::1]:8080/x"


def test_redact_url_preserves_ipv6_brackets_no_port() -> None:
    """IPv6 hosts without a port also keep brackets."""
    redacted = redact_url("https://alice@[2001:db8::1]/foo")
    assert redacted == "https://***@[2001:db8::1]/foo"
