"""Helpers for the Sonos (S2) Provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeroconf import IPVersion

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo


def get_primary_ip_address(discovery_info: AsyncServiceInfo) -> str | None:
    """Get primary IP address from zeroconf discovery info."""
    for address in discovery_info.parsed_addresses(IPVersion.V4Only):
        if address.startswith("127"):
            # filter out loopback address
            continue
        if address.startswith("169.254"):
            # filter out APIPA address
            continue
        return address
    return None
