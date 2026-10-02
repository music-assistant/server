"""Tests for provider/url_helpers.py — public-HTTPS detection + validation."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant.providers.yandex_alice.url_helpers import (
    is_public_https_url,
    try_detect_public_https_url,
    validate_external_base_url,
)


class TestIsPublicHttpsUrl:
    """is_public_https_url accepts only public HTTPS URLs."""

    def test_public_https_hostname(self) -> None:
        """Hostname over HTTPS → True."""
        assert is_public_https_url("https://ma.example.com") is True

    def test_public_https_with_port_and_path(self) -> None:
        """Port + path don't change the verdict."""
        assert is_public_https_url("https://ha.example.com:8123/api") is True

    def test_http_rejected(self) -> None:
        """Plain http:// → False (Yandex requires TLS)."""
        assert is_public_https_url("http://ma.example.com") is False

    def test_empty_rejected(self) -> None:
        """Empty string → False."""
        assert is_public_https_url("") is False

    def test_loopback_rejected(self) -> None:
        """Localhost / 127.0.0.1 → False (not reachable from Yandex)."""
        assert is_public_https_url("https://127.0.0.1") is False
        assert is_public_https_url("https://localhost:8095") is False
        assert is_public_https_url("https://localhost.:8095") is False

    def test_private_ip_rejected(self) -> None:
        """RFC1918 / Docker bridge addresses → False."""
        assert is_public_https_url("https://192.168.1.10") is False
        assert is_public_https_url("https://10.0.0.1") is False
        assert is_public_https_url("https://172.22.0.2:8095") is False

    def test_invalid_scheme_rejected(self) -> None:
        """ws://, ftp://, etc. → False."""
        assert is_public_https_url("ws://ma.example.com") is False


class TestValidateExternalBaseUrl:
    """validate_external_base_url is the ConfigEntry-compatible front."""

    def test_empty_string_allowed(self) -> None:
        """Empty value is OK at form-load (user hasn't typed yet)."""
        assert validate_external_base_url("") is True

    def test_whitespace_allowed(self) -> None:
        """Whitespace-only → treated as empty, OK."""
        assert validate_external_base_url("   ") is True

    def test_https_allowed(self) -> None:
        """Valid HTTPS URL → True."""
        assert validate_external_base_url("https://ma.example.com") is True

    def test_http_rejected(self) -> None:
        """http:// → False."""
        assert validate_external_base_url("http://ma.example.com") is False

    def test_non_string_rejected(self) -> None:
        """Other types (e.g. int, None) → False."""
        assert validate_external_base_url(None) is False
        assert validate_external_base_url(42) is False


class TestTryDetectPublicHttpsUrl:
    """
    try_detect_public_https_url reads mass.webserver.base_url only.

    The Yandex webhook lives on the webserver (port 8095 by default),
    not on the streamserver (8097). Probing ``mass.streams.base_url``
    would hand the user the streamserver URL — we must read the
    webserver URL exclusively.
    """

    def test_webserver_public_https_returned(self) -> None:
        """When mass.webserver.base_url is a public HTTPS URL, return it."""
        mass = MagicMock()
        mass.streams.base_url = "http://172.22.0.2:8097"  # ignored
        mass.webserver.base_url = "https://ma.example.com"
        assert try_detect_public_https_url(mass) == "https://ma.example.com"

    def test_streams_public_https_does_not_leak_through(self) -> None:
        """Even if streams happens to be public HTTPS, we don't return it."""
        mass = MagicMock()
        mass.streams.base_url = "https://stream.example.com"
        mass.webserver.base_url = "http://172.22.0.2:8095"
        assert try_detect_public_https_url(mass) is None

    def test_webserver_internal_returns_none(self) -> None:
        """Typical Docker setup — internal → None."""
        mass = MagicMock()
        mass.streams.base_url = "http://172.22.0.2:8097"
        mass.webserver.base_url = "http://172.22.0.2:8095"
        assert try_detect_public_https_url(mass) is None

    def test_no_attributes_returns_none(self) -> None:
        """Older MA without these attrs → None, no exception."""
        mass = MagicMock(spec=[])  # no attributes whatsoever
        assert try_detect_public_https_url(mass) is None
