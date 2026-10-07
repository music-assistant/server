"""Tests for the webserver publish address selection."""

from music_assistant.controllers.webserver.controller import _get_publish_addresses
from music_assistant.helpers.util import format_ip_for_url

ALL_ADDRESSES = ("192.168.1.10", "10.0.0.5", "fd00::10", "fd00::20")


def test_wildcard_bind_adds_other_ip_family() -> None:
    """A wildcard bind publishes the publish IP plus the first address of the other family."""
    assert _get_publish_addresses("0.0.0.0", "192.168.1.10", ALL_ADDRESSES) == [
        "192.168.1.10",
        "fd00::10",
    ]
    assert _get_publish_addresses(None, "192.168.1.10", ALL_ADDRESSES) == [
        "192.168.1.10",
        "fd00::10",
    ]
    assert _get_publish_addresses("::", "fd00::10", ALL_ADDRESSES) == [
        "fd00::10",
        "192.168.1.10",
    ]


def test_specific_bind_publishes_only_that_address() -> None:
    """Binding to one specific address publishes only that address."""
    assert _get_publish_addresses("fd00::10", "fd00::10", ALL_ADDRESSES) == ["fd00::10"]
    assert _get_publish_addresses("192.168.1.10", "192.168.1.10", ALL_ADDRESSES) == ["192.168.1.10"]


def test_ipv4_only_host() -> None:
    """An IPv4-only host publishes just its IPv4 address on a wildcard bind."""
    v4_addresses = ("192.168.1.10", "10.0.0.5")
    assert _get_publish_addresses("0.0.0.0", "192.168.1.10", v4_addresses) == ["192.168.1.10"]
    assert _get_publish_addresses(None, "192.168.1.10", v4_addresses) == ["192.168.1.10"]


def test_ipv6_only_host() -> None:
    """An IPv6-only host publishes its IPv6 address, for both wildcard and specific binds."""
    v6_addresses = ("fd00::10", "fd00::20")
    assert _get_publish_addresses("::", "fd00::10", v6_addresses) == ["fd00::10"]
    assert _get_publish_addresses(None, "fd00::10", v6_addresses) == ["fd00::10"]
    assert _get_publish_addresses("fd00::10", "fd00::10", v6_addresses) == ["fd00::10"]


def test_ipv6_publish_ip_yields_bracketed_base_url() -> None:
    """An IPv6 publish IP must produce a bracketed host in the (auto) base URL."""
    publish_ip = _get_publish_addresses("::", "fd00::10", ("fd00::10",))[0]
    assert f"http://{format_ip_for_url(publish_ip)}:8095" == "http://[fd00::10]:8095"
