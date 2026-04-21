"""Tests for URL validation and redaction helpers."""

from __future__ import annotations

import pytest

from music_assistant.providers.dlna_receiver.urls import redact_url, validate_stream_url


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


def test_redact_url_passthrough_when_no_userinfo() -> None:
    """URLs without userinfo are returned verbatim."""
    uri = "http://example.com:8080/path?q=1"
    assert redact_url(uri) == uri


def test_redact_url_masks_user_and_password() -> None:
    """user:pass@host is replaced with ***@host, preserving port/path/query."""
    redacted = redact_url("http://alice:secret@example.com:8080/stream?token=xyz")
    assert "alice" not in redacted
    assert "secret" not in redacted
    assert redacted == "http://***@example.com:8080/stream?token=xyz"


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


def test_redact_url_preserves_ipv6_brackets() -> None:
    """IPv6 hosts keep their brackets when userinfo is stripped."""
    redacted = redact_url("http://user:pass@[::1]:8080/x")
    assert redacted == "http://***@[::1]:8080/x"


def test_redact_url_preserves_ipv6_brackets_no_port() -> None:
    """IPv6 hosts without a port also keep brackets."""
    redacted = redact_url("https://alice@[2001:db8::1]/foo")
    assert redacted == "https://***@[2001:db8::1]/foo"
