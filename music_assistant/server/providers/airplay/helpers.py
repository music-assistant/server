"""Various helpers/utilities for the Airplay provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeroconf import IPVersion

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo


def convert_airplay_volume(value: float) -> int:
    """Remap Airplay Volume to 0..100 scale."""
    airplay_min = -30
    airplay_max = 0
    normal_min = 0
    normal_max = 100
    portion = (value - airplay_min) * (normal_max - normal_min) / (airplay_max - airplay_min)
    return int(portion + normal_min)


def get_model_from_am(am_property: str | None) -> tuple[str, str]:
    """Return Manufacturer and Model name from mdns AM property."""
    manufacturer = "Unknown"
    model = "Generic Airplay device"
    if not am_property:
        return (manufacturer, model)
    if isinstance(am_property, bytes):
        am_property = am_property.decode("utf-8")
    if am_property == "AudioAccessory5,1":
        model = "HomePod"
        manufacturer = "Apple"
    elif "AppleTV" in am_property:
        model = "Apple TV"
        manufacturer = "Apple"
    else:
        model = am_property
    return (manufacturer, model)


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
