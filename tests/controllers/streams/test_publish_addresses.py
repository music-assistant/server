"""Tests for the streamserver publish address selection."""

from music_assistant.controllers.streams.controller import _get_publish_addresses

ALL_ADDRESSES = ("192.168.1.10", "10.0.0.5", "172.17.0.1", "fd00::10")


def test_wildcard_bind_publishes_every_address() -> None:
    """A wildcard bind publishes every detected address, best candidate first."""
    assert _get_publish_addresses("0.0.0.0", None, ALL_ADDRESSES) == [
        "192.168.1.10",
        "10.0.0.5",
        "172.17.0.1",
        "fd00::10",
    ]
    assert _get_publish_addresses("::", None, ALL_ADDRESSES) == list(ALL_ADDRESSES)
    assert _get_publish_addresses("", None, ALL_ADDRESSES) == list(ALL_ADDRESSES)


def test_specific_bind_publishes_only_that_address() -> None:
    """Binding to one specific address publishes only that address."""
    assert _get_publish_addresses("192.168.1.10", None, ALL_ADDRESSES) == ["192.168.1.10"]
    assert _get_publish_addresses("fd00::10", None, ALL_ADDRESSES) == ["fd00::10"]


def test_configured_publish_ip_is_authoritative() -> None:
    """An explicitly configured publish IP is published on its own, never widened."""
    assert _get_publish_addresses("0.0.0.0", "10.0.0.5", ALL_ADDRESSES) == ["10.0.0.5"]
    # ...even when it is not an address of this host at all (NAT/port forward setups)
    assert _get_publish_addresses("0.0.0.0", "203.0.113.7", ALL_ADDRESSES) == ["203.0.113.7"]
    # ...and it outranks a specific bind IP, matching how publish_ip itself resolves
    assert _get_publish_addresses("192.168.1.10", "203.0.113.7", ALL_ADDRESSES) == ["203.0.113.7"]


def test_single_homed_host_is_unchanged() -> None:
    """The common single-address host keeps publishing exactly that one address."""
    assert _get_publish_addresses("0.0.0.0", None, ("192.168.1.10",)) == ["192.168.1.10"]
