"""Tests for the Chromecast Sendspin bridge."""

from unittest.mock import MagicMock

from music_assistant.providers.chromecast.sendspin_bridge import get_sendspin_server_url


def test_sendspin_server_url_uses_secure_proxy() -> None:
    """Prefer the HTTPS capability URL when the webserver exposes one."""
    mass = MagicMock()
    mass.webserver.sendspin_cast_base_url = "https://music.example.test/sendspin-cast/token"

    assert get_sendspin_server_url(mass) == (
        "https://music.example.test/sendspin-cast/token",
        True,
    )


def test_sendspin_server_url_falls_back_to_direct_ipv4() -> None:
    """Keep the existing direct WebSocket transport without an HTTPS proxy."""
    mass = MagicMock()
    mass.webserver.sendspin_cast_base_url = None
    mass.streams.publish_ip = "192.0.2.10"

    assert get_sendspin_server_url(mass) == ("ws://192.0.2.10:8927", False)


def test_sendspin_server_url_formats_direct_ipv6() -> None:
    """Preserve bracket formatting for the direct IPv6 fallback."""
    mass = MagicMock()
    mass.webserver.sendspin_cast_base_url = None
    mass.streams.publish_ip = "2001:db8::10"

    assert get_sendspin_server_url(mass) == ("ws://[2001:db8::10]:8927", False)
