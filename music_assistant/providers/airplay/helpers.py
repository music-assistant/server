"""Various helpers/utilities for the AirPlay provider."""

from __future__ import annotations

import logging
import os
import platform
import time
from typing import TYPE_CHECKING

from zeroconf import IPVersion

from music_assistant.helpers.process import check_output
from music_assistant.providers.airplay.constants import (
    AIRPLAY_2_DEFAULT_MODELS,
    BROKEN_AIRPLAY_MODELS,
    StreamingProtocol,
)

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

_LOGGER = logging.getLogger(__name__)

# NTP epoch delta: difference between Unix epoch (1970) and NTP epoch (1900)
NTP_EPOCH_DELTA = 0x83AA7E80  # 2208988800 seconds


def convert_airplay_volume(value: float) -> int:
    """Remap AirPlay Volume to 0..100 scale."""
    airplay_min = -30
    airplay_max = 0
    normal_min = 0
    normal_max = 100
    portion = (value - airplay_min) * (normal_max - normal_min) / (airplay_max - airplay_min)
    return int(portion + normal_min)


def get_model_info(info: AsyncServiceInfo) -> tuple[str, str]:  # noqa: PLR0911
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
    if model == "UPL-AMP":
        return ("Ubiquiti Inc.", "UPL-AMP")
    if "AirPort" in model:
        return ("Apple", "AirPort Express")
    if "AudioAccessory" in model:
        return ("Apple", "HomePod")
    if "AppleTV" in model:
        model = "Apple TV"
        manufacturer = "Apple"
    # Detect Mac devices (Mac mini, MacBook, iMac, etc.)
    # Model identifiers like: Mac16,11, MacBookPro18,3, iMac21,1
    if model.startswith(("Mac", "iMac")):
        # Parse Mac model to friendly name
        if model.startswith("MacBookPro"):
            return ("Apple", f"MacBook Pro ({model})")
        if model.startswith("MacBookAir"):
            return ("Apple", f"MacBook Air ({model})")
        if model.startswith("MacBook"):
            return ("Apple", f"MacBook ({model})")
        if model.startswith("iMac"):
            return ("Apple", f"iMac ({model})")
        if model.startswith("Macmini"):
            return ("Apple", f"Mac mini ({model})")
        if model.startswith("MacPro"):
            return ("Apple", f"Mac Pro ({model})")
        if model.startswith("MacStudio"):
            return ("Apple", f"Mac Studio ({model})")
        # Generic Mac device (e.g. Mac16,11 for Mac mini M4)
        return ("Apple", f"Mac ({model})")

    return (manufacturer or "AirPlay", model)


def get_primary_ip_address_from_zeroconf(discovery_info: AsyncServiceInfo) -> str | None:
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


def is_broken_airplay_model(manufacturer: str, model: str) -> bool:
    """Check if a model is known to have broken RAOP support."""
    for broken_manufacturer, broken_model in BROKEN_AIRPLAY_MODELS:
        if broken_manufacturer in (manufacturer, "*") and broken_model in (model, "*"):
            return True
    return False


def is_airplay2_preferred_model(manufacturer: str, model: str) -> bool:
    """Check if a model is known to work better with AirPlay 2 protocol."""
    for ap2_manufacturer, ap2_model in AIRPLAY_2_DEFAULT_MODELS:
        if ap2_manufacturer in (manufacturer, "*") and ap2_model in (model, "*"):
            return True
    return False


async def get_cli_binary(protocol: StreamingProtocol) -> str:
    """Find the correct raop/airplay binary belonging to the platform.

    Args:
        protocol: The streaming protocol (RAOP or AIRPLAY2)

    Returns:
        Path to the CLI binary

    Raises:
        RuntimeError: If the binary cannot be found
    """

    async def check_binary(cli_path: str) -> str | None:
        try:
            if protocol == StreamingProtocol.RAOP:
                args = [
                    cli_path,
                    "-check",
                ]
                passing_output = "cliraop check"
            else:
                args = [
                    cli_path,
                    "--testrun",
                ]
                passing_output = "cliap2 check"

            returncode, output = await check_output(*args)
            _LOGGER.debug("%s returned %d with output: %s", cli_path, int(returncode), str(output))
            if returncode == 0 and output.strip().decode() == passing_output:
                return cli_path
        except OSError:
            pass
        return None

    base_path = os.path.join(os.path.dirname(__file__), "bin")
    system = platform.system().lower().replace("darwin", "macos")
    architecture = platform.machine().lower()

    if protocol == StreamingProtocol.RAOP:
        package = "cliraop"
    elif protocol == StreamingProtocol.AIRPLAY2:
        package = "cliap2"
    else:
        raise RuntimeError(f"Unsupported streaming protocol requested: {protocol}")

    if bridge_binary := await check_binary(
        os.path.join(base_path, f"{package}-{system}-{architecture}")
    ):
        return bridge_binary

    msg = (
        f"Unable to locate {protocol.name} CLI stream binary {package} for {system}/{architecture}"
    )
    raise RuntimeError(msg)


def get_ntp_timestamp() -> int:
    """
    Get current NTP timestamp (64-bit).

    Returns:
        int: 64-bit NTP timestamp (upper 32 bits = seconds, lower 32 bits = fraction)
    """
    # Get current Unix timestamp with microsecond precision
    current_time = time.time()

    # Split into seconds and microseconds
    seconds = int(current_time)
    microseconds = int((current_time - seconds) * 1_000_000)

    # Convert to NTP epoch (add offset from 1970 to 1900)
    ntp_seconds = seconds + NTP_EPOCH_DELTA

    # Convert microseconds to NTP fraction (2^32 parts per second)
    # fraction = (microseconds * 2^32) / 1_000_000
    ntp_fraction = int((microseconds << 32) / 1_000_000)

    # Combine into 64-bit value
    return (ntp_seconds << 32) | ntp_fraction


def ntp_to_seconds_fraction(ntp_timestamp: int) -> tuple[int, int]:
    """
    Split NTP timestamp into seconds and fraction components.

    Args:
        ntp_timestamp: 64-bit NTP timestamp

    Returns:
        tuple: (seconds, fraction)
    """
    seconds = ntp_timestamp >> 32
    fraction = ntp_timestamp & 0xFFFFFFFF
    return seconds, fraction


def ntp_to_unix_time(ntp_timestamp: int) -> float:
    """
    Convert NTP timestamp to Unix timestamp (float).

    Args:
        ntp_timestamp: 64-bit NTP timestamp

    Returns:
        float: Unix timestamp (seconds since 1970-01-01)
    """
    seconds = ntp_timestamp >> 32
    fraction = ntp_timestamp & 0xFFFFFFFF

    # Convert back to Unix epoch
    unix_seconds = seconds - NTP_EPOCH_DELTA

    # Convert fraction to microseconds
    microseconds = (fraction * 1_000_000) >> 32

    return unix_seconds + (microseconds / 1_000_000)


def unix_time_to_ntp(unix_timestamp: float) -> int:
    """
    Convert Unix timestamp (float) to NTP timestamp.

    Args:
        unix_timestamp: Unix timestamp (seconds since 1970-01-01)

    Returns:
        int: 64-bit NTP timestamp
    """
    seconds = int(unix_timestamp)
    microseconds = int((unix_timestamp - seconds) * 1_000_000)

    # Convert to NTP epoch
    ntp_seconds = seconds + NTP_EPOCH_DELTA

    # Convert microseconds to NTP fraction
    ntp_fraction = int((microseconds << 32) / 1_000_000)

    return (ntp_seconds << 32) | ntp_fraction


def add_seconds_to_ntp(ntp_timestamp: int, seconds: float) -> int:
    """
    Add seconds to an NTP timestamp.

    Args:
        ntp_timestamp: 64-bit NTP timestamp
        seconds: Number of seconds to add (can be fractional)

    Returns:
        int: New NTP timestamp with seconds added
    """
    # Extract whole seconds and fraction
    whole_seconds = int(seconds)
    fraction = seconds - whole_seconds

    # Convert to NTP format (upper 32 bits = seconds, lower 32 bits = fraction)
    ntp_seconds = whole_seconds << 32
    ntp_fraction = int(fraction * (1 << 32))

    return ntp_timestamp + ntp_seconds + ntp_fraction
