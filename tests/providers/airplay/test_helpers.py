"""Unit tests for AirPlay provider helpers."""

import plistlib
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientError

from music_assistant.providers.airplay.constants import (
    AIRPLAY_DISCOVERY_TYPE,
    RAOP_DISCOVERY_TYPE,
)
from music_assistant.providers.airplay.helpers import (
    _CLI_BINARY_CHECK_TIMEOUT,
    SetMembership,
    cli_clock_time,
    default_buffer_depth,
    get_cli_binary,
    get_decoded_property,
    get_model_info,
    is_apple_device,
    is_apple_tv,
    parse_set_membership,
    probe_audio_formats,
    serialize_txt_records,
    supports_airplay2,
    supports_companion_pairing,
)

ALAC_44100_24 = 1 << 19
ALAC_48000_24 = 1 << 21


def test_cli_clock_time_ignores_wall_clock_steps() -> None:
    """A wall clock step does not move the cliairplay timeline."""
    before = cli_clock_time()
    with (
        patch("time.time", return_value=time.time() + 3600),
        patch("time.time_ns", return_value=time.time_ns() + 3600 * 10**9),
    ):
        during = cli_clock_time()

    assert 0 <= during - before < 1


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
    """
    Every family defaults to Automatic since buffered became the auto-route.

    The LinkPlay generations that used to need a deepened realtime queue
    (WiiM 1750 ms, Edifier MS50A 2500 ms) manage their own buffer on the
    buffered stream; the per-player depth setting remains as the manual
    override for field reports.
    """
    assert (
        default_buffer_depth("Linkplay Technology Inc.", "WiiM Pro Receiver", "p20.4.8.814756") == 0
    )
    assert default_buffer_depth("Edifier Inc", "Edifier MS50A", "p20.Linkplay.4.6.430230") == 0
    assert default_buffer_depth("Sonos", "Era 100", "p20.96.0-79160") == 0
    assert default_buffer_depth("Apple Inc.", "AppleTV11,1", None) == 0


def test_home_accessory_is_recognised_as_apple_receiver() -> None:
    """HomeAccessory models (Apple HomeView) are Apple receivers but not Apple TVs."""
    info = MagicMock()
    info.decoded_properties = {"model": "HomeAccessory17,1"}
    manufacturer, model = get_model_info(info)
    assert (manufacturer, model) == ("Apple", "HomeView")
    assert is_apple_device(manufacturer, model)
    assert not is_apple_tv(manufacturer, model)


# The TXT keys of an Apple TV and its HomePod, idle and while senders play to the set
# (ids made up); gid, pgid, igl, gcgl and pgcgl change with a session.
_SET_ID = "3F2A9C1E-7B44-4D0E-9A61-2C8D5E7F1A30"
_SET_KEYS = {"psgid": _SET_ID, "psgsz": "2", "psgtp": "1", "pgm": "3", "gpn": "Study"}
_APPLE_TV_IDLE = {"gid": _SET_ID, "igl": "1", "gcgl": "1", "flags": "0x644", "pgmid": "0"}
_HOMEPOD_IDLE = {"gid": _SET_ID, "pgid": _SET_ID, "igl": "0", "flags": "0xa0c04", "pgmid": "1"}
_OUR_GROUP = "6D0A1F2B-3C4D-4E5F-8A9B-0C1D2E3F4A5B"
_APPLE_SENDER_GROUP = "9E8D7C6B-5A49-4382-9170-6F5E4D3C2B1A"


def _txt_record(service_type: str, properties: dict[str, str]) -> MagicMock:
    """Return a record of the given type that carries the given TXT keys."""
    return MagicMock(type=service_type, decoded_properties=properties)


@pytest.mark.parametrize(
    ("properties", "position"),
    [
        (_APPLE_TV_IDLE, 0),
        (_HOMEPOD_IDLE, 1),
        # our session to the HomePod
        ({**_HOMEPOD_IDLE, "gid": _OUR_GROUP, "pgid": _OUR_GROUP, "flags": "0x1a0c04"}, 1),
        # an Apple sender playing to the room
        (
            {**_APPLE_TV_IDLE, "gid": _APPLE_SENDER_GROUP, "pgid": _APPLE_SENDER_GROUP, "igl": "0"},
            0,
        ),
        ({**_HOMEPOD_IDLE, "gid": _APPLE_SENDER_GROUP, "pgid": _APPLE_SENDER_GROUP}, 1),
    ],
)
def test_the_set_of_a_device_stays_the_same_whoever_plays_to_it(
    properties: dict[str, str], position: int
) -> None:
    """The set is read from the keys that stay put, whatever session the devices are in."""
    record = _txt_record(AIRPLAY_DISCOVERY_TYPE, {**_SET_KEYS, **properties})

    assert parse_set_membership(record) == SetMembership(_SET_ID, position, 2, "Study")


@pytest.mark.parametrize(
    "record",
    [
        # an Apple TV without a set, and a speaker of another brand
        _txt_record(AIRPLAY_DISCOVERY_TYPE, {"flags": "0x644", "model": "AppleTV14,1"}),
        _txt_record(AIRPLAY_DISCOVERY_TYPE, {"gid": "AA:BB:CC:00:22:01", "manufacturer": "Sonos"}),
        # the RAOP record, the same keys under another type, a set of another kind
        _txt_record(RAOP_DISCOVERY_TYPE, {**_SET_KEYS, **_HOMEPOD_IDLE}),
        _txt_record(AIRPLAY_DISCOVERY_TYPE, {**_SET_KEYS, **_HOMEPOD_IDLE, "psgtp": "2"}),
        _txt_record(AIRPLAY_DISCOVERY_TYPE, {**_SET_KEYS, "pgmid": "x"}),
        None,
    ],
)
def test_a_device_outside_a_set_has_no_place_in_one(record: MagicMock | None) -> None:
    """Only an _airplay record with the keys of an Apple TV's set names a place in one."""
    assert parse_set_membership(record) is None
