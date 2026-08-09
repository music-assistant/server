"""Various helpers/utilities for the AirPlay provider."""

from __future__ import annotations

import logging
import os
import platform
import plistlib
import re
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.process import check_output
from music_assistant.helpers.util import format_ip_for_url

from .constants import AIRPLAY_BUFFER_DEPTH_DEFAULTS

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

    from music_assistant.mass import MusicAssistant

_LOGGER = logging.getLogger(__name__)
_COMPANION_PAIRING_DISABLED = 0x04
_COMPANION_PAIRING_WITH_PIN = 0x4000
# Bound the binary's `--check` probe. It normally answers instantly, but the first
# execution of a freshly-fetched binary can stall (e.g. macOS Gatekeeper verification
# of an unsigned download), and a wedged binary would otherwise block provider load or
# a stream start indefinitely.
_CLI_BINARY_CHECK_TIMEOUT = 15.0
# Bound the /info capability probe: it runs in the discovery path, and a receiver
# that is slow to answer must not hold up player registration.
_INFO_PROBE_TIMEOUT = 5.0


def convert_airplay_volume(value: float) -> int:
    """Remap AirPlay dB volume (-30..0) to 0..100 scale."""
    airplay_min = -30.0
    airplay_max = 0.0
    value = max(airplay_min, min(airplay_max, value))
    portion = (value - airplay_min) * 100.0 / (airplay_max - airplay_min)
    return max(0, min(100, round(portion)))


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


def parse_airplay_features(features_value: str | None) -> int:
    """Return an AirPlay features bitmask, or zero for an invalid value."""
    if not features_value:
        return 0
    try:
        parts = features_value.split(",")
        features = int(parts[0], 16)
        if len(parts) > 1:
            features |= int(parts[1], 16) << 32
    except TypeError, ValueError:
        return 0
    return features


def supports_airplay2(features_value: str | None) -> bool:
    """
    Check if a device advertises AirPlay 2 support in its features bitmask.

    :param features_value: Raw features value from the mDNS TXT records
        (``features`` on the _airplay service or ``ft`` on the _raop service),
        formatted as ``0xLOW`` or ``0xLOW,0xHIGH``.
    """
    features = parse_airplay_features(features_value)
    # SupportsUnifiedMediaControl (bit 38) / SupportsCoreUtilsPairingAndEncryption
    # (bit 48): either one means the device speaks AirPlay 2. This mirrors the
    # test the cliairplay binary uses for its automatic route selection.
    return bool((features >> 38) & 1 or (features >> 48) & 1)


def is_apple_device(manufacturer: str, model: str) -> bool:
    """
    Check if a device is a (standalone) Apple device with native AirPlay support.

    Apple devices (HomePod, Apple TV) have native AirPlay support
    and should be exposed as PlayerType.PLAYER.
    We don't include MacBooks etc. here as they are not standalone devices
    and may also be used for other protocols.
    """
    return manufacturer.lower().startswith("apple") and (
        "homepod" in model.lower() or "apple tv" in model.lower()
    )


def is_macos_device(manufacturer: str, model: str) -> bool:
    """Return whether an AirPlay device identifies as a Mac."""
    return manufacturer.lower().startswith("apple") and model.lower().startswith(("mac", "imac"))


def is_apple_tv(manufacturer: str, model: str) -> bool:
    """
    Check if a device identifies as an Apple TV (and not a HomePod).

    Only Apple TVs run the tvOS dashboard app, so this narrows :func:`is_apple_device`
    to the Apple TV family. The model strings come from :func:`get_model_info`
    (e.g. "Apple TV 4K", "Apple TV Gen4").
    """
    return manufacturer.lower().startswith("apple") and "apple tv" in model.lower()


def default_buffer_depth(manufacturer: str, model: str, fv: str | None) -> int:
    """
    Return the default receiver buffer depth in ms for a device, 0 for automatic.

    :param manufacturer: Device manufacturer from discovery.
    :param model: Device model from discovery.
    :param fv: The device's _airplay fv (firmware) TXT record, when known.
    """
    for manufacturer_match, model_match, fv_match, depth_ms in AIRPLAY_BUFFER_DEPTH_DEFAULTS:
        # fnmatchcase with both sides lowered: plain fnmatch only normalizes
        # case on case-insensitive platforms, so a capitalized table row would
        # match on macOS and silently fail on Linux.
        if (
            fnmatchcase(manufacturer.lower(), manufacturer_match.lower())
            and fnmatchcase(model.lower(), model_match.lower())
            and fnmatchcase((fv or "").lower(), fv_match.lower())
        ):
            return depth_ms
    return 0


def get_decoded_property(discovery_info: AsyncServiceInfo, key: str) -> str | None:
    """
    Return an mDNS TXT property value by case-insensitive key.

    TXT record keys are case-insensitive (RFC 6763) and zeroconf preserves the
    casing as advertised on the wire, which differs per device (e.g. Companion
    services advertise ``rpFl``, MRP services ``SystemBuildVersion``).

    :param discovery_info: The mDNS service info to read the property from.
    :param key: The TXT record key to look up (any casing).
    """
    decoded_properties = discovery_info.decoded_properties
    if (value := decoded_properties.get(key)) is not None:
        return value
    folded_key = key.casefold()
    for prop_key, prop_value in decoded_properties.items():
        if prop_key.casefold() == folded_key:
            return prop_value
    return None


def supports_companion_pairing(discovery_info: AsyncServiceInfo | None) -> bool:
    """Return whether a Companion service supports PIN pairing."""
    if discovery_info is None:
        return False
    raw_flags = get_decoded_property(discovery_info, "rpFl")
    if raw_flags is None:
        return False
    try:
        flags = int(raw_flags, 16)
    except TypeError, ValueError:
        return False
    return bool(flags & _COMPANION_PAIRING_WITH_PIN) and not bool(
        flags & _COMPANION_PAIRING_DISABLED
    )


def supports_mrp_tunnel(discovery_info: AsyncServiceInfo | None) -> bool:
    """Return whether an AirPlay service advertises tunneled MRP control."""
    if discovery_info is None:
        return False
    features = parse_airplay_features(
        discovery_info.decoded_properties.get("features")
        or discovery_info.decoded_properties.get("ft")
    )
    return bool((features >> 58) & 1)


def supports_transient_mrp(discovery_info: AsyncServiceInfo | None) -> bool:
    """Return whether an AirPlay MRP tunnel supports transient authentication."""
    if not supports_mrp_tunnel(discovery_info):
        return False
    assert discovery_info is not None
    features = parse_airplay_features(
        discovery_info.decoded_properties.get("features")
        or discovery_info.decoded_properties.get("ft")
    )
    return bool((features >> 43) & 1 or (features >> 48) & 1)


def supports_mrp_service(discovery_info: AsyncServiceInfo | None) -> bool:
    """Return whether a native MRP service is usable."""
    if discovery_info is None or discovery_info.port is None:
        return False
    build = get_decoded_property(discovery_info, "SystemBuildVersion") or ""
    match = re.match(r"^(\d+)[A-Z]", build)
    return match is None or int(match.group(1)) < 19


async def probe_audio_formats(mass: MusicAssistant, host: str, port: int) -> int:
    """
    Return the audio formats an AirPlay 2 receiver advertises, as a bitmask.

    Zero when the device is unreachable or publishes no format tables.

    :param mass: The MusicAssistant instance.
    :param host: Address of the receiver.
    :param port: Port of the receiver's _airplay._tcp service.
    """
    # The tables live in the receiver's /info response, which is served
    # unauthenticated, so this needs no pairing or credentials.
    url = f"http://{format_ip_for_url(host)}:{port}/info"
    try:
        async with mass.http_session.get(
            url, timeout=ClientTimeout(total=_INFO_PROBE_TIMEOUT)
        ) as resp:
            if resp.status != 200:
                return 0
            info = plistlib.loads(await resp.read())
    except ClientError, TimeoutError, plistlib.InvalidFileException, ValueError:
        return 0
    return _parse_format_tables(info) if isinstance(info, dict) else 0


async def get_cli_binary() -> str:
    """
    Find the cliairplay binary for the current platform.

    :raises RuntimeError: If the binary cannot be found.
    """
    system = platform.system()
    architecture = platform.machine()
    binary_name = _get_cli_binary_name(system, architecture)
    if binary_name is None:
        msg = f"Unsupported cliairplay platform: {system.lower()}/{architecture.lower()}"
        raise RuntimeError(msg)
    base_path = os.path.join(os.path.dirname(__file__), "bin")
    binary_path = os.path.join(base_path, binary_name)

    try:
        returncode, output = await check_output(
            binary_path, "--check", timeout=_CLI_BINARY_CHECK_TIMEOUT
        )
        output_str = output.strip().decode()
        if returncode == 0 and "cliairplay" in output_str and "check" in output_str:
            return binary_path
    except TimeoutError:
        msg = (
            f"{binary_name} did not respond to --check within "
            f"{_CLI_BINARY_CHECK_TIMEOUT:.0f}s (first-run verification or a wedged binary)"
        )
        raise RuntimeError(msg) from None
    except OSError:
        pass

    msg = f"Unable to locate {binary_name} for {system.lower()}/{architecture.lower()}"
    raise RuntimeError(msg)


def player_id_to_mac_address(player_id: str) -> str:
    """Convert a player_id to a MAC address-like string."""
    # the player_id is the mac address prefixed with "ap"
    hex_str = player_id.replace("ap", "").upper()
    return ":".join(hex_str[i : i + 2] for i in range(0, 12, 2))


def generate_active_remote_id(mac_address: str) -> str:
    """
    Generate an Active-Remote ID for DACP communication.

    The Active-Remote ID is used to match DACP callbacks from devices to the
    correct stream. This function generates a consistent ID based on the
    player_id (=macaddress, =device id), converted to uint32).

    :return: Active-Remote ID as decimal string.
    """
    # Convert MAC address format to uint32
    # Remove colons: "AA:BB:CC:DD:EE:FF" -> "AABBCCDDEEFF"
    hex_str = mac_address.replace(":", "").upper()
    # Parse as uint64 and truncate to uint32 (lower 32 bits)
    device_id_u64 = int(hex_str, 16)
    device_id_u32 = device_id_u64 & 0xFFFFFFFF
    return str(device_id_u32)


def serialize_txt_records(discovery_info: AsyncServiceInfo) -> str:
    """
    Serialize mDNS TXT records for cliairplay's --txt argument.

    The binary receives the full _airplay._tcp TXT as a single
    space-separated "key=value key=value ..." argument and uses it for
    automatic route selection (RAOP vs AirPlay 2, native vs RAOP-compat,
    PTP vs NTP). Pairs containing whitespace are skipped as the binary
    splits the blob on spaces.

    :param discovery_info: The _airplay._tcp discovery info of the device.
    """
    pairs: list[str] = []
    for key, value in discovery_info.decoded_properties.items():
        if value is None:
            continue
        if any(char.isspace() for char in key) or any(char.isspace() for char in value):
            continue
        pairs.append(f"{key}={value}")
    return " ".join(pairs)


def get_final_output_format(audio_format: AudioFormat) -> AudioFormat:
    """
    Determine the output format ffmpeg must encode to for the cliairplay binary.

    The cliairplay binary always uses ALAC encoding internally.
    """
    return AudioFormat(
        content_type=ContentType.ALAC,
        sample_rate=audio_format.sample_rate,
        bit_depth=audio_format.bit_depth,
        channels=audio_format.channels,
    )


def _parse_format_tables(info: dict[str, Any]) -> int:
    """Return the union of the format tables in a receiver's /info response."""
    # Each stream advertises its formats either as a list of bit indices in
    # supportedAudioFormatsExtended, or as a plain mask in the older
    # supportedFormats. A device can use a different shape per stream.
    extended = info.get("supportedAudioFormatsExtended")
    legacy = info.get("supportedFormats")
    formats = 0
    for stream in ("audioStream", "bufferStream"):
        if isinstance(extended, dict) and isinstance(bits := extended.get(stream), list):
            for bit in bits:
                if isinstance(bit, int) and 0 <= bit < 64:
                    formats |= 1 << bit
        elif isinstance(legacy, dict) and isinstance(mask := legacy.get(stream), int):
            formats |= mask
    return formats


def _get_cli_binary_name(system: str, machine: str) -> str | None:
    """Return the cliairplay release asset name for a platform."""
    normalized_system = system.lower().replace("darwin", "macos")
    normalized_machine = machine.lower()

    if normalized_machine in ("amd64", "x86_64"):
        architecture = "x86_64"
    elif normalized_machine in ("aarch64", "arm64"):
        architecture = "arm64" if normalized_system == "macos" else "aarch64"
    else:
        return None
    if normalized_system not in ("linux", "macos"):
        return None
    return f"cliairplay-{normalized_system}-{architecture}"
