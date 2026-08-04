"""Unit tests for the AirPlay stream CLI argument assembly."""

import asyncio
import errno
import logging
import os
import threading
from collections.abc import AsyncGenerator, Callable, Coroutine
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from music_assistant_models.enums import ContentType, PlaybackState
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.providers.airplay.constants import (
    AIRPLAY_ARTWORK_SIZE,
    AIRPLAY_JOIN_START_ACK_TIMEOUT_MS,
    AIRPLAY_START_ACK_TIMEOUT_MS,
    CONF_AIRPLAY_CREDENTIALS,
    CONF_ENCRYPTION,
    CONF_PASSWORD,
    AirPlayRemoteCommand,
    ClockReadiness,
    StreamingProtocol,
)
from music_assistant.providers.airplay.stream import AirPlayStream, CliError

START_UNIX_MS = 1_750_000_000_000
AP2_FEATURES = "0x4A7FDFD5,0x3C177FDE"


def _make_player() -> MagicMock:
    """Build a mock AirPlay player with both discovery records present."""
    player = MagicMock()
    player.player_id = "apaabbccddeeff"
    player.display_name = "Player A"
    player.address = "192.168.1.50"
    player.protocol = StreamingProtocol.AIRPLAY2
    player.protocol_override = None
    player.volume_level = 40
    player.device_info.mac_address = "AA:BB:CC:DD:EE:FF"
    player.device_info.ip_address = "192.168.1.50"
    player.logger = logging.getLogger("test.airplay.player")
    player.config.get_value = MagicMock(side_effect=lambda _key, default=None: default)
    player.state.active_group = None

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
    # auto-detected publish ip: reachable address of this host, but not the interface
    # the stream to this device leaves from - so it is never handed to the binary
    prov.mass.streams.publish_ip = "192.168.1.99"
    prov.mass.streams.get_source_ip = AsyncMock(return_value="192.168.1.5")
    prov.mass.streams.get_publish_ip = MagicMock(return_value=None)
    player.provider = prov
    return player


async def _build_args(player: MagicMock) -> list[str]:
    """Build the CLI args for the given player with the externals patched out."""
    stream = AirPlayStream(player)
    with patch(
        "music_assistant.providers.airplay.stream.get_cli_binary",
        return_value="/fake/cliairplay",
    ):
        return await stream._build_cli_args()


def _arg_value(args: list[str], flag: str) -> Any:
    """Return the value following the given flag in the argument list."""
    return args[args.index(flag) + 1]


def _acking_write_cli_command(
    stream: AirPlayStream, at_unix_ms: int | None = None
) -> Callable[[str], Coroutine[Any, Any, bool]]:
    """
    Build a ``_write_cli_command`` replacement that acks a START like the binary.

    :param stream: The stream whose START commands are answered.
    :param at_unix_ms: Scheduled audible instant to report back; defaults to the
        commanded instant, i.e. the binary found it feasible.
    """

    async def write_command(command: str) -> bool:
        # A START nothing acks otherwise pays the real ack timeout in wall clock;
        # feeding the ack here leaves start()'s wait already released.
        if "ACTION=START" in command:
            requested = int(command.split("START_UNIX_MS=")[1].split("\n", 1)[0])
            stream._handle_status_line(
                f"[STATUS] started requested_unix_ms={requested} "
                f"at_unix_ms={requested if at_unix_ms is None else at_unix_ms}"
            )
        return True

    return write_command


@pytest.mark.asyncio
async def test_cli_args_default_auto() -> None:
    """Default (no protocol override) passes --protocol auto with the full mDNS TXT."""
    player = _make_player()
    args = await _build_args(player)

    assert _arg_value(args, "--protocol") == "auto"
    assert "--start-unix-ms" not in args
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
    # networking: the interface the timing packets leave from is pinned
    assert _arg_value(args, "--if") == "192.168.1.5"
    assert "--publish-ip" not in args
    # the target is the only positional argument; PREPARE selects stdin
    assert args[-1] == "192.168.1.50"
    assert "-" not in args
    assert "--cmdpipe" in args


@pytest.mark.asyncio
async def test_cli_args_auto_publish_ip_is_never_advertised() -> None:
    """
    An auto-detected publish IP must not reach --publish-ip.

    The binary treats it as authoritative for the PTP timing-peer list, while the
    timing packets leave from the resolved --if interface. A peer list naming any
    other address makes the receiver discard our clock and play silence.
    """
    player = _make_player()
    player.provider.mass.streams.publish_ip = "10.45.0.20"

    args = await _build_args(player)

    assert _arg_value(args, "--if") == "192.168.1.5"
    assert "--publish-ip" not in args
    assert "10.45.0.20" not in args


@pytest.mark.asyncio
async def test_cli_args_configured_publish_ip_is_advertised() -> None:
    """An explicitly configured publish IP is a reachability statement and is passed on."""
    player = _make_player()
    player.provider.mass.streams.get_publish_ip = MagicMock(return_value="10.45.0.20")

    args = await _build_args(player)

    assert _arg_value(args, "--publish-ip") == "10.45.0.20"
    player.provider.mass.streams.get_publish_ip.assert_called_once_with("192.168.1.50")


@pytest.mark.asyncio
async def test_cli_args_publish_ip_omitted_when_it_matches_the_interface() -> None:
    """A publish IP identical to the bound interface adds nothing to the peer list."""
    player = _make_player()
    player.provider.mass.streams.get_publish_ip = MagicMock(return_value="192.168.1.5")

    args = await _build_args(player)

    assert _arg_value(args, "--if") == "192.168.1.5"
    assert "--publish-ip" not in args


@pytest.mark.asyncio
async def test_cli_args_no_interface_pin_leaves_routing_to_the_binary() -> None:
    """With no interface to pin, --if is dropped so the routing table decides."""
    player = _make_player()
    player.provider.mass.streams.get_source_ip = AsyncMock(return_value=None)

    args = await _build_args(player)

    assert "--if" not in args


@pytest.mark.asyncio
async def test_cli_args_log_the_pinned_interface(caplog: pytest.LogCaptureFixture) -> None:
    """The resolved interface is stated outright, so a user's log shows what it bound to."""
    player = _make_player()

    with caplog.at_level(logging.DEBUG):
        await _build_args(player)

    assert (
        "cliairplay network binding for player apaabbccddeeff: "
        "if=192.168.1.5 publish_ip=<not configured>" in caplog.text
    )


@pytest.mark.asyncio
async def test_cli_args_log_the_unpinned_interface_and_publish_ip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unpinned interface and a configured publish IP are named for what they are."""
    player = _make_player()
    player.provider.mass.streams.get_source_ip = AsyncMock(return_value=None)
    player.provider.mass.streams.get_publish_ip = MagicMock(return_value="10.45.0.20")

    with caplog.at_level(logging.DEBUG):
        await _build_args(player)

    assert (
        "cliairplay network binding for player apaabbccddeeff: "
        "if=<all interfaces> publish_ip=10.45.0.20" in caplog.text
    )


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
    assert "--encrypt" in args


@pytest.mark.asyncio
async def test_cli_args_raop_encryption_can_be_disabled() -> None:
    """The legacy encryption preference remains available for incompatible receivers."""
    player = _make_player()
    player.protocol_override = StreamingProtocol.RAOP
    player.config.get_value = MagicMock(
        side_effect=lambda key, default=None: False if key == CONF_ENCRYPTION else default
    )

    args = await _build_args(player)

    assert "--encrypt" not in args


@pytest.mark.asyncio
async def test_cli_args_no_ptp_shared_without_daemon() -> None:
    """--ptp-shared is only passed while the provider's PTP daemon is running."""
    player = _make_player()
    player.provider.ptp_daemon_running = False
    args = await _build_args(player)
    assert "--ptp-shared" not in args


@pytest.mark.asyncio
async def test_cli_args_no_latency_override() -> None:
    """The playback lead/buffer is binary-managed; MA never passes --latency."""
    player = _make_player()
    args = await _build_args(player)
    assert "--latency" not in args


@pytest.mark.asyncio
async def test_cli_args_hires_pcm_format() -> None:
    """A 24-bit stream passes --bitdepth 24 while the pipe carries s32le samples."""
    player = _make_player()
    hires_format = AudioFormat(content_type=ContentType.PCM_S32LE, sample_rate=48000, bit_depth=24)
    stream = AirPlayStream(player, pcm_format=hires_format)
    with patch(
        "music_assistant.providers.airplay.stream.get_cli_binary",
        return_value="/fake/cliairplay",
    ):
        args = await stream._build_cli_args()

    assert _arg_value(args, "--samplerate") == "48000"
    assert _arg_value(args, "--bitdepth") == "24"
    # the ffmpeg pipe format must be the 32-bit container (binary truncates to 24)
    assert stream.pcm_format.content_type == ContentType.PCM_S32LE


@pytest.mark.asyncio
async def test_cli_args_raop_only_device() -> None:
    """True legacy RAOP uses the command pipe and has no positional audio source."""
    player = _make_player()
    player.airplay_discovery_info = None
    player.protocol = StreamingProtocol.RAOP
    args = await _build_args(player)

    assert _arg_value(args, "--protocol") == "auto"
    assert _arg_value(args, "--port") == "5000"
    assert "--txt" not in args
    assert "--name" not in args
    assert "--encrypt" in args
    assert "--cmdpipe" in args
    assert args[-1] == "192.168.1.50"
    assert "-" not in args


@pytest.mark.asyncio
async def test_cli_args_auto_raop_uses_raop_service_port() -> None:
    """Auto-selected legacy RAOP targets the RAOP service rather than the AP2 service."""
    player = _make_player()
    player.protocol = StreamingProtocol.RAOP
    player.airplay_discovery_info.decoded_properties["features"] = "0x0"

    args = await _build_args(player)

    assert _arg_value(args, "--protocol") == "auto"
    assert _arg_value(args, "--port") == "5000"
    assert "--name" not in args


@pytest.mark.asyncio
async def test_cli_args_pass_raop_feature_fallback_to_auto_router() -> None:
    """AP2 bits advertised only on _raop.ft still reach the binary route resolver."""
    player = _make_player()
    player.airplay_discovery_info.decoded_properties.pop("features")
    player.raop_discovery_info.decoded_properties["ft"] = AP2_FEATURES

    args = await _build_args(player)

    assert _arg_value(args, "--protocol") == "auto"
    assert _arg_value(args, "--port") == "7000"
    assert f"ft={AP2_FEATURES}" in _arg_value(args, "--txt")


@pytest.mark.asyncio
async def test_cli_args_featureless_ap2_only_device_forces_airplay2() -> None:
    """An AP2-only receiver without feature bits cannot fall through to legacy RAOP."""
    player = _make_player()
    player.raop_discovery_info = None
    player.airplay_discovery_info.decoded_properties = {}

    args = await _build_args(player)

    assert _arg_value(args, "--protocol") == "airplay2"
    assert _arg_value(args, "--port") == "7000"


@pytest.mark.parametrize(
    ("line", "expected_route"),
    [
        (
            "[STATUS] route protocol=airplay2 flow=realtime timing=ptp buffered=0",
            "AirPlay 2 (realtime, PTP)",
        ),
        # a buffered stream is named by its delivery, whatever flow it was requested as
        (
            "[STATUS] route protocol=airplay2 flow=realtime timing=ntp buffered=1",
            "AirPlay 2 (buffered, NTP)",
        ),
        (
            "[STATUS] route protocol=raop flow=realtime timing=ntp buffered=0",
            "RAOP",
        ),
    ],
    ids=["airplay2-realtime", "airplay2-buffered", "raop"],
)
def test_parse_route_status(
    line: str, expected_route: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The [STATUS] route line resolves the route this stream took and reports it."""
    player = _make_player()
    stream = AirPlayStream(player)

    with caplog.at_level(logging.INFO):
        stream._parse_route_status(line)

    assert stream.active_route == expected_route
    assert f"Streaming to Player A via {expected_route}" in caplog.text


@pytest.mark.asyncio
async def test_stdout_reader_dispatches_the_route_line() -> None:
    """The CLI stdout reader hands the [STATUS] route line to the route parser."""
    player = _make_player()
    stream = AirPlayStream(player)
    process = MagicMock()
    process.read = AsyncMock(
        side_effect=[
            b"[STATUS] route protocol=airplay2 flow=realtime timing=ptp buffered=0\n",
            b"",
        ]
    )
    stream._cli_proc = process

    await stream._stdout_reader()

    assert stream.active_route == "AirPlay 2 (realtime, PTP)"


def test_parse_latency_status() -> None:
    """The [STATUS] latency line is parsed into the stream's latency attributes."""
    player = _make_player()
    stream = AirPlayStream(player)
    stream._parse_latency_status(
        "[STATUS] latency lead_ms=1750 device_min_frames=11025 device_max_frames=88200 "
        "warm_lead_ms=1200"
    )
    assert stream.latency_lead_ms == 1750
    assert stream.device_min_frames == 11025
    assert stream.device_max_frames == 88200
    assert stream.warm_lead_ms == 1200


def test_parse_latency_status_reads_every_field_on_its_own() -> None:
    """One unusable value must not leave the fields after it stale from the last report."""
    player = _make_player()
    stream = AirPlayStream(player)
    stream._parse_latency_status(
        "[STATUS] latency lead_ms=1750 device_min_frames=11025 device_max_frames=88200 "
        "warm_lead_ms=1200"
    )

    stream._parse_latency_status(
        "[STATUS] latency lead_ms=900 device_min_frames=garbage device_max_frames=44100 "
        "warm_lead_ms=300"
    )

    assert stream.latency_lead_ms == 900
    assert stream.device_min_frames == 0  # unusable, so unreported
    assert stream.device_max_frames == 44100
    assert stream.warm_lead_ms == 300


def test_parse_latency_status_logs_the_warm_lead(caplog: pytest.LogCaptureFixture) -> None:
    """The warm lead drives every warm group anchor, so it belongs in the line."""
    stream = AirPlayStream(_make_player())

    with caplog.at_level(logging.DEBUG):
        stream._parse_latency_status(
            "[STATUS] latency lead_ms=1750 device_min_frames=11025 device_max_frames=88200 "
            "warm_lead_ms=1200"
        )

    assert "warm lead=1200ms" in caplog.text


def test_mrp_push_accepted_is_not_logged_at_info(caplog: pytest.LogCaptureFixture) -> None:
    """A push the device accepted is bookkeeping, not something to act on."""
    stream = AirPlayStream(_make_player())

    with caplog.at_level(logging.INFO):
        stream._parse_mrp_status("[STATUS] mrp path=command status=200")

    assert caplog.text == ""


@pytest.mark.parametrize("status", [302, 403, 500], ids=["redirect", "forbidden", "server-error"])
def test_mrp_push_rejection_is_reported(status: int, caplog: pytest.LogCaptureFixture) -> None:
    """Anything but a 2xx is the device not taking the push, so it is reported."""
    stream = AirPlayStream(_make_player())

    with caplog.at_level(logging.WARNING):
        stream._parse_mrp_status(f"[STATUS] mrp path=command status={status}")

    assert "Player A" in caplog.text
    assert str(status) in caplog.text


def test_mrp_artwork_rejection_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    """An artwork rejection carries no path= or status=, and must not read as a plain push."""
    stream = AirPlayStream(_make_player())

    with caplog.at_level(logging.WARNING):
        stream._parse_mrp_status(
            "[STATUS] mrp artwork=rejected reason=progressive_jpeg bytes=48123 "
            "width=512 height=512 precision=8 sof=0xc2 components=3 progressive=1 "
            "clear_status=200 staging_max_bytes=131072"
        )

    assert "rejected the now-playing artwork" in caplog.text
    assert "progressive_jpeg" in caplog.text
    assert "HTTP ?" not in caplog.text


def test_mrp_channel_status_is_not_read_as_an_http_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The data-channel line reports 0/1, which must not be warned about as a failed push."""
    stream = AirPlayStream(_make_player())

    with caplog.at_level(logging.WARNING):
        stream._parse_mrp_status("[STATUS] mrp path=channel status=0")

    assert caplog.text == ""


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # both tables published: the union is kept
        (
            "[STATUS] capabilities requested=0x80000 realtime_formats=0x40000 "
            "realtime_known=1 buffered_formats=0x80000 buffered_known=1",
            0xC0000,
        ),
        # the Apple TV publishes 24-bit for its buffered stream only
        (
            "[STATUS] capabilities requested=0x80000 realtime_formats=0x1440800 "
            "realtime_known=1 buffered_formats=0xe80000 buffered_known=1",
            0x1EC0800,
        ),
        # a table the device did not publish is ignored, even when non-zero
        (
            "[STATUS] capabilities requested=0x40000 realtime_formats=0x40000 "
            "realtime_known=1 buffered_formats=0x80000 buffered_known=0",
            0x40000,
        ),
        # RAOP-compat routes report nothing: the probed value survives
        (
            "[STATUS] capabilities requested=0x40000 realtime_formats=0x0 "
            "realtime_known=0 buffered_formats=0x0 buffered_known=0",
            0x1234,
        ),
        # a malformed mask is ignored rather than raising
        (
            "[STATUS] capabilities realtime_formats=nonsense realtime_known=1",
            0x1234,
        ),
    ],
)
def test_parse_capabilities_status(line: str, expected: int) -> None:
    """The [STATUS] capabilities line refreshes the formats the player advertises."""
    player = _make_player()
    player.advertised_audio_formats = 0x1234
    stream = AirPlayStream(player)

    stream._parse_capabilities_status(line)

    assert player.advertised_audio_formats == expected


@pytest.mark.parametrize(
    ("value", "command"),
    [
        ("play", AirPlayRemoteCommand.PLAY),
        ("pause", AirPlayRemoteCommand.PAUSE),
        ("play_pause", AirPlayRemoteCommand.PLAY_PAUSE),
        ("next", AirPlayRemoteCommand.NEXT),
        ("previous", AirPlayRemoteCommand.PREVIOUS),
    ],
)
def test_parse_remote_event(value: str, command: AirPlayRemoteCommand) -> None:
    """A normalized CLI remote event is dispatched against its own player."""
    player = _make_player()
    stream = AirPlayStream(player)

    stream._parse_remote_event(f"[EVENT] remote command={value}")

    player.provider.handle_remote_command.assert_called_once_with(player, command)


def test_parse_remote_event_rejects_unknown_command(caplog: pytest.LogCaptureFixture) -> None:
    """An unknown CLI remote event is reported and ignored."""
    player = _make_player()
    stream = AirPlayStream(player)

    with caplog.at_level(logging.WARNING):
        stream._parse_remote_event("[EVENT] remote command=unsupported")

    player.provider.handle_remote_command.assert_not_called()
    assert "Ignoring unknown cliairplay remote command: unsupported" in caplog.text


@pytest.mark.asyncio
async def test_stdout_reader_dispatches_remote_events_once() -> None:
    """The CLI stdout reader dispatches each normalized remote event exactly once."""
    player = _make_player()
    stream = AirPlayStream(player)
    process = MagicMock()
    output = "".join(f"[EVENT] remote command={command}\n" for command in AirPlayRemoteCommand)
    process.read = AsyncMock(side_effect=[output.encode(), b""])
    stream._cli_proc = process

    await stream._stdout_reader()

    assert player.provider.handle_remote_command.call_args_list == [
        call(player, command) for command in AirPlayRemoteCommand
    ]


def test_command_pipe_paths_are_unique_per_stream() -> None:
    """A stopped stream cannot remove a replacement stream's command pipe."""
    player = _make_player()
    first_stream = AirPlayStream(player)
    second_stream = AirPlayStream(player)
    assert first_stream.commands_pipe.path != second_stream.commands_pipe.path


@pytest.mark.asyncio
async def test_command_pipe_write_returns_false_without_reader(tmp_path: Path) -> None:
    """A command pipe write reports when no reader can receive the command."""
    pipe_path = tmp_path / "commands"
    os.mkfifo(pipe_path)
    writer = AsyncNamedPipeWriter(str(pipe_path))

    assert await writer.write(b"ACTION=STANDBY\n") is False


@pytest.mark.asyncio
async def test_command_pipe_write_returns_true_for_complete_write() -> None:
    """A complete command pipe write reports successful delivery."""
    data = b"ACTION=STANDBY\n"
    writer = AsyncNamedPipeWriter("/tmp/commands")  # noqa: S108
    writer._write_fd = 42

    with patch("music_assistant.helpers.named_pipe.os.write", return_value=len(data)) as write:
        assert await writer.write(data) is True

    write.assert_called_once_with(42, data)


@pytest.mark.asyncio
async def test_command_pipe_write_completes_short_write() -> None:
    """A short command pipe write continues with the remaining data."""
    data = b"ACTION=STANDBY\n"
    writer = AsyncNamedPipeWriter("/tmp/commands")  # noqa: S108
    writer._write_fd = 42

    with patch(
        "music_assistant.helpers.named_pipe.os.write",
        side_effect=[5, len(data) - 5],
    ) as write:
        assert await writer.write(data) is True

    assert write.call_args_list == [call(42, memoryview(data)), call(42, memoryview(data)[5:])]


@pytest.mark.asyncio
async def test_command_pipe_write_returns_false_and_resets_fd_on_epipe() -> None:
    """A closed command pipe reader resets the writer for a later retry."""
    writer = AsyncNamedPipeWriter("/tmp/commands")  # noqa: S108
    writer._write_fd = 42

    with (
        patch(
            "music_assistant.helpers.named_pipe.os.write",
            side_effect=OSError(errno.EPIPE, "reader closed"),
        ),
        patch("music_assistant.helpers.named_pipe.os.close") as close_fd,
    ):
        assert await writer.write(b"ACTION=STANDBY\n") is False

    close_fd.assert_called_once_with(42)
    assert writer._write_fd is None


@pytest.mark.asyncio
async def test_command_pipe_write_resets_fd_when_epipe_follows_partial_write() -> None:
    """An EPIPE after a short write reports failure and resets the writer."""
    data = b"ACTION=STANDBY\n"
    writer = AsyncNamedPipeWriter("/tmp/commands")  # noqa: S108
    writer._write_fd = 42

    with (
        patch(
            "music_assistant.helpers.named_pipe.os.write",
            side_effect=[5, OSError(errno.EPIPE, "reader closed")],
        ) as write,
        patch("music_assistant.helpers.named_pipe.os.close") as close_fd,
    ):
        assert await writer.write(data) is False

    assert write.call_args_list == [call(42, memoryview(data)), call(42, memoryview(data)[5:])]
    close_fd.assert_called_once_with(42)
    assert writer._write_fd is None


@pytest.mark.asyncio
async def test_command_pipe_writes_are_serialized() -> None:
    """Concurrent command writes cannot interleave after a short write."""
    writer = AsyncNamedPipeWriter("/tmp/commands")  # noqa: S108
    writer._write_fd = 42
    written_chunks: list[bytes] = []
    first_chunk_written = threading.Event()
    release_first_write = threading.Event()

    def write_chunk(_fd: int, data: memoryview) -> int:
        chunk = bytes(data)
        if not written_chunks:
            written_chunks.append(chunk[:2])
            first_chunk_written.set()
            assert release_first_write.wait(timeout=1)
            return 2
        written_chunks.append(chunk)
        return len(chunk)

    with patch("music_assistant.helpers.named_pipe.os.write", side_effect=write_chunk):
        first_write = asyncio.create_task(writer.write(b"FIRST\n"))
        assert await asyncio.to_thread(first_chunk_written.wait, 1)
        second_write = asyncio.create_task(writer.write(b"SECOND\n"))
        await asyncio.sleep(0)
        release_first_write.set()
        assert all(await asyncio.gather(first_write, second_write))

    assert b"".join(written_chunks) == b"FIRST\nSECOND\n"


@pytest.mark.asyncio
async def test_command_pipe_write_propagates_non_epipe_errors() -> None:
    """An unexpected command pipe write error remains visible to the caller."""
    writer = AsyncNamedPipeWriter("/tmp/commands")  # noqa: S108
    writer._write_fd = 42

    with (
        patch(
            "music_assistant.helpers.named_pipe.os.write",
            side_effect=OSError(errno.EBADF, "bad file descriptor"),
        ),
        pytest.raises(OSError, match="bad file descriptor"),
    ):
        await writer.write(b"ACTION=STANDBY\n")


@pytest.mark.asyncio
async def test_command_pipe_wait_for_reader_resolves_when_the_reader_attaches(
    tmp_path: Path,
) -> None:
    """The reader wait ends as soon as the binary opens its end of the command pipe."""
    pipe_path = tmp_path / "commands"
    os.mkfifo(pipe_path)
    writer = AsyncNamedPipeWriter(str(pipe_path))
    read_fds: list[int] = []

    async def attach_reader() -> None:
        await asyncio.sleep(0.01)
        read_fds.append(os.open(pipe_path, os.O_RDONLY | os.O_NONBLOCK))

    reader = asyncio.create_task(attach_reader())
    try:
        assert await writer.wait_for_reader(timeout=1) is True
        assert read_fds  # resolved on the attach, not before it
    finally:
        await reader
        for read_fd in read_fds:
            os.close(read_fd)
        await writer.remove()


@pytest.mark.asyncio
async def test_command_pipe_wait_for_reader_gives_up_at_its_timeout(tmp_path: Path) -> None:
    """A command pipe nothing ever reads fails the wait within the requested window."""
    pipe_path = tmp_path / "commands"
    os.mkfifo(pipe_path)
    writer = AsyncNamedPipeWriter(str(pipe_path))
    loop = asyncio.get_running_loop()

    started = loop.time()
    assert await writer.wait_for_reader(timeout=0.1) is False

    assert 0.1 <= loop.time() - started < 1


@pytest.mark.asyncio
async def test_command_pipe_wait_for_reader_uses_no_worker_thread(tmp_path: Path) -> None:
    """Waiting for the binary's reader never strands a blocking worker thread."""
    pipe_path = tmp_path / "commands"
    os.mkfifo(pipe_path)
    writer = AsyncNamedPipeWriter(str(pipe_path))

    with (
        patch(
            "music_assistant.helpers.named_pipe.os.open",
            side_effect=OSError(errno.ENXIO, "no reader"),
        ),
        patch(
            "music_assistant.helpers.named_pipe.asyncio.to_thread",
            new_callable=AsyncMock,
        ) as to_thread,
    ):
        assert await writer.wait_for_reader(timeout=0.1) is False

    to_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_command_pipe_wait_for_reader_defers_to_an_in_flight_write(tmp_path: Path) -> None:
    """The reader wait leaves the descriptor to a write that is already under way."""
    pipe_path = tmp_path / "commands"
    os.mkfifo(pipe_path)
    writer = AsyncNamedPipeWriter(str(pipe_path))
    read_fd = os.open(pipe_path, os.O_RDONLY | os.O_NONBLOCK)

    try:
        await writer._write_lock.acquire()
        wait = asyncio.create_task(writer.wait_for_reader(timeout=1))
        await asyncio.sleep(0)

        # a reader is attached the whole time, so only the write lock holds this up
        assert not wait.done()

        writer._write_lock.release()
        assert await wait is True
    finally:
        os.close(read_fd)
        await writer.remove()


@pytest.mark.asyncio
async def test_command_pipe_write_reuses_the_fd_from_the_reader_wait(tmp_path: Path) -> None:
    """A command written after the reader wait rides the descriptor that wait opened."""
    pipe_path = tmp_path / "commands"
    os.mkfifo(pipe_path)
    writer = AsyncNamedPipeWriter(str(pipe_path))
    read_fd = os.open(pipe_path, os.O_RDONLY | os.O_NONBLOCK)

    try:
        assert await writer.wait_for_reader(timeout=1) is True

        with patch("music_assistant.helpers.named_pipe.os.open") as open_pipe:
            assert await writer.write(b"ACTION=STANDBY\n") is True

        open_pipe.assert_not_called()
        assert os.read(read_fd, 64) == b"ACTION=STANDBY\n"
    finally:
        os.close(read_fd)
        await writer.remove()


@pytest.mark.asyncio
async def test_cli_command_updates_timestamp_after_successful_delivery() -> None:
    """A delivered command updates the player's last command timestamp."""
    player = _make_player()
    player.last_command_sent = 10.0
    stream = AirPlayStream(player)
    stream._cli_proc = MagicMock(closed=False)

    with (
        patch.object(stream.commands_pipe, "write", new=AsyncMock(return_value=True)),
        patch("music_assistant.providers.airplay.stream.time.time", return_value=20.0),
    ):
        assert await stream.send_cli_command("ACTION=STANDBY") is True

    assert player.last_command_sent == 20.0


@pytest.mark.asyncio
async def test_cli_command_preserves_timestamp_when_delivery_fails() -> None:
    """A dropped command leaves the player's last command timestamp unchanged."""
    player = _make_player()
    player.last_command_sent = 10.0
    stream = AirPlayStream(player)
    stream._cli_proc = MagicMock(closed=False)

    with patch.object(stream.commands_pipe, "write", new=AsyncMock(return_value=False)):
        assert await stream.send_cli_command("ACTION=STANDBY") is False

    assert player.last_command_sent == 10.0


@pytest.mark.asyncio
async def test_cli_command_preserves_timestamp_when_delivery_raises() -> None:
    """A command write error leaves the player's last command timestamp unchanged."""
    player = _make_player()
    player.last_command_sent = 10.0
    stream = AirPlayStream(player)
    stream._cli_proc = MagicMock(closed=False)

    with (
        patch.object(
            stream.commands_pipe,
            "write",
            new=AsyncMock(side_effect=OSError("command pipe failed")),
        ),
        pytest.raises(OSError, match="command pipe failed"),
    ):
        await stream.send_cli_command("ACTION=STANDBY")

    assert player.last_command_sent == 10.0


@pytest.mark.asyncio
async def test_connect_does_not_write_before_the_binary_can_read() -> None:
    """Connecting lays out the command pipe and starts cliairplay, pushing nothing into it."""
    player = _make_player()
    stream = AirPlayStream(player)
    # media is available, so a push that never happens is by design and not a missing source
    player.current_media = MagicMock(corrected_elapsed_time=12.5)
    process = MagicMock(closed=False)
    process.start = AsyncMock(return_value=None)
    operation_order: list[str] = []

    async def create_pipe() -> None:
        operation_order.append("pipe")

    async def start_process() -> None:
        operation_order.append("process")

    def consume_task(awaitable: Any) -> MagicMock:
        awaitable.close()
        task = MagicMock()
        task.done.return_value = True
        return task

    process.start.side_effect = start_process
    player.provider.mass.create_task.side_effect = consume_task
    with (
        patch.object(stream, "_build_cli_args", new_callable=AsyncMock, return_value=["binary"]),
        patch(
            "music_assistant.providers.airplay.stream.AsyncProcess",
            return_value=process,
        ),
        patch.object(stream.commands_pipe, "create", side_effect=create_pipe),
        patch.object(stream, "send_metadata", new_callable=AsyncMock) as send_metadata_mock,
    ):
        await stream.connect()

    assert operation_order == ["pipe", "process"]
    send_metadata_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_failure_cleans_up_process_and_pipe() -> None:
    """A cliairplay process that fails to start cannot leave a live process or FIFO."""
    player = _make_player()
    stream = AirPlayStream(player)
    process = MagicMock(closed=False)
    process.start = AsyncMock(side_effect=OSError("process start failed"))
    process.kill = AsyncMock()

    with (
        patch.object(stream, "_build_cli_args", new_callable=AsyncMock, return_value=["binary"]),
        patch(
            "music_assistant.providers.airplay.stream.AsyncProcess",
            return_value=process,
        ),
        patch.object(stream.commands_pipe, "create", new_callable=AsyncMock),
        patch.object(stream.commands_pipe, "remove", new_callable=AsyncMock) as remove_pipe,
        pytest.raises(OSError, match="process start failed"),
    ):
        await stream.connect()

    process.kill.assert_awaited_once()
    remove_pipe.assert_awaited_once()
    assert stream._cli_proc is None
    assert stream._cleanup_complete is True


@pytest.mark.asyncio
async def test_start_sends_command_and_stamps_position() -> None:
    """START is delivered over the command pipe and stamps the media position."""
    player = _make_player()
    stream = AirPlayStream(player)
    stream._cli_proc = MagicMock(closed=False)
    stream._connected.set()

    with patch.object(
        stream,
        "_write_cli_command",
        new_callable=AsyncMock,
        side_effect=_acking_write_cli_command(stream),
    ) as write_command:
        assert await stream.start(START_UNIX_MS, 12_000) == START_UNIX_MS

    write_command.assert_awaited_once_with(f"START_UNIX_MS={START_UNIX_MS}\nACTION=START")
    assert stream._start_position == 12.0
    player.set_state_from_stream.assert_called_once_with(elapsed_time=12.0, stream=stream)


@pytest.mark.asyncio
async def test_start_join_marks_the_command() -> None:
    """A late-join START carries START_JOIN=1 so the binary enforces clock readiness."""
    player = _make_player()
    stream = AirPlayStream(player)
    stream._cli_proc = MagicMock(closed=False)
    stream._connected.set()

    with patch.object(
        stream,
        "_write_cli_command",
        new_callable=AsyncMock,
        side_effect=_acking_write_cli_command(stream),
    ) as write_command:
        assert await stream.start(START_UNIX_MS, 0, join=True) == START_UNIX_MS

    write_command.assert_awaited_once_with(
        f"START_UNIX_MS={START_UNIX_MS}\nSTART_JOIN=1\nACTION=START"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "complete_before_start",
    [True, False],
    ids=["delivered", "rendering"],
)
async def test_start_transition_artwork_settled_or_retried(complete_before_start: bool) -> None:
    """START keeps already-delivered transition artwork settled and retries a superseded render."""
    player = _make_player()
    stream = AirPlayStream(player)
    stream._cli_proc = MagicMock(closed=False)
    stream._connected.set()
    metadata = MagicMock(
        corrected_elapsed_time=0,
        queue_item_id="item-new",
        title="New track",
        artist="Artist",
        album="Album",
        duration=180,
        image_url="new-image",
    )
    stream.session = MagicMock(media=metadata)
    render_started = asyncio.Event()
    release_render = asyncio.Event()
    metadata_tasks: list[asyncio.Task[Any]] = []
    render_count = 0

    def create_task(target: Any, **_kwargs: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(target())
        metadata_tasks.append(task)
        return task

    async def prepare_artwork(_image_url: str, _generation: int) -> str:
        nonlocal render_count
        render_count += 1
        if render_count == 1:
            render_started.set()
            if not complete_before_start:
                await release_render.wait()
            return "/cache/pretransition.jpg"
        return "/cache/posttransition.jpg"

    player.provider.mass.create_task.side_effect = create_task
    with (
        patch.object(
            stream,
            "_write_cli_command",
            new_callable=AsyncMock,
            side_effect=_acking_write_cli_command(stream),
        ) as write_command,
        patch.object(
            stream,
            "_prepare_artwork",
            new_callable=AsyncMock,
            side_effect=prepare_artwork,
        ),
    ):
        pretransition_task = asyncio.create_task(stream.send_metadata(None, metadata))
        await render_started.wait()
        if complete_before_start:
            await pretransition_task
        assert await stream.start(START_UNIX_MS, 0) == START_UNIX_MS
        await asyncio.gather(*metadata_tasks)
        release_render.set()
        await pretransition_task

    commands = [args.args[0] for args in write_command.await_args_list]
    start_command = f"START_UNIX_MS={START_UNIX_MS}\nACTION=START"
    if complete_before_start:
        # artwork delivered before the anchor stays settled; re-pushing it
        # around the START would make an Apple TV re-render its screen
        assert commands[-1] == start_command
        assert "ARTWORK=/cache/pretransition.jpg" in commands
    else:
        # the anchor superseded the in-flight render; the post-anchor push
        # renders again and delivers the artwork once
        assert commands[-2:] == [start_command, "ARTWORK=/cache/posttransition.jpg"]
        assert "ARTWORK=/cache/pretransition.jpg" not in commands
    assert stream._metadata_generation == 2
    assert stream._metadata_artwork_checksum == "new-image"


@pytest.mark.asyncio
async def test_start_requires_connected_process() -> None:
    """START is rejected without a connected cliairplay process."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock(closed=False)
    # not connected: _connected event never set

    with (
        patch.object(stream, "_write_cli_command", new_callable=AsyncMock) as write_command,
        pytest.raises(RuntimeError, match="without a connected cliairplay process"),
    ):
        await stream.start(START_UNIX_MS, 0)

    write_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_flush_sends_command_and_awaits_ack() -> None:
    """FLUSH is delivered and resolves once the binary reports it flushed."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock(closed=False)
    stream._connected.set()

    with patch.object(
        stream, "_write_cli_command", new_callable=AsyncMock, return_value=True
    ) as write_command:
        flush_task = asyncio.create_task(stream.flush())
        await asyncio.sleep(0)
        assert stream._handle_status_line("[STATUS] flushed") is False
        assert await flush_task is True

    write_command.assert_awaited_once_with("ACTION=FLUSH")


@pytest.mark.asyncio
async def test_flush_times_out_without_ack() -> None:
    """FLUSH returns False when the binary never acknowledges it."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock(closed=False)
    stream._connected.set()

    with patch.object(stream, "_write_cli_command", new_callable=AsyncMock, return_value=True):
        assert await stream.flush(timeout=0) is False


@pytest.mark.asyncio
async def test_flush_returns_false_when_command_not_delivered() -> None:
    """FLUSH reports failure when the command cannot be delivered."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock(closed=False)
    stream._connected.set()

    with patch.object(stream, "_write_cli_command", new_callable=AsyncMock, return_value=False):
        assert await stream.flush() is False


@pytest.mark.asyncio
async def test_start_raises_when_command_not_delivered() -> None:
    """A dropped START surfaces as an error so the caller can fall back cold."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock(closed=False)
    stream._connected.set()

    with (
        patch.object(stream, "_write_cli_command", new_callable=AsyncMock, return_value=False),
        pytest.raises(PlayerCommandFailed, match="Could not deliver START"),
    ):
        await stream.start(1_750_000_000_000, 0)


@pytest.mark.asyncio
async def test_start_fails_fast_on_reported_start_failure() -> None:
    """A reported start failure ends the ack wait at once instead of timing out."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock(closed=False)
    stream._connected.set()

    with patch.object(stream, "_write_cli_command", new_callable=AsyncMock, return_value=True):
        start_task = asyncio.create_task(stream.start(START_UNIX_MS, 0, join=True))
        await asyncio.sleep(0)
        stream._handle_status_line(
            '[STATUS] error code=start_failed http=0 detail="no live session to start"'
        )
        with pytest.raises(PlayerCommandFailed, match="no live session to start"):
            await start_task

    # a command failure must not poison how a NEW connection is reported
    assert stream._connect_error is None


@pytest.mark.asyncio
async def test_flush_fails_fast_on_reported_flush_failure() -> None:
    """A reported flush failure resolves the ack wait as a failure, not a timeout."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock(closed=False)
    stream._connected.set()

    with patch.object(stream, "_write_cli_command", new_callable=AsyncMock, return_value=True):
        flush_task = asyncio.create_task(stream.flush())
        await asyncio.sleep(0)
        stream._handle_status_line(
            '[STATUS] error code=flush_failed http=0 detail="session rejected the flush"'
        )
        assert await flush_task is False

    assert stream._connect_error is None


@pytest.mark.asyncio
async def test_start_failure_does_not_outlive_its_command() -> None:
    """A failed START leaves no error behind that would fail the next one."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock(closed=False)
    stream._connected.set()
    stream._handle_status_line("[STATUS] error code=start_failed")

    with patch.object(stream, "_write_cli_command", new_callable=AsyncMock, return_value=True):
        start_task = asyncio.create_task(stream.start(START_UNIX_MS, 0))
        await asyncio.sleep(0)
        stream._handle_status_line(
            f"[STATUS] started requested_unix_ms={START_UNIX_MS} at_unix_ms={START_UNIX_MS}"
        )
        assert await start_task == START_UNIX_MS


@pytest.mark.asyncio
async def test_start_returns_the_instant_the_binary_scheduled() -> None:
    """A corrected ack, not the commanded instant, is what the caller maps content onto."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock(closed=False)
    stream._connected.set()
    corrected = START_UNIX_MS + 700

    with patch.object(
        stream,
        "_write_cli_command",
        new_callable=AsyncMock,
        side_effect=_acking_write_cli_command(stream, corrected),
    ):
        assert await stream.start(START_UNIX_MS, 0) == corrected


@pytest.mark.asyncio
async def test_start_reports_no_instant_when_the_ack_never_arrives(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unacknowledged START reports no instant and says the commanded one is assumed."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock(closed=False)
    stream._connected.set()

    with (
        patch.object(stream, "_write_cli_command", new_callable=AsyncMock, return_value=True),
        patch("music_assistant.providers.airplay.stream.AIRPLAY_START_ACK_TIMEOUT_MS", 10),
        caplog.at_level(logging.WARNING),
    ):
        assert await stream.start(START_UNIX_MS, 0) is None

    assert "did not acknowledge its start" in caplog.text
    assert str(START_UNIX_MS) in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("join", "expected_timeout"),
    [
        (True, AIRPLAY_JOIN_START_ACK_TIMEOUT_MS / 1000),
        (False, AIRPLAY_START_ACK_TIMEOUT_MS / 1000),
    ],
    ids=["join", "plain"],
)
async def test_start_ack_window_matches_the_arm(join: bool, expected_timeout: float) -> None:
    """A join's ack is held for clock verification, so it waits far longer than a plain start."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock(closed=False)
    stream._connected.set()
    timeouts: list[float] = []

    async def record_timeout(awaitable: Any, timeout: float) -> None:
        timeouts.append(timeout)
        awaitable.close()
        raise TimeoutError

    with (
        patch.object(stream, "_write_cli_command", new_callable=AsyncMock, return_value=True),
        patch(
            "music_assistant.providers.airplay.stream.asyncio.wait_for",
            side_effect=record_timeout,
        ),
    ):
        assert await stream.start(START_UNIX_MS, 0, join=join) is None

    assert timeouts == [expected_timeout]


@pytest.mark.asyncio
async def test_flush_returns_false_when_not_connected() -> None:
    """FLUSH is a no-op returning False before the device connects."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock(closed=False)
    # not connected

    with patch.object(stream, "_write_cli_command", new_callable=AsyncMock) as write_command:
        assert await stream.flush() is False

    write_command.assert_not_awaited()


def test_flushed_status_sets_flush_event() -> None:
    """The [STATUS] flushed line releases the flush acknowledgement event."""
    stream = AirPlayStream(_make_player())
    assert not stream._flushed.is_set()

    assert stream._handle_status_line("[STATUS] flushed") is False

    assert stream._flushed.is_set()


@pytest.mark.asyncio
async def test_clock_ready_projection_resolves_the_wait() -> None:
    """A probing receiver reports when its clock becomes usable, from its first probe."""
    stream = AirPlayStream(_make_player())

    assert (
        stream._handle_status_line(
            "[STATUS] clock_ready mode=ptp state=probing streak_ms=0 exchanges=1 "
            f"ready_in_ms=2300 ready_at_unix_ms={START_UNIX_MS}"
        )
        is False
    )

    assert await stream.wait_clock_ready(timeout=0.01) == (
        ClockReadiness.PROJECTED,
        START_UNIX_MS,
    )


@pytest.mark.asyncio
async def test_clock_ready_cold_line_keeps_waiting_for_a_projection() -> None:
    """A receiver that has not probed yet carries no projection, so the wait goes on."""
    stream = AirPlayStream(_make_player())

    stream._handle_status_line(
        "[STATUS] clock_ready mode=ptp state=cold streak_ms=0 exchanges=0 "
        "ready_in_ms=0 ready_at_unix_ms=0"
    )

    assert await stream.wait_clock_ready(timeout=0.01) == (ClockReadiness.UNREPORTED, 0)
    assert not stream._clock_ready.is_set()

    stream._handle_status_line(
        "[STATUS] clock_ready mode=ptp state=ready streak_ms=2400 exchanges=9 "
        f"ready_in_ms=0 ready_at_unix_ms={START_UNIX_MS}"
    )

    assert await stream.wait_clock_ready(timeout=0.01) == (
        ClockReadiness.PROJECTED,
        START_UNIX_MS,
    )


@pytest.mark.asyncio
async def test_clock_ready_ntp_resolves_without_a_projection() -> None:
    """NTP timing has no receiver clock to wait for, so the wait ends with nothing."""
    stream = AirPlayStream(_make_player())

    stream._handle_status_line(
        "[STATUS] clock_ready mode=ntp state=ready streak_ms=0 exchanges=0 "
        f"ready_in_ms=0 ready_at_unix_ms={START_UNIX_MS}"
    )

    assert stream._clock_ready.is_set()
    assert await stream.wait_clock_ready(timeout=0.01) == (ClockReadiness.NOT_APPLICABLE, 0)


@pytest.mark.asyncio
async def test_wait_clock_ready_times_out_for_a_binary_that_never_reports() -> None:
    """A binary that does not report readiness is told apart from one that answered."""
    stream = AirPlayStream(_make_player())

    assert await stream.wait_clock_ready(timeout=0.01) == (ClockReadiness.UNREPORTED, 0)


@pytest.mark.asyncio
async def test_clock_ready_stalled_state_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    """A receiver that never answers our clock is reported loudly once and carries no projection."""
    stream = AirPlayStream(_make_player())

    with caplog.at_level(logging.DEBUG):
        ended = stream._handle_status_line(
            "[STATUS] clock_ready mode=ptp state=stalled streak_ms=0 exchanges=0 "
            "ready_in_ms=0 ready_at_unix_ms=0"
        )
        stream._handle_status_line(
            "[STATUS] clock_ready mode=ptp state=stalled streak_ms=0 exchanges=0 "
            "ready_in_ms=0 ready_at_unix_ms=0"
        )

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert ended is False
    assert len(warnings) == 1
    assert "Player A" in warnings[0].getMessage()
    assert "319/320" in warnings[0].getMessage()
    assert stream._clock_ready.is_set()
    assert await stream.wait_clock_ready(timeout=0.01) == (ClockReadiness.STALLED, 0)


def test_clock_ready_stall_warning_is_ptp_only(caplog: pytest.LogCaptureFixture) -> None:
    """An NTP-timed session has no clock of ours to answer, so it never reads as a stall."""
    stream = AirPlayStream(_make_player())

    with caplog.at_level(logging.DEBUG):
        stream._handle_status_line(
            "[STATUS] clock_ready mode=ntp state=stalled streak_ms=0 exchanges=0 "
            f"ready_in_ms=0 ready_at_unix_ms={START_UNIX_MS}"
        )

    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
    assert stream._clock_ready_at_unix_ms == 0


@pytest.mark.parametrize("state", ["cold", "probing", "ready"])
def test_clock_ready_handshake_states_do_not_warn(
    state: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Any state but a stall is a normal step of the clock handshake and stays quiet."""
    stream = AirPlayStream(_make_player())

    with caplog.at_level(logging.DEBUG):
        ended = stream._handle_status_line(
            f"[STATUS] clock_ready mode=ptp state={state} streak_ms=900 exchanges=4 "
            f"ready_in_ms=940 ready_at_unix_ms={START_UNIX_MS}"
        )

    assert ended is False
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


def test_elapsed_includes_start_position() -> None:
    """Reported progress is the current anchor's media base plus the binary delta."""
    player = _make_player()
    stream = AirPlayStream(player)
    stream._start_position = 12.0

    stream._update_elapsed(1.5)

    player.set_state_from_stream.assert_called_once_with(
        state=PlaybackState.PLAYING,
        elapsed_time=13.5,
        stream=stream,
    )


def test_legacy_reanchor_warns_accumulate_per_event() -> None:
    """The legacy warn line reports a per-event shift, so successive events accumulate."""
    stream = AirPlayStream(_make_player())
    assert stream.cumulative_shift_seconds == 0.0

    for _event in range(2):
        assert (
            stream._handle_status_line(
                "[AP2] Re-anchored after PCM starvation: "
                "shifted_frames=67870 lead_frames=77175 count=1"
            )
            is False
        )

    # two +1.539s events accumulate to ~3.078s
    assert stream.cumulative_shift_seconds == pytest.approx(2 * 67870 / 44100)


def test_reanchor_status_supersedes_legacy_warn() -> None:
    """The [STATUS] REANCHOR total is authoritative and stops the legacy warn double count."""
    stream = AirPlayStream(_make_player())

    # a first legacy warn accumulates until the status line arrives
    stream._handle_status_line(
        "[AP2] Re-anchored after PCM starvation: shifted_frames=67870 lead_frames=77175 count=1"
    )
    # the machine-readable line SETS from the cumulative total and supersedes the warn
    assert (
        stream._handle_status_line(
            "[STATUS] REANCHOR shifted_frames=67870 total_shifted_frames=67870 sample_rate=44100"
        )
        is False
    )
    assert stream._reanchor_status_seen is True
    assert stream.cumulative_shift_seconds == pytest.approx(67870 / 44100)

    # both lines fire per event for new binaries: the warn that follows is ignored,
    # only the status line's cumulative total is tracked (no double count)
    stream._handle_status_line(
        "[AP2] Re-anchored after PCM starvation: shifted_frames=67870 lead_frames=77175 count=2"
    )
    assert stream.cumulative_shift_seconds == pytest.approx(67870 / 44100)
    stream._handle_status_line(
        "[STATUS] REANCHOR shifted_frames=67870 total_shifted_frames=135740 sample_rate=44100"
    )
    assert stream.cumulative_shift_seconds == pytest.approx(135740 / 44100)


def test_reanchor_status_prefers_line_sample_rate() -> None:
    """The sample rate carried on the [STATUS] REANCHOR line wins over the stream format."""
    player = _make_player()
    stream = AirPlayStream(player)
    stream.pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=48000, bit_depth=16
    )

    stream._handle_status_line(
        "[STATUS] REANCHOR shifted_frames=44100 total_shifted_frames=44100 sample_rate=44100"
    )

    # converts at 44100 (from the line), not 48000 (the stream format) -> exactly 1.0s
    assert stream.cumulative_shift_seconds == pytest.approx(1.0)


def test_reanchor_status_ignores_line_without_total() -> None:
    """A [STATUS] REANCHOR line missing the total leaves the shift unchanged."""
    stream = AirPlayStream(_make_player())
    stream.cumulative_shift_seconds = 1.5

    stream._handle_status_line("[STATUS] REANCHOR shifted_frames=67870 sample_rate=44100")

    assert stream.cumulative_shift_seconds == 1.5
    assert stream._reanchor_status_seen is False


def test_reanchor_shift_ignores_line_without_frames() -> None:
    """A malformed legacy re-anchor line leaves the tracked shift unchanged."""
    stream = AirPlayStream(_make_player())
    stream.cumulative_shift_seconds = 1.5

    ended = stream._handle_status_line("[AP2] Re-anchored after PCM starvation: shifted_frames=")

    assert ended is False
    assert stream.cumulative_shift_seconds == 1.5


@pytest.mark.asyncio
async def test_start_resets_reanchor_shift() -> None:
    """A START re-anchors from scratch, clearing the shift and the status flag."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock(closed=False)
    stream._connected.set()
    stream.cumulative_shift_seconds = 3.078
    stream._reanchor_status_seen = True

    with patch.object(
        stream,
        "_write_cli_command",
        new_callable=AsyncMock,
        side_effect=_acking_write_cli_command(stream),
    ):
        assert await stream.start(START_UNIX_MS, 0) == START_UNIX_MS

    assert stream.cumulative_shift_seconds == 0.0
    assert stream._reanchor_status_seen is False


@pytest.mark.asyncio
async def test_connect_resets_accumulated_shift() -> None:
    """A fresh cliairplay process starts from a zero playout shift and cleared flag."""
    player = _make_player()
    stream = AirPlayStream(player)
    stream.cumulative_shift_seconds = 5.0
    stream._reanchor_status_seen = True
    process = MagicMock(closed=False)
    process.start = AsyncMock(return_value=None)

    def consume_task(awaitable: Any) -> MagicMock:
        awaitable.close()
        task = MagicMock()
        task.done.return_value = True
        return task

    player.provider.mass.create_task.side_effect = consume_task
    with (
        patch.object(stream, "_build_cli_args", new_callable=AsyncMock, return_value=["binary"]),
        patch("music_assistant.providers.airplay.stream.AsyncProcess", return_value=process),
        patch.object(stream.commands_pipe, "create", new_callable=AsyncMock),
    ):
        await stream.connect()

    assert stream.cumulative_shift_seconds == 0.0
    assert stream._reanchor_status_seen is False


@pytest.mark.asyncio
async def test_initial_metadata_skips_artwork() -> None:
    """The pre-connect metadata push cannot delay setup on artwork rendering."""
    player = _make_player()
    stream = AirPlayStream(player)
    stream._cli_proc = MagicMock(closed=False)
    metadata = MagicMock(
        title="Track",
        artist="Artist",
        album="Album",
        duration=180,
        image_url="image",
    )

    with (
        patch.object(stream, "send_cli_command", new_callable=AsyncMock) as send_command,
        patch.object(
            stream,
            "_render_and_send_artwork",
            new_callable=AsyncMock,
        ) as send_artwork,
    ):
        await stream.send_metadata(0, metadata, send_artwork=False)

    assert send_command.await_count == 2
    commands = [args.args[0] for args in send_command.await_args_list]
    assert "TITLE=Track" in commands[0]
    # the progress correction rides along right after the metadata push
    assert commands[1].endswith("PROGRESS=0")
    send_artwork.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_for_connection_pushes_metadata_immediately() -> None:
    """
    Track metadata is pushed the instant the binary can receive it.

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
    operation_order: list[str] = []

    async def wait_for_reader(_timeout: float) -> bool:
        operation_order.append("reader")
        return True

    async def send_current_metadata(**_kwargs: Any) -> None:
        operation_order.append("metadata")

    with (
        patch.object(stream, "_cli_proc", MagicMock()),  # non-None so the method proceeds
        patch.object(stream.commands_pipe, "wait_for_reader", side_effect=wait_for_reader),
        patch.object(stream, "_send_current_metadata", side_effect=send_current_metadata),
        patch.object(stream, "send_cli_command", return_value=None),  # avoid a real coroutine
    ):
        await stream.wait_for_connection()

    # Nothing is written before the binary has a reader on the command pipe.
    assert operation_order == ["reader", "metadata"]
    # Metadata pushed synchronously on connect...
    player._on_player_media_updated.assert_called_once_with()
    # ...and never routed through the delayed call_later path.
    deferred_callables = [call.args[1] for call in player.provider.mass.call_later.call_args_list]
    assert player._on_player_media_updated not in deferred_callables
    # The volume resend is still deferred (existing behavior preserved).
    assert player.provider.mass.call_later.call_count == 1
    assert player.provider.mass.call_later.call_args_list[0].args[0] == 2


@pytest.mark.asyncio
async def test_wait_for_connection_reports_an_unread_command_pipe() -> None:
    """A binary that never attaches to the command pipe is reported for the player it serves."""
    player = _make_player()
    player.logger = MagicMock()
    player.volume_muted = False
    stream = AirPlayStream(player)
    stream._connected.set()
    stream._cli_proc = MagicMock(closed=False)

    with (
        patch.object(
            stream.commands_pipe, "wait_for_reader", new=AsyncMock(return_value=False)
        ) as wait_for_reader,
        patch.object(stream, "_send_current_metadata", new_callable=AsyncMock),
        patch.object(stream, "send_cli_command", return_value=None),
    ):
        await stream.wait_for_connection()

    wait_for_reader.assert_awaited_once()
    player.logger.warning.assert_called_once()
    assert player.display_name in player.logger.warning.call_args.args


@pytest.mark.asyncio
async def test_wait_for_connection_stays_quiet_about_a_stopped_stream() -> None:
    """A stream torn down while connecting took its command pipe along, which is no fault."""
    player = _make_player()
    player.logger = MagicMock()
    player.volume_muted = False
    stream = AirPlayStream(player)
    stream._connected.set()
    stream._cli_proc = MagicMock(closed=False)
    stream._stopping = True

    with (
        patch.object(stream.commands_pipe, "wait_for_reader", new=AsyncMock(return_value=False)),
        patch.object(stream, "_send_current_metadata", new_callable=AsyncMock),
        patch.object(stream, "send_cli_command", return_value=None),
    ):
        await stream.wait_for_connection()

    player.logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_prepare_artwork_returns_cache_path() -> None:
    """Artwork preparation returns the shared cache path without a per-player copy."""
    player = _make_player()
    stream = AirPlayStream(player)
    image_url = "https://example.com/artwork.png"
    cached_path = "/cache/thumbnails/artwork_flat.jpg"

    with patch(
        "music_assistant.providers.airplay.stream.get_image_thumb_path",
        new=AsyncMock(return_value=cached_path),
    ) as get_thumb_path:
        result = await stream._prepare_artwork(image_url, 1)

    assert result == cached_path
    assert not hasattr(stream, "_artwork_paths")
    get_thumb_path.assert_awaited_once_with(
        stream.mass,
        image_url,
        AIRPLAY_ARTWORK_SIZE,
        "",
        image_format="JPEG",
        flatten_transparency=True,
    )


@pytest.mark.asyncio
async def test_stop_cleans_up_when_stop_command_fails() -> None:
    """A command-pipe failure cannot skip process and stream cleanup."""
    player = _make_player()
    stream = AirPlayStream(player)
    process = MagicMock()
    process.closed = False
    process.kill = AsyncMock()
    stream._cli_proc = process

    with (
        patch.object(
            stream.commands_pipe,
            "write",
            new_callable=AsyncMock,
            side_effect=OSError("command pipe failed"),
        ),
        patch.object(stream.commands_pipe, "remove", new_callable=AsyncMock) as remove_pipe,
        pytest.raises(OSError, match="command pipe failed"),
    ):
        await stream.stop(force=True)

    assert stream._stopped is True
    assert stream._cleanup_complete is True
    remove_pipe.assert_awaited_once()
    process.kill.assert_awaited_once()
    player.set_state_from_stream.assert_called_once_with(
        state=PlaybackState.IDLE,
        elapsed_time=0,
        stream=stream,
    )


@pytest.mark.asyncio
async def test_stop_awaits_cancelled_stdout_reader() -> None:
    """Stream teardown waits for the stdout reader to release process resources."""
    player = _make_player()
    stream = AirPlayStream(player)
    process = MagicMock()
    process.closed = False
    process.kill = AsyncMock()
    stream._cli_proc = process
    reader_started = asyncio.Event()

    async def _stdout_reader() -> None:
        reader_started.set()
        await asyncio.Event().wait()

    reader_task = asyncio.create_task(_stdout_reader())
    stream._stdout_reader_task = reader_task
    await reader_started.wait()

    with (
        patch.object(stream.commands_pipe, "write", new_callable=AsyncMock),
        patch.object(stream.commands_pipe, "remove", new_callable=AsyncMock),
    ):
        await stream.stop(force=True)

    assert reader_task.cancelled()
    process.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_force_stop_does_not_wait_for_artwork_render() -> None:
    """Force-stop tears down immediately while remote artwork rendering finishes."""
    player = _make_player()
    stream = AirPlayStream(player)
    process = MagicMock()
    process.closed = False
    process.kill = AsyncMock()
    stream._cli_proc = process
    metadata = MagicMock(
        title="Track",
        artist="Artist",
        album="Album",
        duration=180,
        image_url="slow-image",
    )
    artwork_started = asyncio.Event()
    release_artwork = asyncio.Event()

    async def _prepare_artwork(_image_url: str, _generation: int) -> str:
        artwork_started.set()
        await release_artwork.wait()
        return "late.jpg"

    with (
        patch.object(stream.commands_pipe, "write", new_callable=AsyncMock),
        patch.object(stream.commands_pipe, "remove", new_callable=AsyncMock),
        patch.object(
            stream,
            "_prepare_artwork",
            new_callable=AsyncMock,
            side_effect=_prepare_artwork,
        ),
    ):
        metadata_task = asyncio.create_task(stream.send_metadata(0, metadata))
        await artwork_started.wait()
        await asyncio.wait_for(stream.stop(force=True), timeout=0.5)
        release_artwork.set()
        await metadata_task

    assert stream._cleanup_complete is True
    process.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_eof_cleans_up_command_pipe() -> None:
    """A naturally ended CLI stream removes its command pipe."""
    player = _make_player()
    stream = AirPlayStream(player)
    process = MagicMock()
    stream._cli_proc = process

    async def _stderr_lines() -> AsyncGenerator[str]:
        yield "[STATUS] eof"

    with (
        patch.object(process, "iter_stderr", return_value=_stderr_lines()),
        patch.object(stream.commands_pipe, "remove", new_callable=AsyncMock) as remove_pipe,
    ):
        await stream._stderr_reader()

    assert stream._stopped is True
    remove_pipe.assert_awaited_once()
    player.schedule_group_rejoin.assert_not_called()


async def _run_unexpected_process_death(player: MagicMock) -> AirPlayStream:
    """Drive the stderr reader through an unexpected process exit."""
    stream = AirPlayStream(player)
    process = MagicMock()
    stream._cli_proc = process

    async def _stderr_lines() -> AsyncGenerator[str]:
        yield "some final log line"

    with (
        patch.object(process, "iter_stderr", return_value=_stderr_lines()),
        patch.object(stream.commands_pipe, "remove", new_callable=AsyncMock),
    ):
        await stream._stderr_reader()
    return stream


@pytest.mark.asyncio
async def test_unexpected_death_of_synced_child_schedules_rejoin() -> None:
    """A grouped member whose process dies unexpectedly gets a re-join scheduled."""
    player = _make_player()
    player.synced_to = "leader"
    player.group_members = []
    # the leader's other members are captured as fallback candidates in case
    # leadership transfers while the re-join backoff runs
    leader = MagicMock()
    leader.group_members = ["leader", player.player_id, "sibling"]
    player.provider.mass.players.get_player.return_value = leader

    stream = await _run_unexpected_process_death(player)

    player.schedule_group_rejoin.assert_called_once_with(["leader", "sibling"])
    player.set_state_from_stream.assert_called_once_with(
        state=PlaybackState.IDLE, elapsed_time=0, stream=stream
    )


@pytest.mark.asyncio
async def test_unexpected_death_of_leader_schedules_rejoin_to_members() -> None:
    """A dying leader re-joins towards its surviving members (leadership transfers)."""
    player = _make_player()
    player.synced_to = None
    player.group_members = [player.player_id, "child1", "child2"]

    await _run_unexpected_process_death(player)

    player.schedule_group_rejoin.assert_called_once_with(["child1", "child2"])
    # the controller sets the leader's final state (transfer or dissolve)
    player.set_state_from_stream.assert_not_called()


@pytest.mark.asyncio
async def test_unexpected_death_of_solo_player_schedules_no_rejoin() -> None:
    """An ungrouped player's process death only marks the player idle."""
    player = _make_player()
    player.synced_to = None
    player.group_members = []

    stream = await _run_unexpected_process_death(player)

    player.schedule_group_rejoin.assert_not_called()
    player.set_state_from_stream.assert_called_once_with(
        state=PlaybackState.IDLE, elapsed_time=0, stream=stream
    )


@pytest.mark.asyncio
async def test_unexpected_death_of_static_group_member_drops_member_only() -> None:
    """A static group member's death drops just that member, never the whole group."""
    player = _make_player()
    player.synced_to = "leader"
    player.group_members = []
    # the player is a static member of an actively playing group player, for
    # which cmd_ungroup would release (stop) the WHOLE group
    player.state.active_group = "syncgroup1"
    group_player = MagicMock()
    group_player.static_group_members = ["leader", player.player_id]
    leader = MagicMock()
    leader.group_members = ["leader", player.player_id]
    player.provider.mass.players.get_player.side_effect = lambda player_id: {
        "syncgroup1": group_player,
        "leader": leader,
    }.get(player_id)

    await _run_unexpected_process_death(player)

    players_controller = player.provider.mass.players
    players_controller.cmd_set_members.assert_called_once_with(
        "leader", player_ids_to_remove=[player.player_id]
    )
    players_controller.cmd_ungroup.assert_not_called()
    player.schedule_group_rejoin.assert_called_once_with(["leader"])


@pytest.mark.asyncio
async def test_process_eof_during_render_does_not_send_artwork() -> None:
    """A cache lookup finishing after EOF cannot send stale artwork."""
    player = _make_player()
    stream = AirPlayStream(player)
    render_started = asyncio.Event()
    release_render = asyncio.Event()

    async def _get_image_thumb_path(*_args: Any, **_kwargs: Any) -> str:
        render_started.set()
        await release_render.wait()
        return "/cache/thumbnails/artwork.jpg"

    with (
        patch(
            "music_assistant.providers.airplay.stream.get_image_thumb_path",
            new_callable=AsyncMock,
            side_effect=_get_image_thumb_path,
        ),
        patch.object(stream, "send_cli_command", new_callable=AsyncMock) as send_command,
    ):
        render_task = asyncio.create_task(stream._render_and_send_artwork("image", 1))
        await render_started.wait()
        stream._stopped = True
        release_render.set()
        await render_task

    send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_metadata_updates_only_send_latest_artwork() -> None:
    """An older slow artwork render cannot overwrite a newer track update."""
    player = _make_player()
    stream = AirPlayStream(player)
    process = MagicMock()
    process.closed = False
    stream._cli_proc = process
    first_render_started = asyncio.Event()
    release_first_render = asyncio.Event()

    old_metadata = MagicMock(
        title="Old track",
        artist="Artist",
        album="Album",
        duration=180,
        image_url="old-image",
    )
    new_metadata = MagicMock(
        title="New track",
        artist="Artist",
        album="Album",
        duration=180,
        image_url="new-image",
    )

    async def _prepare_artwork(image_url: str, _generation: int) -> str:
        if image_url == "old-image":
            first_render_started.set()
            await release_first_render.wait()
            return "old.jpg"
        return "new.jpg"

    with (
        patch.object(stream.commands_pipe, "write", new_callable=AsyncMock) as write_command,
        patch.object(
            stream,
            "_prepare_artwork",
            new_callable=AsyncMock,
            side_effect=_prepare_artwork,
        ),
    ):
        old_task = asyncio.create_task(stream.send_metadata(0, old_metadata))
        await first_render_started.wait()
        new_task = asyncio.create_task(stream.send_metadata(0, new_metadata))
        await asyncio.sleep(0)
        assert stream._metadata_generation == 2
        release_first_render.set()
        await asyncio.gather(old_task, new_task)

    commands = [call.args[0].decode() for call in write_command.await_args_list]
    assert any("TITLE=New track" in command for command in commands)
    assert not any("ARTWORK=old.jpg" in command for command in commands)
    assert commands[-1] == "ARTWORK=new.jpg\n"


@pytest.mark.asyncio
async def test_metadata_revert_resends_text_after_superseded_artwork() -> None:
    """Reverting while artwork renders restores the previously displayed track text."""
    player = _make_player()
    stream = AirPlayStream(player)
    process = MagicMock()
    process.closed = False
    stream._cli_proc = process
    first_metadata = MagicMock(
        title="First track",
        artist="Artist",
        album="Album",
        duration=180,
        image_url=None,
    )
    second_metadata = MagicMock(
        title="Second track",
        artist="Artist",
        album="Album",
        duration=180,
        image_url="second-image",
    )
    first_checksum = "First track|Artist|Album|180|None"
    stream._metadata_text_checksum = first_checksum
    stream._pending_metadata_checksum = first_checksum
    artwork_started = asyncio.Event()
    release_artwork = asyncio.Event()

    async def _prepare_artwork(_image_url: str, _generation: int) -> str:
        artwork_started.set()
        await release_artwork.wait()
        return "second.jpg"

    with (
        patch.object(stream.commands_pipe, "write", new_callable=AsyncMock) as write_command,
        patch.object(
            stream,
            "_prepare_artwork",
            new_callable=AsyncMock,
            side_effect=_prepare_artwork,
        ),
    ):
        second_task = asyncio.create_task(stream.send_metadata(0, second_metadata))
        await artwork_started.wait()
        revert_task = asyncio.create_task(stream.send_metadata(0, first_metadata))
        await asyncio.sleep(0)
        release_artwork.set()
        await asyncio.gather(second_task, revert_task)

    metadata_commands = [
        call.args[0].decode()
        for call in write_command.await_args_list
        if "ACTION=SENDMETA" in call.args[0].decode()
    ]
    assert "TITLE=Second track" in metadata_commands[0]
    assert "TITLE=First track" in metadata_commands[-1]


@pytest.mark.asyncio
async def test_repeated_metadata_retries_superseded_artwork() -> None:
    """A B-to-C-to-B update sequence still applies B artwork after supersession."""
    player = _make_player()
    stream = AirPlayStream(player)
    process = MagicMock()
    process.closed = False
    stream._cli_proc = process
    initial_text_checksum = "item-initial|Initial|Artist|Album"
    initial_checksum = f"{initial_text_checksum}|initial-image"
    stream._metadata_artwork_checksum = "initial-image"
    stream._metadata_text_checksum = initial_text_checksum
    stream._pending_metadata_checksum = initial_checksum
    metadata_b = MagicMock(
        queue_item_id="item-b",
        title="Track B",
        artist="Artist",
        album="Album",
        duration=180,
        image_url="b-image",
    )
    metadata_c = MagicMock(
        queue_item_id="item-c",
        title="Track C",
        artist="Artist",
        album="Album",
        duration=180,
        image_url="c-image",
    )
    first_artwork_started = asyncio.Event()
    release_first_artwork = asyncio.Event()
    c_artwork_started = asyncio.Event()
    release_c_artwork = asyncio.Event()
    b_render_count = 0

    async def _prepare_artwork(image_url: str, _generation: int) -> str:
        nonlocal b_render_count
        if image_url == "b-image":
            b_render_count += 1
            if b_render_count == 1:
                first_artwork_started.set()
                await release_first_artwork.wait()
                return "b-stale.jpg"
            return "b-final.jpg"
        c_artwork_started.set()
        await release_c_artwork.wait()
        return "c.jpg"

    with (
        patch.object(stream.commands_pipe, "write", new_callable=AsyncMock) as write_command,
        patch.object(
            stream,
            "_prepare_artwork",
            new_callable=AsyncMock,
            side_effect=_prepare_artwork,
        ) as prepare_artwork,
    ):
        first_b_task = asyncio.create_task(stream.send_metadata(0, metadata_b))
        await first_artwork_started.wait()
        c_task = asyncio.create_task(stream.send_metadata(0, metadata_c))
        await c_artwork_started.wait()
        final_b_task = asyncio.create_task(stream.send_metadata(0, metadata_b))
        await final_b_task
        release_first_artwork.set()
        release_c_artwork.set()
        await asyncio.gather(first_b_task, c_task)

    rendered_images = [args.args[0] for args in prepare_artwork.await_args_list]
    commands = [args.args[0].decode() for args in write_command.await_args_list]
    assert rendered_images == ["b-image", "c-image", "b-image"]
    assert "ARTWORK=b-stale.jpg\n" not in commands
    assert "ARTWORK=c.jpg\n" not in commands
    assert commands[-1] == "ARTWORK=b-final.jpg\n"
    assert stream._metadata_artwork_checksum == "b-image"


@pytest.mark.asyncio
async def test_send_metadata_passes_cached_artwork_path_to_binary() -> None:
    """The ARTWORK command passes the absolute cache path returned by preparation."""
    player = _make_player()
    stream = AirPlayStream(player)
    metadata = MagicMock(
        duration=180,
        title="Track",
        artist="Artist",
        album="Album",
        image_url="https://example.com/artwork.png",
    )
    cached_path = "/cache/thumbnails/artwork_flat.jpg"
    send_command = AsyncMock()

    with (
        patch.object(stream, "_prepare_artwork", new=AsyncMock(return_value=cached_path)),
        patch.object(stream, "send_cli_command", new=send_command),
    ):
        await stream.send_metadata(None, metadata)

    assert send_command.await_args_list[-1] == call(f"ARTWORK={cached_path}")


@pytest.mark.asyncio
async def test_failed_artwork_delivery_is_retried() -> None:
    """A dropped ARTWORK command remains pending for the next metadata update."""
    stream = AirPlayStream(_make_player())
    metadata = MagicMock(
        queue_item_id="item-1",
        duration=180,
        title="Track",
        artist="Artist",
        album="Album",
        image_url="image",
    )
    artwork_path = "/cache/thumbnails/artwork.jpg"

    with (
        patch.object(
            stream,
            "_prepare_artwork",
            new_callable=AsyncMock,
            return_value=artwork_path,
        ) as prepare_artwork,
        patch.object(
            stream,
            "send_cli_command",
            new_callable=AsyncMock,
            side_effect=[True, False, True],
        ) as send_command,
    ):
        await stream.send_metadata(None, metadata)
        assert stream._metadata_artwork_checksum == ""
        await stream.send_metadata(None, metadata)

    assert prepare_artwork.await_count == 2
    assert [args.args[0] for args in send_command.await_args_list].count(
        f"ARTWORK={artwork_path}"
    ) == 2
    assert stream._metadata_artwork_checksum == "image"


@pytest.mark.asyncio
async def test_text_refinement_keeps_delivered_artwork_settled() -> None:
    """A text-only metadata update after delivery does not re-render unchanged art."""
    stream = AirPlayStream(_make_player())
    metadata = MagicMock(
        duration=180,
        title="Track",
        artist="Artist",
        album="Album",
        image_url="image",
    )
    refined = MagicMock(
        queue_item_id=metadata.queue_item_id,
        duration=180,
        title="Track (Remastered)",
        artist="Artist",
        album="Album",
        image_url="image",
    )
    artwork_path = "/cache/thumbnails/artwork.jpg"

    with (
        patch.object(
            stream,
            "_prepare_artwork",
            new_callable=AsyncMock,
            return_value=artwork_path,
        ) as prepare_artwork,
        patch.object(
            stream,
            "send_cli_command",
            new_callable=AsyncMock,
            return_value=True,
        ) as send_command,
    ):
        await stream.send_metadata(None, metadata)
        assert stream._metadata_artwork_checksum == "image"
        # the refinement bumps the metadata generation (pending identity
        # changed), which before the identity settle re-armed the artwork
        await stream.send_metadata(None, refined)

    prepare_artwork.assert_awaited_once()
    commands = [args.args[0] for args in send_command.await_args_list]
    assert commands.count(f"ARTWORK={artwork_path}") == 1
    assert "TITLE=Track (Remastered)" in commands[-1]


# --- Structured connect failures reported by the binary ---


@pytest.mark.asyncio
async def test_connect_error_status_line_is_parsed() -> None:
    """The machine-readable failure line is captured with all of its fields."""
    stream = AirPlayStream(_make_player())

    stream._handle_status_line(
        '[STATUS] error code=auth_required http=401 detail="RTSP setup rejected"'
    )

    assert stream._connect_error == CliError("auth_required", 401, "RTSP setup rejected")


@pytest.mark.asyncio
async def test_started_ack_status_line_parsing() -> None:
    """A started ack releases the START wait; a malformed one carries no details."""
    stream = AirPlayStream(_make_player())

    stream._handle_status_line(
        "[STATUS] started requested_unix_ms=1750000000000 at_unix_ms=1750000000004"
    )
    assert stream._started.is_set()
    assert stream._start_ack == (1750000000000, 1750000000004)

    stream._started.clear()
    stream._start_ack = None
    stream._handle_status_line("[STATUS] started requested_unix_ms=garbage at_unix_ms=1")
    assert stream._started.is_set()
    assert stream._start_ack is None


# --- Post-commit anchor verification ---


def test_anchor_corrected_status_line_rebases_the_position() -> None:
    """A correction carrying a content cut moves the reported-position base by it."""
    stream = AirPlayStream(_make_player())
    stream._start_position = 12.0

    ended = stream._handle_status_line(
        "[STATUS] anchor_corrected requested_unix_ms=1750000000000 "
        "from_unix_ms=1750000000400 at_unix_ms=1750000000900 content_cut_ms=500"
    )

    assert ended is False
    assert stream._start_position == 12.5


def test_anchor_corrected_status_line_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    """The correction is logged loudly, including the display name and the delta."""
    stream = AirPlayStream(_make_player())

    with caplog.at_level(logging.WARNING):
        stream._handle_status_line(
            "[STATUS] anchor_corrected requested_unix_ms=0 "
            "from_unix_ms=1750000000400 at_unix_ms=1750000000900 content_cut_ms=500"
        )

    assert "Player A" in caplog.text
    assert "+500 ms" in caplog.text


def test_anchor_corrected_status_line_tolerates_malformed_line() -> None:
    """A malformed anchor_corrected line is dropped instead of raising or rebasing."""
    stream = AirPlayStream(_make_player())
    stream._start_position = 12.0

    ended = stream._handle_status_line("[STATUS] anchor_corrected requested_unix_ms=garbage")

    assert ended is False
    assert stream._start_position == 12.0


def test_content_cut_short_rebases_the_position_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cut that ended early gives back the ms the correction over-advanced the base by."""
    stream = AirPlayStream(_make_player())
    stream._start_position = 12.0
    stream._handle_status_line(
        "[STATUS] anchor_corrected requested_unix_ms=1750000000000 "
        "from_unix_ms=1750000000400 at_unix_ms=1750000000900 content_cut_ms=500"
    )
    assert stream._start_position == 12.5

    with caplog.at_level(logging.WARNING):
        ended = stream._handle_status_line(
            "[STATUS] content_cut requested_ms=500 cut_ms=180 cut_bytes=31752 drain_ms=210"
        )

    assert ended is False
    assert stream._start_position == pytest.approx(12.18)
    assert "AirPlay content cut" in caplog.text
    assert "Player A" in caplog.text
    assert "320 ms short" in caplog.text


def test_content_cut_in_full_leaves_the_position_alone(caplog: pytest.LogCaptureFixture) -> None:
    """A cut that took what it asked for needs no correction and stays quiet."""
    stream = AirPlayStream(_make_player())
    stream._start_position = 12.0
    stream._handle_status_line(
        "[STATUS] anchor_corrected requested_unix_ms=1750000000000 "
        "from_unix_ms=1750000000400 at_unix_ms=1750000000900 content_cut_ms=500"
    )

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        # a few ms below the request is byte quantization, not a short cut
        stream._handle_status_line(
            "[STATUS] content_cut requested_ms=500 cut_ms=498 cut_bytes=87887 drain_ms=505"
        )

    assert stream._start_position == 12.5
    assert caplog.text == ""


def test_content_cut_after_a_new_anchor_is_not_reconciled() -> None:
    """A cut settling after a START must not be taken off that START's absolute base."""
    stream = AirPlayStream(_make_player())
    stream._start_position = 12.0
    stream._handle_status_line(
        "[STATUS] anchor_corrected requested_unix_ms=1750000000000 "
        "from_unix_ms=1750000000400 at_unix_ms=1750000000900 content_cut_ms=500"
    )
    stream.rebase_position(30_000)

    stream._handle_status_line(
        "[STATUS] content_cut requested_ms=500 cut_ms=0 cut_bytes=0 drain_ms=12"
    )

    assert stream._start_position == 30.0


def test_content_cut_status_line_tolerates_malformed_line() -> None:
    """A malformed content_cut line is dropped instead of raising or rebasing."""
    stream = AirPlayStream(_make_player())
    stream._start_position = 12.0
    stream._handle_status_line(
        "[STATUS] anchor_corrected requested_unix_ms=1750000000000 "
        "from_unix_ms=1750000000400 at_unix_ms=1750000000900 content_cut_ms=500"
    )

    ended = stream._handle_status_line("[STATUS] content_cut requested_ms=500 cut_ms=garbage")

    assert ended is False
    assert stream._start_position == 12.5


def test_clock_verified_status_line_is_debug_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A clock_verified line needs no server action beyond a debug note of the margin."""
    stream = AirPlayStream(_make_player())

    with caplog.at_level(logging.DEBUG):
        ended = stream._handle_status_line("[STATUS] clock_verified margin_ms=42")

    assert ended is False
    assert "42" in caplog.text


@pytest.mark.asyncio
async def test_connect_error_status_line_tolerates_missing_fields() -> None:
    """A failure line without http/detail still yields the reported code."""
    stream = AirPlayStream(_make_player())

    stream._handle_status_line("[STATUS] error code=connect_failed")

    assert stream._connect_error == CliError("connect_failed", 0, "")


@pytest.mark.asyncio
async def test_auth_required_surfaces_password_required_error() -> None:
    """A device asking for a password produces an actionable, translated error."""
    stream = AirPlayStream(_make_player())
    stream._handle_status_line('[STATUS] error code=auth_required http=401 detail="no password"')
    stream._process_ended.set()

    with (
        patch.object(stream, "_cli_proc", MagicMock()),
        pytest.raises(PlayerCommandFailed) as err,
    ):
        await stream.wait_for_connection()

    assert err.value.translation_key == "password_required"


@pytest.mark.asyncio
async def test_auth_failed_surfaces_authentication_failed_error() -> None:
    """A rejected password is reported as an authentication failure, not a timeout."""
    stream = AirPlayStream(_make_player())
    stream._handle_status_line('[STATUS] error code=auth_failed http=401 detail="bad password"')
    stream._process_ended.set()

    with (
        patch.object(stream, "_cli_proc", MagicMock()),
        pytest.raises(PlayerCommandFailed) as err,
    ):
        await stream.wait_for_connection()

    assert err.value.translation_key == "authentication_failed"


@pytest.mark.asyncio
async def test_generic_connect_failure_keeps_the_timeout_semantics() -> None:
    """A non-auth failure keeps raising the plain timeout its callers already handle."""
    stream = AirPlayStream(_make_player())
    stream._handle_status_line('[STATUS] error code=connect_failed http=0 detail="no route"')
    stream._process_ended.set()

    with patch.object(stream, "_cli_proc", MagicMock()), pytest.raises(TimeoutError):
        await stream.wait_for_connection()


@pytest.mark.asyncio
async def test_dead_process_fails_the_connect_wait_immediately() -> None:
    """
    An older binary reports no reason at all, so the wait keeps its timeout error.

    It must still end the moment the process is gone instead of running out the
    full connect timeout.
    """
    stream = AirPlayStream(_make_player())
    stream._process_ended.set()  # process died without emitting a [STATUS] error line

    started = asyncio.get_running_loop().time()
    with patch.object(stream, "_cli_proc", MagicMock()), pytest.raises(TimeoutError):
        await stream.wait_for_connection()

    assert asyncio.get_running_loop().time() - started < 1


@pytest.mark.asyncio
async def test_auth_failed_marks_the_stored_password_invalid() -> None:
    """A rejected password is persisted so the player keeps offering its setup action."""
    player = _make_player()
    stream = AirPlayStream(player)

    stream._handle_status_line('[STATUS] error code=auth_failed http=401 detail="bad password"')

    player.set_password_invalid.assert_called_once_with(True)


@pytest.mark.asyncio
async def test_auth_required_also_marks_the_password_as_needed() -> None:
    """
    A device that demanded a password we could not supply flips into setup.

    Devices can enforce a password without announcing it (stale TXT records), so
    the runtime signal must set the marker too - it is the only reliable one.
    """
    player = _make_player()
    stream = AirPlayStream(player)

    stream._handle_status_line('[STATUS] error code=auth_required http=401 detail="no password"')

    player.set_password_invalid.assert_called_once_with(True)


@pytest.mark.asyncio
async def test_plain_connect_failures_leave_the_password_marker_alone() -> None:
    """A non-authentication failure says nothing about the stored password."""
    player = _make_player()
    stream = AirPlayStream(player)

    stream._handle_status_line('[STATUS] error code=connect_failed http=0 detail="no route"')

    player.set_password_invalid.assert_not_called()


@pytest.mark.asyncio
async def test_successful_connect_clears_the_password_marker() -> None:
    """Whatever the device accepted is a working password."""
    player = _make_player()
    stream = AirPlayStream(player)

    stream._handle_status_line("[STATUS] connected")

    assert stream.connected is True
    player.set_password_invalid.assert_called_once_with(False)


# --- Password preflight ---


@pytest.mark.asyncio
async def test_connect_refuses_password_device_without_password_or_credentials() -> None:
    """A password-protected AirPlay 2 device with nothing to authenticate never spawns a process."""
    player = _make_player()
    player.password_required = True
    player.get_setup_value = MagicMock(return_value=None)
    stream = AirPlayStream(player)

    with pytest.raises(PlayerCommandFailed) as err:
        await stream.connect()

    assert err.value.translation_key == "password_required"
    assert stream._cli_proc is None


@pytest.mark.asyncio
async def test_password_preflight_passes_with_password_or_credentials() -> None:
    """Either a configured password or stored credentials let the connect proceed."""
    player = _make_player()
    player.password_required = True
    player.get_setup_value = MagicMock(return_value=None)
    player.config.get_value = MagicMock(
        side_effect=lambda key, default=None: "s3cret" if key == CONF_PASSWORD else default
    )
    AirPlayStream(player)._check_password_preflight()

    # credentials alone are enough: the binary's pair-verify leg may still succeed
    player.config.get_value = MagicMock(side_effect=lambda _key, default=None: default)
    player.get_setup_value = MagicMock(
        side_effect=lambda key, default=None: (
            "ab" * 96 if key == CONF_AIRPLAY_CREDENTIALS else default
        )
    )
    AirPlayStream(player)._check_password_preflight()


@pytest.mark.asyncio
async def test_password_preflight_skipped_for_raop() -> None:
    """The preflight only guards the native AirPlay 2 flow; RAOP carries its own password."""
    player = _make_player()
    player.protocol = StreamingProtocol.RAOP
    player.password_required = True
    player.get_setup_value = MagicMock(return_value=None)

    AirPlayStream(player)._check_password_preflight()
