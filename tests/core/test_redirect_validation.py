"""Tests for redirect URL validation helpers."""

from unittest.mock import MagicMock, patch

from music_assistant.helpers.redirect_validation import _is_private_ip, is_allowed_redirect_url


def test_empty_url_is_blocked() -> None:
    """Empty URL is always blocked."""
    ok, category = is_allowed_redirect_url("")
    assert ok is False
    assert category == "blocked"


def test_musicassistant_scheme_trusted() -> None:
    """Custom musicassistant:// scheme is trusted."""
    ok, category = is_allowed_redirect_url("musicassistant://auth/callback")
    assert ok is True
    assert category == "trusted"


def test_home_assistant_my_trusted() -> None:
    """Home Assistant my.home-assistant.io is trusted."""
    ok, category = is_allowed_redirect_url("https://my.home-assistant.io/redirect/oauth")
    assert ok is True
    assert category == "trusted"


def test_homeassistant_local_http_trusted() -> None:
    """homeassistant.local over http is trusted."""
    ok, category = is_allowed_redirect_url("http://homeassistant.local/callback")
    assert ok is True
    assert category == "trusted"


def test_homeassistant_local_https_trusted() -> None:
    """homeassistant.local over https is trusted."""
    ok, category = is_allowed_redirect_url("https://homeassistant.local/callback")
    assert ok is True
    assert category == "trusted"


def test_localhost_trusted() -> None:
    """Localhost is always trusted."""
    ok, category = is_allowed_redirect_url("http://localhost/callback")
    assert ok is True
    assert category == "trusted"


def test_127_0_0_1_trusted() -> None:
    """Loopback IP 127.0.0.1 is trusted."""
    ok, category = is_allowed_redirect_url("http://127.0.0.1/callback")
    assert ok is True
    assert category == "trusted"


def test_ipv6_loopback_trusted() -> None:
    """IPv6 loopback ::1 is trusted."""
    ok, category = is_allowed_redirect_url("http://[::1]/callback")
    assert ok is True
    assert category == "trusted"


def test_private_rfc1918_class_a_trusted() -> None:
    """Private RFC-1918 class A address is trusted."""
    ok, category = is_allowed_redirect_url("http://10.0.0.1/callback")
    assert ok is True
    assert category == "trusted"


def test_private_rfc1918_class_c_trusted() -> None:
    """Private RFC-1918 class C address is trusted."""
    ok, category = is_allowed_redirect_url("http://192.168.1.100/callback")
    assert ok is True
    assert category == "trusted"


def test_external_url_requires_consent() -> None:
    """External HTTPS URL is valid but external (requires consent)."""
    ok, category = is_allowed_redirect_url("https://example.com/callback")
    assert ok is True
    assert category == "external"


def test_invalid_scheme_blocked() -> None:
    """Non-http(s) URLs without a registered custom scheme are blocked."""
    ok, category = is_allowed_redirect_url("javascript://evil.com")
    assert ok is False
    assert category == "blocked"


def test_ftp_scheme_blocked() -> None:
    """FTP scheme is blocked."""
    ok, category = is_allowed_redirect_url("ftp://evil.com/steal")
    assert ok is False
    assert category == "blocked"


def test_same_origin_request_trusted() -> None:
    """URL matching the request host is trusted (same origin)."""
    mock_request = MagicMock()
    mock_request.host = "mymass.example.com:8095"
    ok, category = is_allowed_redirect_url(
        "http://mymass.example.com:8095/callback", request=mock_request
    )
    assert ok is True
    assert category == "trusted"


def test_base_url_trusted() -> None:
    """URL matching the configured base_url is trusted."""
    ok, category = is_allowed_redirect_url(
        "https://mymass.example.com/callback",
        base_url="https://mymass.example.com",
    )
    assert ok is True
    assert category == "trusted"


def test_is_private_ip_rfc1918() -> None:
    """Private RFC-1918 addresses are identified correctly."""
    assert _is_private_ip("10.0.0.1") is True
    assert _is_private_ip("172.16.0.1") is True
    assert _is_private_ip("192.168.0.1") is True


def test_is_private_ip_public() -> None:
    """Public IPs are not private."""
    assert _is_private_ip("8.8.8.8") is False
    assert _is_private_ip("1.1.1.1") is False


def test_is_private_ip_invalid_hostname() -> None:
    """Non-IP hostname returns False without raising."""
    assert _is_private_ip("not-an-ip.example.com") is False


def test_url_with_no_hostname_blocked() -> None:
    """URLs with an empty hostname component are blocked."""
    # http:///path has http scheme but no hostname
    ok, category = is_allowed_redirect_url("http:///path")
    assert ok is False
    assert category == "blocked"


def test_url_parsing_exception_returns_blocked() -> None:
    """If URL parsing raises an unexpected exception the URL is blocked."""
    with patch(
        "music_assistant.helpers.redirect_validation.urlparse",
        side_effect=RuntimeError("unexpected"),
    ):
        ok, category = is_allowed_redirect_url("https://example.com/callback")
    assert ok is False
    assert category == "blocked"
