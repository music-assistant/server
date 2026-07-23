"""Unit tests for the AirPlay stream CLI argument assembly."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from music_assistant_models.enums import ContentType, PlaybackState
from music_assistant_models.media_items import AudioFormat

from music_assistant.providers.airplay.constants import (
    AIRPLAY_ARTWORK_SIZE,
    CONF_ENCRYPTION,
    AirPlayRemoteCommand,
    StreamingProtocol,
)
from music_assistant.providers.airplay.stream import AirPlayStream

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
        return await stream._build_cli_args()


def _arg_value(args: list[str], flag: str) -> Any:
    """Return the value following the given flag in the argument list."""
    return args[args.index(flag) + 1]


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
        args = await stream._build_cli_args()

    assert _arg_value(args, "--samplerate") == "48000"
    assert _arg_value(args, "--bitdepth") == "24"
    # the ffmpeg pipe format must be the 32-bit container (binary truncates to 24)
    assert stream.pcm_format.content_type == ContentType.PCM_S32LE


@pytest.mark.asyncio
async def test_cli_args_raop_only_device() -> None:
    """A device without an _airplay._tcp service targets the RAOP service, no --txt."""
    player = _make_player()
    player.airplay_discovery_info = None
    player.protocol = StreamingProtocol.RAOP
    args = await _build_args(player)

    assert _arg_value(args, "--protocol") == "auto"
    assert _arg_value(args, "--port") == "5000"
    assert "--txt" not in args
    assert "--name" not in args
    assert "--encrypt" in args


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
async def test_start_queues_text_metadata_before_connection() -> None:
    """Text metadata is queued as soon as cliairplay starts."""
    player = _make_player()
    stream = AirPlayStream(player)
    metadata = MagicMock(corrected_elapsed_time=12.5)
    player.current_media = metadata
    process = MagicMock(closed=False)
    process.start = AsyncMock()
    operation_order: list[str] = []

    async def create_pipe() -> None:
        operation_order.append("pipe")

    async def start_process() -> None:
        operation_order.append("process")

    async def send_metadata(*_args: Any, **_kwargs: Any) -> None:
        operation_order.append("metadata")

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
        patch.object(stream, "send_metadata", side_effect=send_metadata) as send_metadata_mock,
    ):
        await stream.start()

    assert operation_order == ["pipe", "process", "metadata"]
    send_metadata_mock.assert_awaited_once_with(12, metadata, send_artwork=False)


@pytest.mark.asyncio
async def test_start_failure_cleans_up_process_and_pipe() -> None:
    """A metadata write failure cannot leave a live cliairplay process or FIFO."""
    player = _make_player()
    stream = AirPlayStream(player)
    metadata = MagicMock(corrected_elapsed_time=0)
    player.current_media = metadata
    process = MagicMock(closed=False)
    process.start = AsyncMock()
    process.kill = AsyncMock()

    def consume_task(awaitable: Any) -> MagicMock:
        awaitable.close()
        task = MagicMock()
        task.done.return_value = True
        return task

    player.provider.mass.create_task.side_effect = consume_task
    with (
        patch.object(stream, "_build_cli_args", new_callable=AsyncMock, return_value=["binary"]),
        patch(
            "music_assistant.providers.airplay.stream.AsyncProcess",
            return_value=process,
        ),
        patch.object(stream.commands_pipe, "create", new_callable=AsyncMock),
        patch.object(stream.commands_pipe, "remove", new_callable=AsyncMock) as remove_pipe,
        patch.object(
            stream,
            "send_metadata",
            new_callable=AsyncMock,
            side_effect=OSError("metadata write failed"),
        ),
        pytest.raises(OSError, match="metadata write failed"),
    ):
        await stream.start()

    process.kill.assert_awaited_once()
    remove_pipe.assert_awaited_once()
    assert stream._cli_proc is None
    assert stream._cleanup_complete is True


@pytest.mark.asyncio
async def test_generation_zero_uses_prepare_and_start_commands() -> None:
    """The first generation is prepared, primed, and started over the command pipe."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock(closed=False)
    stream._connected.set()

    with patch.object(stream, "_write_cli_command", new_callable=AsyncMock) as write_command:
        await stream.prepare_generation(0, "-", 12_000)
        assert stream._handle_status_line("[STATUS] primed generation=0") is False
        assert await stream.wait_generation_primed(0)
        await stream.start_generation(0, 12_000, START_UNIX_MS)

    assert [call.args[0] for call in write_command.await_args_list] == [
        "GENERATION=0\nAUDIO=-\nPOSITION_MS=12000\nACTION=PREPARE",
        f"GENERATION=0\nSTART_UNIX_MS={START_UNIX_MS}\nACTION=START",
    ]


@pytest.mark.asyncio
async def test_generation_cannot_start_before_primed() -> None:
    """A START command is rejected until that generation has reported primed."""
    stream = AirPlayStream(_make_player())
    stream._cli_proc = MagicMock(closed=False)
    stream._connected.set()

    with (
        patch.object(stream, "_write_cli_command", new_callable=AsyncMock) as write_command,
        pytest.raises(RuntimeError, match="before it is primed"),
    ):
        await stream.start_generation(0, 0, START_UNIX_MS)

    write_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_generation_cannot_start_after_process_exit() -> None:
    """A primed generation is not started after its CLI process exits."""
    stream = AirPlayStream(_make_player())
    process = MagicMock(closed=False)
    stream._cli_proc = process
    stream._connected.set()

    with patch.object(stream, "_write_cli_command", new_callable=AsyncMock) as write_command:
        await stream.prepare_generation(0, "-", 0)
        stream._handle_status_line("[STATUS] primed generation=0")
        process.closed = True
        with pytest.raises(RuntimeError, match="without a connected cliairplay process"):
            await stream.start_generation(0, 0, START_UNIX_MS)

    write_command.assert_awaited_once_with("GENERATION=0\nAUDIO=-\nPOSITION_MS=0\nACTION=PREPARE")


def test_generation_zero_elapsed_includes_position_once() -> None:
    """Generation-0 progress is based on its media position without session offset."""
    player = _make_player()
    stream = AirPlayStream(player)
    stream._generation_position = 12.0

    stream._update_elapsed(1.5)

    player.set_state_from_stream.assert_called_once_with(
        state=PlaybackState.PLAYING,
        elapsed_time=13.5,
        stream=stream,
    )


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

    send_command.assert_awaited_once()
    assert send_command.await_args is not None
    assert "TITLE=Track" in send_command.await_args.args[0]
    send_artwork.assert_not_awaited()


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
        render_task = asyncio.create_task(
            stream._render_and_send_artwork("image", "metadata-checksum", 1)
        )
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
    stream._metadata_checksum = first_checksum
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
    initial_checksum = "Initial|Artist|Album|180|initial-image"
    stream._metadata_checksum = initial_checksum
    stream._metadata_text_checksum = initial_checksum
    stream._pending_metadata_checksum = initial_checksum
    metadata_b = MagicMock(
        title="Track B",
        artist="Artist",
        album="Album",
        duration=180,
        image_url="b-image",
    )
    metadata_c = MagicMock(
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
    assert stream._metadata_checksum == "Track B|Artist|Album|180|b-image"


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
