"""Tests for the URL used to reach the webserver's own API from this host."""

from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest

from music_assistant.controllers.webserver.controller import (
    CONF_BASE_URL,
    CONF_ENABLE_SSL,
    WebserverController,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock Music Assistant instance."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    return mass


@pytest.fixture
def webserver(mock_mass: MagicMock) -> WebserverController:
    """Create a WebserverController carrying the state that setup() resolves."""
    webserver = WebserverController(mock_mass)
    config = MagicMock()
    config.get_value.return_value = False
    webserver.config = cast("CoreConfig", config)
    webserver.publish_port = 8095
    return webserver


@pytest.mark.parametrize(
    ("bind_ip", "publish_ip", "expected"),
    [
        # pinned to a single interface: loopback would not reach the server
        ("192.168.1.5", "192.168.1.5", "http://192.168.1.5:8095"),
        ("fd00::5", "fd00::5", "http://[fd00::5]:8095"),
        # wildcard bind: loopback reaches the server, whatever is advertised.
        # a publish_ip that only exists outside a container/NAT must never be dialed
        ("0.0.0.0", "203.0.113.10", "http://127.0.0.1:8095"),
        ("::", "192.168.1.5", "http://127.0.0.1:8095"),
        ("0.0.0.0", "fd00::1", "http://[::1]:8095"),
        ("::", "fd00::1", "http://[::1]:8095"),
        ("", "192.168.1.5", "http://127.0.0.1:8095"),
        (None, "192.168.1.5", "http://127.0.0.1:8095"),
    ],
)
def test_url_follows_the_bind_address(
    webserver: WebserverController,
    bind_ip: str | None,
    publish_ip: str,
    expected: str,
) -> None:
    """Verify the URL resolves to an address that exists on this host."""
    webserver.bind_ip = bind_ip
    webserver.publish_ip = publish_ip

    assert webserver.internal_base_url == expected


def test_url_ignores_a_configured_base_url(webserver: WebserverController) -> None:
    """Verify an external base URL is not dialed back in through DNS and a reverse proxy."""
    cast("MagicMock", webserver.config).get_value.side_effect = lambda key, default=None: {
        CONF_BASE_URL: "https://ma.example.com",
        CONF_ENABLE_SSL: False,
    }.get(key, default)
    webserver.bind_ip = "0.0.0.0"
    webserver.publish_ip = "192.168.1.5"

    assert webserver.base_url == "https://ma.example.com"
    assert webserver.internal_base_url == "http://127.0.0.1:8095"


def test_url_uses_tls_when_ssl_is_enabled(webserver: WebserverController) -> None:
    """Verify the scheme matches what the webserver serves."""
    cast("MagicMock", webserver.config).get_value.side_effect = lambda key, default=None: {
        CONF_ENABLE_SSL: True
    }.get(key, default)
    webserver.bind_ip = "0.0.0.0"
    webserver.publish_ip = "192.168.1.5"

    assert webserver.internal_base_url == "https://127.0.0.1:8095"
