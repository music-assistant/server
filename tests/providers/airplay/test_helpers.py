"""Unit tests for AirPlay provider helpers."""

import plistlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError

from music_assistant.providers.airplay.helpers import (
    _CLI_BINARY_CHECK_TIMEOUT,
    default_buffer_depth,
    get_cli_binary,
    get_decoded_property,
    probe_audio_formats,
    serialize_txt_records,
    supports_airplay2,
    supports_companion_pairing,
)

ALAC_44100_24 = 1 << 19
ALAC_48000_24 = 1 << 21


def test_get_decoded_property_matches_case_insensitively() -> None:
    """TXT record keys resolve regardless of the casing advertised on the wire."""
    discovery_info = MagicMock()
    discovery_info.decoded_properties = {"rpFl": "0x367A2", "SystemBuildVersion": "21K69"}

    assert get_decoded_property(discovery_info, "rpFl") == "0x367A2"
    assert get_decoded_property(discovery_info, "rpfl") == "0x367A2"
    assert get_decoded_property(discovery_info, "systembuildversion") == "21K69"
    assert get_decoded_property(discovery_info, "missing") is None


@pytest.mark.parametrize(
    ("properties", "expected"),
    [
        # Apple TV advertises its flags under the mixed-case wire key "rpFl"
        ({"rpFl": "0x367A2"}, True),
        # HomePod: PIN pairing not supported
        ({"rpFl": "0x62792"}, False),
        # pairing explicitly disabled
        ({"rpFl": "0x367A6"}, False),
        ({"rpFl": "invalid"}, False),
        ({}, False),
    ],
)
def test_supports_companion_pairing(properties: dict[str, str], expected: bool) -> None:
    """Companion PIN pairing support is read from the wire-cased rpFl flags."""
    discovery_info = MagicMock()
    discovery_info.decoded_properties = properties
    assert supports_companion_pairing(discovery_info) is expected


def test_supports_companion_pairing_without_service() -> None:
    """A device without a Companion service is never pairable."""
    assert supports_companion_pairing(None) is False


@pytest.mark.parametrize(
    ("features_value", "expected"),
    [
        # Apple TV 4K: UnifiedMediaControl (38) and CoreUtils (48) set
        ("0x4A7FDFD5,0x3C177FDE", True),
        # Sonos: UnifiedMediaControl set
        ("0x445F8A00,0x1C340", True),
        # legacy RAOP-only receiver (single low field, bits 38/48 unreachable)
        ("0x5A7FFFF7", False),
        # high field present but neither AirPlay 2 bit set
        ("0xFFFFFFFF,0x2", False),
        # bit 48 (CoreUtils) alone is enough
        ("0x0,0x10000", True),
        # missing/garbage values never claim support
        (None, False),
        ("", False),
        ("not-a-number", False),
    ],
)
def test_supports_airplay2(features_value: str | None, expected: bool) -> None:
    """AirPlay 2 support is detected from the UnifiedMediaControl/CoreUtils feature bits."""
    assert supports_airplay2(features_value) is expected


def test_serialize_txt_records() -> None:
    """TXT records serialize to space-separated k=v pairs, skipping unsafe entries."""
    discovery_info = MagicMock()
    discovery_info.decoded_properties = {
        "features": "0x5A7FFFF7,0x1E",
        "flags": "0x4",
        "deviceid": "AA:BB:CC:DD:EE:FF",
        "manufacturer": "Acme, Inc.",  # value contains a space: skipped
        "odd key": "value",  # key contains a space: skipped
        "empty": None,  # zeroconf may decode a valueless entry as None: skipped
    }
    txt = serialize_txt_records(discovery_info)
    pairs = txt.split(" ")
    assert "features=0x5A7FFFF7,0x1E" in pairs
    assert "flags=0x4" in pairs
    assert "deviceid=AA:BB:CC:DD:EE:FF" in pairs
    assert len(pairs) == 3


@pytest.mark.parametrize(
    ("system", "machine", "expected_name"),
    [
        ("Linux", "arm64", "cliairplay-linux-aarch64"),
        ("Linux", "amd64", "cliairplay-linux-x86_64"),
        ("Darwin", "aarch64", "cliairplay-macos-arm64"),
        ("Darwin", "x86_64", "cliairplay-macos-x86_64"),
    ],
)
async def test_get_cli_binary_uses_release_asset_name(
    system: str,
    machine: str,
    expected_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Resolve platform aliases to the filename installed by development setup.

    The runtime lookup must use the exact release asset name for every supported alias.
    """
    check_output = AsyncMock(return_value=(0, b"cliairplay v0.1.0 check"))
    monkeypatch.setattr("music_assistant.providers.airplay.helpers.platform.system", lambda: system)
    monkeypatch.setattr(
        "music_assistant.providers.airplay.helpers.platform.machine", lambda: machine
    )
    monkeypatch.setattr("music_assistant.providers.airplay.helpers.check_output", check_output)

    result = await get_cli_binary()

    assert result.endswith(f"/bin/{expected_name}")
    check_output.assert_awaited_once_with(result, "--check", timeout=_CLI_BINARY_CHECK_TIMEOUT)


async def test_get_cli_binary_raises_when_check_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A binary that never answers --check surfaces as RuntimeError, not a hang."""
    check_output = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(
        "music_assistant.providers.airplay.helpers.platform.system", lambda: "Darwin"
    )
    monkeypatch.setattr(
        "music_assistant.providers.airplay.helpers.platform.machine", lambda: "aarch64"
    )
    monkeypatch.setattr("music_assistant.providers.airplay.helpers.check_output", check_output)

    with pytest.raises(RuntimeError, match="did not respond to --check"):
        await get_cli_binary()


@pytest.mark.parametrize(
    ("info", "expected_hires"),
    [
        # Apple TV: 24-bit listed for the buffered stream only
        (
            {
                "supportedAudioFormatsExtended": {
                    "audioStream": [11, 18, 22, 24],
                    "bufferStream": [19, 21, 22, 23],
                }
            },
            True,
        ),
        # Sonos: 16-bit only on both streams
        (
            {
                "supportedAudioFormatsExtended": {
                    "audioStream": [18, 22],
                    "bufferStream": [18, 22],
                }
            },
            False,
        ),
        # Mac: legacy mask for the realtime stream, extended table for the buffered one
        (
            {
                "supportedFormats": {"audioStream": 0x1440800},
                "supportedAudioFormatsExtended": {"bufferStream": [21, 22, 23]},
            },
            True,
        ),
        # receivers that publish no format tables at all
        ({"deviceid": "AA:BB:CC:DD:EE:FF"}, False),
    ],
)
async def test_probe_audio_formats(info: dict[str, Any], expected_hires: bool) -> None:
    """The advertised formats are read from the union of both /info stream tables."""
    mass = _mass_serving_info(info)

    formats = await probe_audio_formats(mass, "10.0.0.5", 7000)

    assert bool(formats & (ALAC_44100_24 | ALAC_48000_24)) is expected_hires
    mass.http_session.get.assert_called_once()
    assert mass.http_session.get.call_args.args[0] == "http://10.0.0.5:7000/info"


async def test_probe_audio_formats_ipv6_address() -> None:
    """An IPv6 receiver address is bracketed for the request URL."""
    mass = _mass_serving_info({})

    await probe_audio_formats(mass, "fe80::1", 7000)

    assert mass.http_session.get.call_args.args[0] == "http://[fe80::1]:7000/info"


@pytest.mark.parametrize(
    "response",
    [
        ClientError("unreachable"),
        TimeoutError,
        b"not-a-plist",
        404,
    ],
)
async def test_probe_audio_formats_failures(response: Any) -> None:
    """An unreachable, slow or nonsensical receiver reports no formats instead of raising."""
    if isinstance(response, int):
        mass = _mass_serving_info({}, status=response)
    elif isinstance(response, bytes):
        mass = _mass_serving_info({})
        mass.http_session.get.return_value.__aenter__.return_value.read = AsyncMock(
            return_value=response
        )
    else:
        mass = MagicMock()
        mass.http_session.get = MagicMock(side_effect=response)

    assert await probe_audio_formats(mass, "10.0.0.5", 7000) == 0


def _mass_serving_info(info: dict[str, Any], status: int = 200) -> MagicMock:
    """Build a mass mock whose http session serves the given /info plist."""
    response = MagicMock()
    response.status = status
    response.read = AsyncMock(return_value=plistlib.dumps(info, fmt=plistlib.FMT_BINARY))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    mass = MagicMock()
    mass.http_session.get = MagicMock(return_value=context)
    return mass


def test_default_buffer_depth_family_table() -> None:
    """The family table maps both LinkPlay generations to the deep queue."""
    # Newer platform: Linkplay named as manufacturer.
    assert (
        default_buffer_depth("Linkplay Technology Inc.", "WiiM Pro Receiver", "p20.4.8.814756")
        == 1750
    )
    # Older platform: OEM brand, the platform only marked in fv.
    assert default_buffer_depth("Edifier Inc", "Edifier MS50A", "p20.Linkplay.4.6.430230") == 1750
    assert default_buffer_depth("Sonos", "Era 100", "p20.96.0-79160") == 0
    assert default_buffer_depth("Apple Inc.", "AppleTV11,1", None) == 0
