"""Unit tests for AirPlay provider helpers."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.airplay.helpers import (
    get_cli_binary,
    get_decoded_property,
    serialize_txt_records,
    supports_airplay2,
    supports_companion_pairing,
)


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
    check_output.assert_awaited_once_with(result, "--check")
