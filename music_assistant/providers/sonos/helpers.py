"""Helpers for the Sonos (S2) Provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeroconf import IPVersion

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo


def get_primary_ip_address(discovery_info: AsyncServiceInfo) -> str | None:
    """Get primary IP address from zeroconf discovery info."""
    for address in discovery_info.ip_addresses_by_version(IPVersion.V4Only):
        if address.is_loopback or address.is_link_local or address.is_unspecified:
            continue
        return str(address)
    # fall back to IPv6 addresses if no usable IPv4 address found
    for address in discovery_info.ip_addresses_by_version(IPVersion.V6Only):
        if address.is_loopback or address.is_link_local or address.is_unspecified:
            continue
        return str(address)
    return None
