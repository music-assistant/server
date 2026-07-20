"""Unit tests for the AirPlay stream CLI argument assembly."""

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.providers.airplay.constants import StreamingProtocol
from music_assistant.providers.airplay.stream import AirPlayStream

START_UNIX_MS = 1_750_000_000_000


def _make_player() -> MagicMock:
    """Build a mock AirPlay player with both discovery records present."""
    player = MagicMock()
    player.player_id = "apaabbccddeeff"
    player.display_name = "Player A"
    player.address = "192.168.1.50"
    player.protocol = StreamingProtocol.AIRPLAY2
    player.protocol_override = None
    player.latency_override_ms = 0
    player.volume_level = 40
    player.device_info.mac_address = "AA:BB:CC:DD:EE:FF"
    player.device_info.ip_address = "192.168.1.50"
    player.logger = logging.getLogger("test.airplay.player")
    player.config.get_value = MagicMock(return_value=None)

    airplay_info = MagicMock()
    airplay_info.port = 7000
    airplay_info.server = "playera.local."
    airplay_info.decoded_properties = {
        "features": "0x5A7FFFF7,0x1E",
        "flags": "0x4",
        "model": "Test1,1",
        "manufacturer": "Acme, Inc.",  # contains a space: must be skipped in --txt
    }
    player.airplay_discovery_info = airplay_info

    raop_info = MagicMock()
    raop_info.port = 5000
    raop_info.name = "AABBCCDDEEFF@Player A._raop._tcp.local."
    raop_info.decoded_properties = {"et": "0,4", "md": "0,1,2", "cn": "0,1"}
    player.raop_discovery_info = raop_info

    prov = MagicMock()
    prov.dacp_id = "ABCDEF0123456789"
    prov.ptp_daemon_running = True
    prov.logger = logging.getLogger("test.airplay.prov")
    prov.mass.streams.publish_ip = "192.168.1.99"
    player.provider = prov
    return player


async def _build_args(player: MagicMock) -> list[str]:
    """Build the CLI args for the given player with the externals patched out."""
    stream = AirPlayStream(player)
    with (
        patch(
            "music_assistant.providers.airplay.stream.get_cli_binary",
            return_value="/fake/cliairplay",
        ),
        patch(
            "music_assistant.providers.airplay.stream.resolve_if_ip",
            return_value="192.168.1.5",
        ),
    ):
        return await stream._build_cli_args(START_UNIX_MS)


def _arg_value(args: list[str], flag: str) -> Any:
    """Return the value following the given flag in the argument list."""
    return args[args.index(flag) + 1]


@pytest.mark.asyncio
async def test_cli_args_default_auto() -> None:
    """Default (no protocol override) passes --protocol auto with the full mDNS TXT."""
    player = _make_player()
    args = await _build_args(player)

    assert _arg_value(args, "--protocol") == "auto"
    assert _arg_value(args, "--start-unix-ms") == str(START_UNIX_MS)
    # legacy timing args are gone
    assert "--ntpstart" not in args
    assert "--wait" not in args
    # AirPlay 2 service is the connection target when it may be used
    assert _arg_value(args, "--port") == "7000"
    assert _arg_value(args, "--name") == "Player A"
    assert _arg_value(args, "--hostname") == "playera.local."
    # RAOP mDNS props still passed for the RAOP-based flows
    assert _arg_value(args, "--udn") == "AABBCCDDEEFF@Player A._raop._tcp.local."
    assert _arg_value(args, "--et") == "0,4"
    assert _arg_value(args, "--cn") == "0,1"
    # full TXT for route selection; pairs containing whitespace are skipped
    txt = _arg_value(args, "--txt")
    assert "features=0x5A7FFFF7,0x1E" in txt
    assert "flags=0x4" in txt
    assert "manufacturer" not in txt
    # default format
    assert _arg_value(args, "--samplerate") == "44100"
    assert _arg_value(args, "--bitdepth") == "16"
    # no explicit latency override configured
    assert "--latency" not in args
    # PTP daemon is running: stream attaches to the shared clock
    assert "--ptp-shared" in args
    # networking
    assert _arg_value(args, "--if") == "192.168.1.5"
    assert _arg_value(args, "--publish-ip") == "192.168.1.99"
    # positional args: device address + stdin
    assert args[-2:] == ["192.168.1.50", "-"]


@pytest.mark.asyncio
async def test_cli_args_raop_override() -> None:
    """A forced RAOP protocol targets the RAOP service and skips AP2-only args."""
    player = _make_player()
    player.protocol_override = StreamingProtocol.RAOP
    args = await _build_args(player)

    assert _arg_value(args, "--protocol") == "raop"
    assert _arg_value(args, "--port") == "5000"
    assert "--name" not in args
    assert "--hostname" not in args
    assert "--ptp-shared" not in args


@pytest.mark.asyncio
async def test_cli_args_airplay2_override() -> None:
    """A forced AirPlay 2 protocol targets the AirPlay service."""
    player = _make_player()
    player.protocol_override = StreamingProtocol.AIRPLAY2
    args = await _build_args(player)

    assert _arg_value(args, "--protocol") == "airplay2"
    assert _arg_value(args, "--port") == "7000"


@pytest.mark.asyncio
async def test_cli_args_no_ptp_shared_without_daemon() -> None:
    """--ptp-shared is only passed while the provider's PTP daemon is running."""
    player = _make_player()
    player.provider.ptp_daemon_running = False
    args = await _build_args(player)
    assert "--ptp-shared" not in args


@pytest.mark.asyncio
async def test_cli_args_latency_override() -> None:
    """An explicitly configured latency override is passed to the binary."""
    player = _make_player()
    player.latency_override_ms = 3000
    args = await _build_args(player)
    assert _arg_value(args, "--latency") == "3000"


@pytest.mark.asyncio
async def test_cli_args_hires_pcm_format() -> None:
    """A 24-bit stream passes --bitdepth 24 while the pipe carries s32le samples."""
    player = _make_player()
    hires_format = AudioFormat(content_type=ContentType.PCM_S32LE, sample_rate=48000, bit_depth=24)
    stream = AirPlayStream(player, pcm_format=hires_format)
    with (
        patch(
            "music_assistant.providers.airplay.stream.get_cli_binary",
            return_value="/fake/cliairplay",
        ),
        patch(
            "music_assistant.providers.airplay.stream.resolve_if_ip",
            return_value="192.168.1.5",
        ),
    ):
        args = await stream._build_cli_args(START_UNIX_MS)

    assert _arg_value(args, "--samplerate") == "48000"
    assert _arg_value(args, "--bitdepth") == "24"
    # the ffmpeg pipe format must be the 32-bit container (binary truncates to 24)
    assert stream.pcm_format.content_type == ContentType.PCM_S32LE


@pytest.mark.asyncio
async def test_cli_args_raop_only_device() -> None:
    """A device without an _airplay._tcp service targets the RAOP service, no --txt."""
    player = _make_player()
    player.airplay_discovery_info = None
    args = await _build_args(player)

    assert _arg_value(args, "--protocol") == "auto"
    assert _arg_value(args, "--port") == "5000"
    assert "--txt" not in args
    assert "--name" not in args


def test_parse_latency_status() -> None:
    """The [STATUS] latency line is parsed into the stream's latency attributes."""
    player = _make_player()
    stream = AirPlayStream(player)
    stream._parse_latency_status(
        "[STATUS] latency lead_ms=1750 device_min_frames=11025 device_max_frames=88200"
    )
    assert stream.latency_lead_ms == 1750
    assert stream.device_min_frames == 11025
    assert stream.device_max_frames == 88200


@pytest.mark.asyncio
async def test_wait_for_connection_pushes_metadata_immediately() -> None:
    """
    Track metadata is pushed the instant the device connects.

    Receivers that gate audio rendering on receiving timeline-anchored metadata
    (e.g. Sonos over native AirPlay 2) must not be left silent while a deferred
    push is pending, so the metadata callback runs synchronously on connect
    while only the volume resend stays on the delayed path.
    """
    player = _make_player()
    player.volume_muted = False
    stream = AirPlayStream(player)
    stream._connected.set()  # connection already established
    player.provider.mass.call_later = MagicMock()

    with (
        patch.object(stream, "_cli_proc", MagicMock()),  # non-None so the method proceeds
        patch.object(stream, "send_cli_command", return_value=None),  # avoid a real coroutine
    ):
        await stream.wait_for_connection()

    # Metadata pushed synchronously on connect...
    player._on_player_media_updated.assert_called_once_with()
    # ...and never routed through the delayed call_later path.
    deferred_callables = [call.args[1] for call in player.provider.mass.call_later.call_args_list]
    assert player._on_player_media_updated not in deferred_callables
    # The volume resend is still deferred (existing behavior preserved).
    assert player.provider.mass.call_later.call_count == 1
    assert player.provider.mass.call_later.call_args_list[0].args[0] == 2
