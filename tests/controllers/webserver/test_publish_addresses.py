"""Tests for the webserver publish address selection."""

from music_assistant.controllers.webserver.controller import _get_publish_addresses

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


def test_single_family_host_publishes_single_address() -> None:
    """A host with only one IP family publishes just the publish IP on a wildcard bind."""
    assert _get_publish_addresses("0.0.0.0", "192.168.1.10", ("192.168.1.10",)) == ["192.168.1.10"]
    assert _get_publish_addresses("::", "fd00::10", ("fd00::10", "fd00::20")) == ["fd00::10"]
