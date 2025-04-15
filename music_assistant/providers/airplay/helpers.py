"""Various helpers/utilities for the AirPlay provider."""

from __future__ import annotations

import os
import platform
from typing import TYPE_CHECKING

from zeroconf import IPVersion

from music_assistant.helpers.process import check_output
from music_assistant.providers.airplay.const import BROKEN_RAOP_MODELS

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo


def convert_airplay_volume(value: float) -> int:
    """Remap AirPlay Volume to 0..100 scale."""
    airplay_min = -30
    airplay_max = 0
    normal_min = 0
    normal_max = 100
    portion = (value - airplay_min) * (normal_max - normal_min) / (airplay_max - airplay_min)
    return int(portion + normal_min)


def get_model_info(info: AsyncServiceInfo) -> tuple[str, str]:
    """Return Manufacturer and Model name from mdns info."""
    manufacturer = info.decoded_properties.get("manufacturer")
    model = info.decoded_properties.get("model")
    if manufacturer and model:
        return (manufacturer, model)
    # try parse from am property
    if am_property := info.decoded_properties.get("am"):
        model = am_property

    if not model:
        model = "Unknown"

    # parse apple model names
    if model == "AudioAccessory6,1":
        return ("Apple", "HomePod 2")
    if model in ("AudioAccessory5,1", "AudioAccessorySingle5,1"):
        return ("Apple", "HomePod Mini")
    if model == "AppleTV1,1":
        return ("Apple", "Apple TV Gen1")
    if model == "AppleTV2,1":
        return ("Apple", "Apple TV Gen2")
    if model in ("AppleTV3,1", "AppleTV3,2"):
        return ("Apple", "Apple TV Gen3")
    if model == "AppleTV5,3":
        return ("Apple", "Apple TV Gen4")
    if model == "AppleTV6,2":
        return ("Apple", "Apple TV 4K")
    if model == "AppleTV11,1":
        return ("Apple", "Apple TV 4K Gen2")
    if model == "AppleTV14,1":
        return ("Apple", "Apple TV 4K Gen3")
    if "AirPort" in model:
        return ("Apple", "AirPort Express")
    if "AudioAccessory" in model:
        return ("Apple", "HomePod")
    if "AppleTV" in model:
        model = "Apple TV"
        manufacturer = "Apple"

    return (manufacturer or "AirPlay", model)


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


def is_broken_raop_model(manufacturer: str, model: str) -> bool:
    """Check if a model is known to have broken RAOP support."""
    for broken_manufacturer, broken_model in BROKEN_RAOP_MODELS:
        if broken_manufacturer in (manufacturer, "*") and broken_model in (model, "*"):
            return True
    return False


async def get_cliraop_binary() -> str:
    """Find the correct raop/airplay binary belonging to the platform."""

    async def check_binary(cliraop_path: str) -> str | None:
        try:
            returncode, output = await check_output(
                cliraop_path,
                "-check",
            )
            if returncode == 0 and output.strip().decode() == "cliraop check":
                return cliraop_path
        except OSError:
            pass
        return None

    base_path = os.path.join(os.path.dirname(__file__), "bin")
    system = platform.system().lower().replace("darwin", "macos")
    architecture = platform.machine().lower()

    if bridge_binary := await check_binary(
        os.path.join(base_path, f"cliraop-{system}-{architecture}")
    ):
        return bridge_binary

    msg = f"Unable to locate RAOP Play binary for {system}/{architecture}"
    raise RuntimeError(msg)
