"""
Tests for dropping unreachable IPv6 host services from discovered Cast info.

Regression tests for https://github.com/music-assistant/support/issues/5753.

pychromecast builds its control socket with ``socket.AF_INET``, so a host service
holding a native IPv6 address can never connect. Zeroconf reports IPv4 addresses
before IPv6 ones, so such an address only turns up when a device's AAAA record is
cached without its A record. pychromecast then stores it as a fallback host, which
costs a failed connect and a five second backoff on every attempt that follows.
"""

from __future__ import annotations

from uuid import UUID

from pychromecast.models import CastInfo, HostServiceInfo, MDNSServiceInfo

from music_assistant.providers.chromecast.helpers import without_ipv6_host_services

MDNS = MDNSServiceInfo(name="LS60D-bfd86af172639ff36e454aa4759903da._googlecast._tcp.local.")
IPV4 = HostServiceInfo(host="192.168.188.67", port=8009)
# global IPv6 as handed out by an ISP prefix
IPV6 = HostServiceInfo(host="2003:e4:d736:9600:5c02:8ec3:c457:fbae", port=8009)
# 6to4, which an AF_INET socket cannot use either
IPV6_6TO4 = HostServiceInfo(host="2002:c0a8:176::1", port=8009)
# IPv4-mapped IPv6, which an AF_INET socket resolves back to plain IPv4
IPV6_MAPPED = HostServiceInfo(host="::ffff:c0a8:176", port=8009)
HOSTNAME = HostServiceInfo(host="bfd86af1-7263-9ff3-6e45-4aa4759903da.local.", port=8009)


def _cast_info(*services: HostServiceInfo | MDNSServiceInfo) -> CastInfo:
    return CastInfo(
        services=set(services),
        uuid=UUID("bfd86af1-7263-9ff3-6e45-4aa4759903da"),
        model_name="LS60D",
        friendly_name="Essbereich Music Frame",
        host="192.168.188.67",
        port=8009,
        cast_type=None,
        manufacturer=None,
    )


def test_ipv6_host_service_is_dropped() -> None:
    """An IPv6 host service is removed while the reachable ones are kept."""
    result = without_ipv6_host_services(_cast_info(MDNS, IPV4, IPV6))
    assert result.services == {MDNS, IPV4}


def test_six_to_four_host_service_is_dropped() -> None:
    """A 6to4 address is dropped as well: it is still a native IPv6 address."""
    result = without_ipv6_host_services(_cast_info(MDNS, IPV4, IPV6_6TO4))
    assert result.services == {MDNS, IPV4}


def test_ipv4_mapped_ipv6_host_service_is_kept() -> None:
    """An IPv4-mapped IPv6 literal is kept: an AF_INET socket resolves it to IPv4."""
    cast_info = _cast_info(MDNS, IPV6_MAPPED)
    assert without_ipv6_host_services(cast_info) is cast_info


def test_hostname_host_service_is_kept() -> None:
    """A host service holding a hostname is left for pychromecast to resolve."""
    result = without_ipv6_host_services(_cast_info(HOSTNAME, IPV6))
    assert result.services == {HOSTNAME}


def test_cast_info_is_returned_unchanged_when_nothing_to_drop() -> None:
    """Without an IPv6 address the given cast info is returned unchanged."""
    cast_info = _cast_info(MDNS, IPV4)
    assert without_ipv6_host_services(cast_info) is cast_info


def test_only_ipv6_services_are_left_alone() -> None:
    """
    An IPv6-only service list is kept rather than emptied.

    An empty list leaves the socket client without a service to reconnect through
    once discovery reports the device's IPv4 address.
    """
    cast_info = _cast_info(IPV6)
    assert without_ipv6_host_services(cast_info) is cast_info


def test_other_cast_info_fields_are_preserved() -> None:
    """Filtering only replaces the services; the rest of the cast info is untouched."""
    cast_info = _cast_info(MDNS, IPV4, IPV6)
    result = without_ipv6_host_services(cast_info)
    assert result.uuid == cast_info.uuid
    assert result.friendly_name == cast_info.friendly_name
    assert result.model_name == cast_info.model_name
    assert result.host == cast_info.host
    assert result.port == cast_info.port
