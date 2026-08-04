"""Tests for the URL used to reach the in-process Sendspin server."""

from unittest.mock import MagicMock

import pytest

from music_assistant.controllers.webserver.controller import WebserverController


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock Music Assistant instance."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    return mass


@pytest.fixture
def webserver(mock_mass: MagicMock) -> WebserverController:
    """Create a WebserverController backed by a mocked Music Assistant instance."""
    return WebserverController(mock_mass)


@pytest.mark.parametrize(
    ("bind_ip", "publish_ip", "expected"),
    [
        # pinned to a single interface: loopback would not reach the server
        ("192.168.1.5", "192.168.1.5", "ws://192.168.1.5:8927/sendspin"),
        ("fd00::5", "fd00::5", "ws://[fd00::5]:8927/sendspin"),
        # wildcard bind: loopback reaches the server, whatever is advertised.
        # a publish_ip that only exists outside a container/NAT must never be dialed
        ("0.0.0.0", "203.0.113.10", "ws://127.0.0.1:8927/sendspin"),
        ("::", "192.168.1.5", "ws://127.0.0.1:8927/sendspin"),
        ("0.0.0.0", "fd00::1", "ws://[::1]:8927/sendspin"),
        ("::", "fd00::1", "ws://[::1]:8927/sendspin"),
        ("", "192.168.1.5", "ws://127.0.0.1:8927/sendspin"),
    ],
)
def test_url_follows_the_bind_address(
    webserver: WebserverController,
    mock_mass: MagicMock,
    bind_ip: str,
    publish_ip: str,
    expected: str,
) -> None:
    """Verify the URL resolves to an address that exists on this host."""
    mock_mass.streams.bind_ip = bind_ip
    mock_mass.streams.publish_ip = publish_ip

    assert webserver.internal_sendspin_url == expected
