"""Unit tests for AirPlay stream session late-join logic."""

import asyncio
import time
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.airplay.constants import StreamingProtocol
from music_assistant.providers.airplay.stream_session import AirPlayStreamSession

PCM_SAMPLE_SIZE = 176400  # 44.1kHz / 16-bit / 2ch


def _make_session(
    start_time: float,
    seconds_streamed: float,
) -> AirPlayStreamSession:
    """
    Create a stream session for testing.

    :param start_time: The wall-clock time when the stream was started.
    :param seconds_streamed: How many seconds of audio have been streamed.
    """
    prov = MagicMock()

    pcm_format = MagicMock()
    pcm_format.pcm_sample_size = PCM_SAMPLE_SIZE
    pcm_format.sample_rate = 44100
    pcm_format.bit_depth = 16
    pcm_format.channels = 2

    leader = MagicMock()
    leader.player_id = "leader"
    leader.protocol = StreamingProtocol.RAOP
    leader.stream = MagicMock()
    leader.stream.running = True
    leader.stream.connected = True
    leader.stream.wait_audio_present = AsyncMock(return_value=True)
    leader.stream.cumulative_shift_seconds = 0.0
    leader.config.get_value = MagicMock(return_value=0)

    session = AirPlayStreamSession(prov, [leader], pcm_format, MagicMock(elapsed_time=0))
    session.start_time = start_time
    session.seconds_streamed = seconds_streamed
    session.start_unix_ms = 1  # dummy
    session.wait_start = 2.0

    return session


def _make_late_joiner(wait_start_ms: int = 2000) -> MagicMock:
    """Create a mock AirPlay player for late-join testing."""
    player = MagicMock()
    player.player_id = "late_joiner"
    player.protocol = StreamingProtocol.RAOP
    player.wait_start = wait_start_ms
    player.stream = None
    player.config = MagicMock()
    player.config.get_value = MagicMock(return_value=0)
    return player


def _setup_stream(player: MagicMock) -> Any:
    """Return a side_effect callable that sets up the stream mock on the player."""

    def _side_effect(*_args: Any, **_kwargs: Any) -> None:
        player.stream = MagicMock()
        player.stream.running = True
        player.stream.connected = True
        player.stream.wait_for_connection = AsyncMock()
        player.stream.wait_audio_present = AsyncMock(return_value=True)
        player.stream.flush = AsyncMock(return_value=True)
        player.stream.start = AsyncMock()
        player.stream.cumulative_shift_seconds = 0.0

    return _side_effect


def _captured_start_at(player: MagicMock) -> float:
    """Return the start time passed to the player's START command."""
    start_unix_ms = player.stream.start.call_args[0][0]
    assert isinstance(start_unix_ms, int)
    return start_unix_ms / 1000


@pytest.mark.asyncio
async def test_initial_client_failure_stops_started_clients() -> None:
    """A partial group startup failure cancels siblings before session teardown."""
    session = _make_session(time.time(), 0)
    first_player: Any = session.sync_clients[0]
    first_player.wait_start = 0
    second_player = MagicMock(wait_start=0)
    session.sync_clients.append(second_player)
    first_started = asyncio.Event()
    first_cancelled = asyncio.Event()

    async def audio_source() -> AsyncGenerator[bytes]:
        yield b""

    async def start_client(player: Any, *_args: Any) -> None:
        if player is first_player:
            first_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                first_cancelled.set()
                raise
        await first_started.wait()
        raise OSError("process failed")

    with (
        patch.object(
            session,
            "_start_client",
            new_callable=AsyncMock,
            side_effect=start_client,
        ),
        patch.object(session, "stop", new_callable=AsyncMock) as stop_session,
        pytest.raises(PlayerCommandFailed, match="Playback failed to start"),
    ):
        await session.start(audio_source())

    assert first_cancelled.is_set()
    stop_session.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_protocol", "second_protocol"),
    [
        (StreamingProtocol.RAOP, StreamingProtocol.RAOP),
        (StreamingProtocol.AIRPLAY2, StreamingProtocol.AIRPLAY2),
        (StreamingProtocol.RAOP, StreamingProtocol.AIRPLAY2),
    ],
)
async def test_initial_group_waits_for_every_member_before_shared_start(
    first_protocol: StreamingProtocol,
    second_protocol: StreamingProtocol,
) -> None:
    """Every member connects before one shared START anchors the group."""
    session = _make_session(0, 0)
    first_player: Any = session.sync_clients[0]
    first_player.player_id = "first"
    first_player.protocol = first_protocol
    first_player.wait_start = 2500
    first_player.config.get_value = MagicMock(return_value=0)
    second_player = MagicMock(player_id="second", wait_start=2500)
    second_player.protocol = second_protocol
    second_player.config.get_value = MagicMock(return_value=0)
    session.sync_clients.append(second_player)
    session.media.elapsed_time = 12
    operations: list[str] = []

    async def start_client(player: MagicMock, _use_shared_ptp: bool) -> None:
        stream = MagicMock(running=True)

        async def wait_for_connection() -> None:
            operations.append(f"connected:{player.player_id}")

        async def start(start_unix_ms: int, position_ms: int) -> None:
            assert position_ms == 12_000
            operations.append(f"started:{player.player_id}:{start_unix_ms}")

        stream.wait_for_connection = AsyncMock(side_effect=wait_for_connection)
        stream.wait_audio_present = AsyncMock(return_value=True)
        stream.start = AsyncMock(side_effect=start)
        player.stream = stream

    with (
        patch.object(session, "_start_client", side_effect=start_client),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "_resolve_shared_ptp", new_callable=AsyncMock, return_value=False),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=100.0),
    ):
        await session.start(MagicMock())

    last_connected = max(i for i, op in enumerate(operations) if op.startswith("connected:"))
    first_start = min(i for i, op in enumerate(operations) if op.startswith("started:"))
    assert last_connected < first_start
    # start = now (100_000 ms) + the group start lead (500 ms), one shared instant
    starts = {int(op.rsplit(":", 1)[1]) for op in operations if op.startswith("started:")}
    assert starts == {100_500}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol",
    [StreamingProtocol.RAOP, StreamingProtocol.AIRPLAY2],
)
async def test_initial_single_player_starts_after_connect(
    protocol: StreamingProtocol,
) -> None:
    """A standalone player is anchored with a single START once connected."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.player_id = "solo"
    player.protocol = protocol
    player.wait_start = 2500
    player.config.get_value = MagicMock(return_value=0)
    stream = MagicMock(running=True)
    stream.wait_for_connection = AsyncMock()
    stream.wait_audio_present = AsyncMock(return_value=True)
    stream.flush = AsyncMock(return_value=True)
    stream.start = AsyncMock()

    async def start_client(_player: MagicMock, _use_shared_ptp: bool) -> None:
        player.stream = stream

    with (
        patch.object(session, "_start_client", side_effect=start_client),
        patch.object(session, "_resolve_shared_ptp", new_callable=AsyncMock, return_value=False),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=200.0),
    ):
        await session.start(MagicMock())

    # start = now (200_000 ms) + the solo start lead (250 ms), position 0
    stream.start.assert_awaited_once_with(200_250, 0)


@pytest.mark.asyncio
async def test_initial_connection_failure_never_starts_partial_group() -> None:
    """If any member fails to connect, no member receives START and the group is stopped."""
    session = _make_session(0, 0)
    first_player: Any = session.sync_clients[0]
    first_player.protocol = StreamingProtocol.RAOP
    first_player.wait_start = 2500
    second_player = MagicMock(player_id="second", wait_start=2500)
    second_player.protocol = StreamingProtocol.RAOP
    session.sync_clients.append(second_player)

    async def start_client(player: MagicMock, _use_shared_ptp: bool) -> None:
        stream = MagicMock(running=True)
        if player is second_player:
            stream.wait_for_connection = AsyncMock(side_effect=TimeoutError("connect timeout"))
        else:
            stream.wait_for_connection = AsyncMock()
        stream.start = AsyncMock()
        player.stream = stream

    with (
        patch.object(session, "_start_client", side_effect=start_client),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "stop", new_callable=AsyncMock) as stop_session,
        pytest.raises(PlayerCommandFailed, match="Playback failed to start"),
    ):
        await session.start(MagicMock())

    for player in session.sync_clients:
        stream: Any = player.stream
        stream.start.assert_not_awaited()
    stop_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_initial_connection_cancellation_never_starts_group() -> None:
    """Cancellation while connecting cleans up without anchoring playback."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.player_id = "solo"
    player.protocol = StreamingProtocol.RAOP
    player.wait_start = 2500
    connection_waiting = asyncio.Event()
    stream = MagicMock(running=True)

    async def wait_for_connection() -> None:
        connection_waiting.set()
        await asyncio.Event().wait()

    stream.wait_for_connection = AsyncMock(side_effect=wait_for_connection)
    stream.start = AsyncMock()

    async def start_client(_player: MagicMock, _use_shared_ptp: bool) -> None:
        player.stream = stream

    with (
        patch.object(session, "_start_client", side_effect=start_client),
        patch.object(session, "stop", new_callable=AsyncMock) as stop_session,
    ):
        start_task = asyncio.create_task(session.start(MagicMock()))
        await connection_waiting.wait()
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task

    stream.start.assert_not_awaited()
    stop_session.assert_awaited_once()


@pytest.mark.parametrize(
    "protocols",
    [
        (StreamingProtocol.RAOP,),
        (StreamingProtocol.AIRPLAY2,),
        (StreamingProtocol.RAOP, StreamingProtocol.AIRPLAY2),
    ],
)
def test_warm_replace_supports_every_streaming_protocol(
    protocols: tuple[StreamingProtocol, ...],
) -> None:
    """Connected legacy RAOP, AirPlay 2 and mixed sessions can replace warm."""
    session = _make_session(0, 0)
    players: Any = []
    for index, protocol in enumerate(protocols):
        player = MagicMock()
        player.player_id = f"player-{index}"
        player.protocol = protocol
        player.stream = MagicMock(running=True, connected=True)
        players.append(player)
    session.sync_clients = players

    assert session.can_replace(players, session.pcm_format)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocols",
    [
        (StreamingProtocol.AIRPLAY2,),
        (StreamingProtocol.RAOP,),
        (StreamingProtocol.AIRPLAY2, StreamingProtocol.AIRPLAY2),
        (StreamingProtocol.RAOP, StreamingProtocol.RAOP),
        (StreamingProtocol.RAOP, StreamingProtocol.AIRPLAY2),
    ],
)
async def test_standby_supports_every_connected_streaming_protocol(
    protocols: tuple[StreamingProtocol, ...],
) -> None:
    """Connected legacy RAOP, AirPlay 2 and mixed sessions can enter standby."""
    session = _make_session(0, 0)
    players: Any = []
    for index, protocol in enumerate(protocols):
        player = MagicMock()
        player.player_id = f"player-{index}"
        player.protocol = protocol
        player.stream = MagicMock(running=True, connected=True)
        player.stream.send_cli_command = AsyncMock(return_value=True)
        players.append(player)
    session.sync_clients = players

    assert await session.standby()
    for player in players:
        player.stream.send_cli_command.assert_awaited_once_with("ACTION=STANDBY")
        player.set_state_from_stream.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocols",
    [
        (StreamingProtocol.RAOP,),
        (StreamingProtocol.AIRPLAY2,),
        (StreamingProtocol.RAOP, StreamingProtocol.AIRPLAY2),
    ],
)
async def test_standby_resumes_warm_on_existing_streams(
    protocols: tuple[StreamingProtocol, ...],
) -> None:
    """RAOP, AirPlay 2 and mixed sessions resume warm via flush-refill on parked streams."""
    session = _make_session(0, 0)
    players: Any = []
    original_streams: dict[str, MagicMock] = {}
    for index, protocol in enumerate(protocols):
        player = MagicMock()
        player.player_id = f"player-{index}"
        player.protocol = protocol
        player.config.get_value = MagicMock(return_value=0)
        player.stream = MagicMock(running=True, connected=True)
        player.stream.send_cli_command = AsyncMock(return_value=True)
        player.stream.wait_audio_present = AsyncMock(return_value=True)
        player.stream.flush = AsyncMock(return_value=True)
        player.stream.start = AsyncMock()
        players.append(player)
        original_streams[player.player_id] = player.stream
    session.sync_clients = players

    assert await session.standby()
    assert session.can_replace(players, session.pcm_format)
    media = MagicMock(elapsed_time=10)
    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=100.0),
    ):
        assert await session.replace(MagicMock(), media)

    # start = now (100_000 ms) + solo/group start lead, position 10s
    expected_start = 100_000 + (250 if len(protocols) == 1 else 500)
    for player in players:
        stream = original_streams[player.player_id]
        assert player.stream is stream
        stream.send_cli_command.assert_awaited_once_with("ACTION=STANDBY")
        stream.flush.assert_awaited_once_with()
        stream.start.assert_awaited_once_with(expected_start, 10_000)


@pytest.mark.asyncio
async def test_warm_replace_flushes_all_before_starting_any() -> None:
    """A group flushes every member and awaits all acks before any shared START."""
    session = _make_session(0, 0)
    players: list[Any] = []
    release_delayed = asyncio.Event()
    delayed_waiting = asyncio.Event()

    for player_id in ("first", "delayed"):
        player = MagicMock(player_id=player_id, protocol=StreamingProtocol.AIRPLAY2)
        player.config.get_value = MagicMock(return_value=0)
        stream = MagicMock(running=True, connected=True)

        async def flush(*, current_id: str = player_id) -> bool:
            if current_id == "delayed":
                delayed_waiting.set()
                await release_delayed.wait()
            return True

        stream.flush = AsyncMock(side_effect=flush)
        stream.wait_audio_present = AsyncMock(return_value=True)
        stream.start = AsyncMock()
        player.stream = stream
        players.append(player)
    session.sync_clients = players

    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=100.0),
    ):
        replace_task = asyncio.create_task(session.replace(MagicMock(), MagicMock(elapsed_time=0)))
        await delayed_waiting.wait()
        # one member's flush is still pending: no member may have been started yet
        for player in players:
            player.stream.start.assert_not_awaited()
        release_delayed.set()
        assert await replace_task

    for player in players:
        player.stream.flush.assert_awaited_once_with()
        # start = now (100_000 ms) + the group start lead (500 ms), position 0
        player.stream.start.assert_awaited_once_with(100_500, 0)


@pytest.mark.asyncio
async def test_warm_replace_stops_old_audio_and_ffmpeg_before_flush() -> None:
    """The old audio feed and ffmpeg are torn down before any FLUSH is sent."""
    session = _make_session(0, 0)
    operations: list[str] = []
    player: Any = session.sync_clients[0]
    player.config.get_value = MagicMock(return_value=0)
    stream = player.stream
    stream.running = True
    stream.connected = True

    async def flush() -> bool:
        operations.append("flush")
        return True

    stream.flush = AsyncMock(side_effect=flush)
    stream.start = AsyncMock()
    old_ffmpeg = MagicMock(closed=False)

    async def kill_ffmpeg() -> None:
        operations.append("ffmpeg-killed")

    old_ffmpeg.kill = AsyncMock(side_effect=kill_ffmpeg)
    session._player_ffmpeg[player.player_id] = old_ffmpeg

    async def old_audio() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            operations.append("audio-cancelled")
            raise

    session._audio_source_task = asyncio.create_task(old_audio())
    # let old_audio reach its await so the cancellation lands inside its try/except
    await asyncio.sleep(0)

    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=100.0),
    ):
        assert await session.replace(MagicMock(), MagicMock(elapsed_time=0))

    assert operations.index("audio-cancelled") < operations.index("flush")
    assert operations.index("ffmpeg-killed") < operations.index("flush")
    old_ffmpeg.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_warm_replace_flush_failure_falls_back_to_cold() -> None:
    """A member that never acknowledges its flush makes the whole replace fall back."""
    session = _make_session(0, 0)
    players: list[Any] = []
    for player_id, acked in (("ok", True), ("failed", False)):
        player = MagicMock(player_id=player_id, protocol=StreamingProtocol.RAOP)
        player.config.get_value = MagicMock(return_value=0)
        stream = MagicMock(running=True, connected=True)
        stream.flush = AsyncMock(return_value=acked)
        stream.start = AsyncMock()
        player.stream = stream
        players.append(player)
    session.sync_clients = players

    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock) as start_ffmpeg,
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
    ):
        assert await session.replace(MagicMock(), MagicMock(elapsed_time=0)) is False

    # no member is started and no fresh ffmpeg is wired once a flush is unacknowledged
    for player in players:
        player.stream.start.assert_not_awaited()
    start_ffmpeg.assert_not_awaited()


@pytest.mark.asyncio
async def test_warm_replace_start_failure_falls_back_to_cold() -> None:
    """A failed shared START after flush returns False so the caller restarts cold."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.config.get_value = MagicMock(return_value=0)
    stream = player.stream
    stream.running = True
    stream.connected = True
    stream.flush = AsyncMock(return_value=True)
    stream.start = AsyncMock(side_effect=OSError("start failed"))

    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=100.0),
    ):
        assert await session.replace(MagicMock(), MagicMock(elapsed_time=0)) is False


@pytest.mark.asyncio
async def test_start_player_ffmpeg_wires_persistent_cli_stdin() -> None:
    """The per-seek ffmpeg is wired to the member's persistent cli stdin fd and tracked."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    old_ffmpeg = MagicMock()
    old_ffmpeg.close = AsyncMock()
    session._player_ffmpeg[player.player_id] = old_ffmpeg

    stream = MagicMock()
    stream.pcm_format = session.pcm_format
    cli_proc = MagicMock()
    cli_proc.proc.stdin.transport.get_extra_info.return_value.fileno.return_value = 77
    stream._cli_proc = cli_proc
    player.stream = stream
    new_ffmpeg = MagicMock()
    new_ffmpeg.start = AsyncMock()

    with (
        patch(
            "music_assistant.providers.airplay.stream_session.get_final_output_format",
            return_value=MagicMock(),
        ),
        patch(
            "music_assistant.providers.airplay.stream_session.get_media_session_id",
            return_value="session-id",
        ),
        patch(
            "music_assistant.providers.airplay.stream_session.FFMpeg", return_value=new_ffmpeg
        ) as ffmpeg_factory,
    ):
        await session._start_player_ffmpeg(player, MagicMock())

    # the old ffmpeg is closed, never killing the shared cli stdin
    old_ffmpeg.close.assert_awaited_once()
    # the fresh ffmpeg writes into the cli process stdin fd (77)
    assert ffmpeg_factory.call_args.kwargs["audio_output"] == 77
    new_ffmpeg.start.assert_awaited_once()
    assert session._player_ffmpeg[player.player_id] is new_ffmpeg


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_state", ["missing", "stopped", "disconnected"])
async def test_standby_requires_every_member_running_and_connected(stream_state: str) -> None:
    """Standby is unavailable when any member lacks a reusable connected session."""
    session = _make_session(0, 0)
    ready_player = MagicMock(player_id="ready", protocol=StreamingProtocol.AIRPLAY2)
    ready_player.stream = MagicMock(running=True, connected=True)
    ready_player.stream.send_cli_command = AsyncMock()
    unavailable_player = MagicMock(player_id="unavailable", protocol=StreamingProtocol.RAOP)
    unavailable_player.stream = None
    if stream_state != "missing":
        unavailable_player.stream = MagicMock(
            running=stream_state != "stopped",
            connected=stream_state != "disconnected",
        )
        unavailable_player.stream.send_cli_command = AsyncMock()
    players: Any = [ready_player, unavailable_player]
    session.sync_clients = players

    assert await session.standby() is False
    assert session.can_replace(players, session.pcm_format) is False
    ready_player.stream.send_cli_command.assert_not_awaited()
    ready_player.set_state_from_stream.assert_not_called()
    if unavailable_player.stream:
        unavailable_player.stream.send_cli_command.assert_not_awaited()
        unavailable_player.set_state_from_stream.assert_not_called()


@pytest.mark.asyncio
async def test_standby_returns_false_when_command_is_not_delivered() -> None:
    """Standby fails without changing state for a member that misses the command."""
    session = _make_session(0, 0)
    logger = MagicMock()
    session.prov.logger = logger
    delivered_player = MagicMock(player_id="delivered", protocol=StreamingProtocol.AIRPLAY2)
    delivered_player.stream = MagicMock(running=True, connected=True)
    delivered_player.stream.send_cli_command = AsyncMock(return_value=True)
    dropped_player = MagicMock(player_id="dropped", protocol=StreamingProtocol.RAOP)
    dropped_player.stream = MagicMock(running=True, connected=True)
    dropped_player.stream.send_cli_command = AsyncMock(return_value=False)
    pending_player = MagicMock(player_id="pending", protocol=StreamingProtocol.AIRPLAY2)
    pending_player.stream = MagicMock(running=True, connected=True)
    pending_player.stream.send_cli_command = AsyncMock(return_value=True)
    session.sync_clients = [delivered_player, dropped_player, pending_player]

    assert await session.standby() is False
    delivered_player.stream.send_cli_command.assert_awaited_once_with("ACTION=STANDBY")
    delivered_player.set_state_from_stream.assert_called_once()
    dropped_player.stream.send_cli_command.assert_awaited_once_with("ACTION=STANDBY")
    dropped_player.set_state_from_stream.assert_not_called()
    pending_player.stream.send_cli_command.assert_not_awaited()
    pending_player.set_state_from_stream.assert_not_called()
    logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_standby_returns_false_when_command_raises() -> None:
    """Standby fails without changing state when command delivery raises."""
    session = _make_session(0, 0)
    logger = MagicMock()
    session.prov.logger = logger
    player = MagicMock(player_id="failed", protocol=StreamingProtocol.AIRPLAY2)
    player.stream = MagicMock(running=True, connected=True)
    player.stream.send_cli_command = AsyncMock(side_effect=OSError("command pipe failed"))
    session.sync_clients = [player]

    assert await session.standby() is False
    player.set_state_from_stream.assert_not_called()
    logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_late_join_empty_buffer() -> None:
    """Test that with an empty buffer, start_at = start_time + seconds_streamed."""
    now = time.time()
    start_time = now - 10
    seconds_streamed = 12.5
    session = _make_session(start_time, seconds_streamed)
    player = _make_late_joiner(wait_start_ms=2000)

    with patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start:
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    assert mock_start.called, "_start_client was never called"
    expected = start_time + seconds_streamed
    assert abs(_captured_start_at(player) - expected) < 0.1


@pytest.mark.asyncio
async def test_late_join_caps_prime_at_whole_ring_when_position_predates_it() -> None:
    """A due position older than the ring caps the prime at the whole buffer."""
    now = 1_000_000.0
    # Synthetic anchor in the future forces the due position behind the oldest
    # buffered sample: only the whole ring can prime and the anchor is pulled
    # to the ring's first sample so content and anchor stay exactly aligned.
    seconds_streamed = 12.5
    buffer_seconds = 3
    start_time = now + 5.0
    session = _make_session(start_time, seconds_streamed)
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * buffer_seconds)
    player = _make_late_joiner(wait_start_ms=2000)

    written_chunks: list[bytes] = []

    async def capture_write(_player: Any, chunk: bytes) -> None:
        written_chunks.append(chunk)

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_write_chunk_to_player", side_effect=capture_write),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    assert mock_start.called, "_start_client was never called"
    # the whole ring is primed, nothing is skipped, and the anchor maps to the
    # ring's first sample: start_at = start_time + (seconds_streamed - buffer)
    assert session._client_skip_bytes[player.player_id] == 0
    assert written_chunks, "expected the whole ring to be primed"
    assert len(written_chunks[0]) / PCM_SAMPLE_SIZE == pytest.approx(buffer_seconds, abs=0.01)
    expected = start_time + (seconds_streamed - buffer_seconds)
    assert _captured_start_at(player) == pytest.approx(expected, abs=0.02)


@pytest.mark.asyncio
async def test_late_join_adds_to_sync_clients() -> None:
    """Test that the late joiner is added to sync_clients."""
    now = time.time()
    session = _make_session(now - 10, 12.5)
    player = _make_late_joiner()

    with patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start:
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    assert player in session.sync_clients


@pytest.mark.asyncio
async def test_late_join_start_failure_stops_client() -> None:
    """A late joiner whose START fails is torn down before joining the session."""
    session = _make_session(time.time() - 10, 12.5)
    player = _make_late_joiner()

    def setup_failing_start(*_args: Any, **_kwargs: Any) -> None:
        _setup_stream(player)()
        player.stream.start = AsyncMock(side_effect=OSError("start failed"))

    with (
        patch.object(session, "_start_client", side_effect=setup_failing_start),
        patch.object(session, "stop_client", new_callable=AsyncMock) as stop_client,
    ):
        await session.add_client(player)

    player.stream.start.assert_awaited_once()
    # START precedes the prime feed and the sync_clients append, so a failed
    # joiner is stopped without ever having joined the session.
    assert player not in session.sync_clients
    stop_client.assert_awaited_once_with(player, reason="late joiner start/prime failed")


@pytest.mark.asyncio
async def test_late_join_no_running_session() -> None:
    """Test that add_client is a no-op when no session is running."""
    now = time.time()
    session = _make_session(now - 10, 12.5)
    # Make the leader's stream not running
    leader = session.sync_clients[0]
    leader.stream = MagicMock()
    leader.stream.running = False
    player = _make_late_joiner()

    with patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start:
        await session.add_client(player)
        mock_start.assert_not_called()
        assert player not in session.sync_clients


@pytest.mark.asyncio
async def test_late_join_primes_from_ring_tail_at_headroom() -> None:
    """A due position inside the ring primes from the tail and anchors at now + headroom."""
    # Freeze time so both the test and the code under test agree on `now`.
    now = 1_000_000.0
    start_time = now - 0.5
    seconds_streamed = 5.0
    session = _make_session(start_time, seconds_streamed)
    # Fill ring buffer with 5 seconds of non-silent PCM.
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 5)
    player = _make_late_joiner(wait_start_ms=2000)

    written_chunks: list[bytes] = []

    async def capture_write(_player: Any, chunk: bytes) -> None:
        written_chunks.append(chunk)

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_write_chunk_to_player", side_effect=capture_write),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    # start_at is now + min_headroom (session.wait_start = 2.0); fed_pos_due = 2.5s
    assert mock_start.called, "_start_client was never called"
    assert _captured_start_at(player) - now == pytest.approx(2.0, abs=0.01), (
        f"start_at should be at now + min_headroom, "
        f"got offset {_captured_start_at(player) - now:.4f}s"
    )

    # The last 2.5s of the ring is primed (positions 2.5s..5.0s), nothing skipped.
    assert session._client_skip_bytes[player.player_id] == 0
    assert written_chunks, "No data was written to the player"
    remaining_seconds = len(written_chunks[0]) / PCM_SAMPLE_SIZE
    assert remaining_seconds == pytest.approx(2.5, abs=0.01), (
        f"expected 2.5s primed, got {remaining_seconds:.4f}s"
    )


@pytest.mark.asyncio
async def test_late_join_skips_live_feed_when_anchor_ahead_of_write_head() -> None:
    """When the due position is ahead of the write head, skip that many live bytes."""
    # Freeze time so both the test and the code under test agree on `now`.
    now = 1_000_000.0
    # Diagnosed clamp case: now - start_time = 8.84s, seconds_streamed = 10.0s,
    # min_headroom = 2.5s and no group shift -> fed_pos_due = 11.34s > 10.0s.
    start_time = now - 8.84
    seconds_streamed = 10.0
    session = _make_session(start_time, seconds_streamed)
    session.wait_start = 2.5
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 10)
    player = _make_late_joiner(wait_start_ms=2000)

    written_chunks: list[bytes] = []

    async def capture_write(_player: Any, chunk: bytes) -> None:
        written_chunks.append(chunk)

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_write_chunk_to_player", side_effect=capture_write),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    # anchor is now + min_headroom and nothing is primed (position past the head)
    assert _captured_start_at(player) - now == pytest.approx(2.5, abs=0.01)
    assert written_chunks == []
    skip_seconds = session._client_skip_bytes[player.player_id] / PCM_SAMPLE_SIZE
    assert skip_seconds == pytest.approx(1.34, abs=0.01)


@pytest.mark.asyncio
async def test_late_join_primes_from_ring_under_group_shift() -> None:
    """A reference member that re-anchored later pulls the due position back into the ring."""
    # Freeze time so both the test and the code under test agree on `now`.
    now = 1_000_000.0
    # Same base as the clamp case, but the reference member accumulated a
    # +1.539s starvation shift (67870 frames @44100) so the group's effective
    # anchor is later: fed_pos_due = 9.80s, back inside the ring. The joiner is
    # primed with ~0.2s from the ring tail and skips nothing.
    start_time = now - 8.84
    seconds_streamed = 10.0
    session = _make_session(start_time, seconds_streamed)
    session.wait_start = 2.5
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 10)
    reference: Any = session.sync_clients[0]
    reference.stream.cumulative_shift_seconds = 67870 / 44100
    player = _make_late_joiner(wait_start_ms=2000)

    written_chunks: list[bytes] = []

    async def capture_write(_player: Any, chunk: bytes) -> None:
        written_chunks.append(chunk)

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_write_chunk_to_player", side_effect=capture_write),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    assert _captured_start_at(player) - now == pytest.approx(2.5, abs=0.01)
    assert session._client_skip_bytes[player.player_id] == 0
    assert written_chunks, "expected a prime write from the ring tail"
    primed_seconds = len(written_chunks[0]) / PCM_SAMPLE_SIZE
    assert primed_seconds == pytest.approx(0.199, abs=0.01)


@pytest.mark.asyncio
async def test_write_chunk_drains_skip_counter_across_chunks() -> None:
    """A per-client skip is consumed across chunks, slicing the partial one."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    ffmpeg = MagicMock(closed=False)
    ffmpeg.write = AsyncMock()
    session._player_ffmpeg[player.player_id] = ffmpeg
    # skip 1.5s of a 1s-per-chunk feed
    session._client_skip_bytes[player.player_id] = PCM_SAMPLE_SIZE * 3 // 2

    chunk_one = b"\x01" * PCM_SAMPLE_SIZE
    chunk_two = b"\x02" * PCM_SAMPLE_SIZE
    chunk_three = b"\x03" * PCM_SAMPLE_SIZE
    for chunk in (chunk_one, chunk_two, chunk_three):
        await session._write_chunk_to_player(player, chunk)

    written = [call_args.args[0] for call_args in ffmpeg.write.await_args_list]
    # first chunk fully consumed, second chunk sliced in half, third whole
    assert written == [b"\x02" * (PCM_SAMPLE_SIZE // 2), chunk_three]
    assert session._client_skip_bytes[player.player_id] == 0


def test_effective_start_time_adds_reference_member_shift() -> None:
    """The effective anchor adds the first sync client's accumulated shift."""
    session = _make_session(100.0, 0)
    reference: Any = session.sync_clients[0]
    reference.stream.cumulative_shift_seconds = 1.539
    assert session.effective_start_time == pytest.approx(101.539)

    # the reference transfers to whichever client is first
    other = MagicMock()
    other.stream.cumulative_shift_seconds = 0.5
    session.sync_clients.insert(0, other)
    assert session.effective_start_time == pytest.approx(100.5)

    # a missing stream falls back to the raw anchor
    other.stream = None
    assert session.effective_start_time == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_stop_client_clears_skip_and_shift_state() -> None:
    """Tearing a client down drops its skip counter and resets its playout shift."""
    session = _make_session(0, 0)
    player = _make_late_joiner()
    player.stream = MagicMock()
    player.stream.session = session
    player.stream.stop = AsyncMock()
    session._client_skip_bytes[player.player_id] = 12_345

    await session.stop_client(player)

    assert player.player_id not in session._client_skip_bytes
    player.stream.reset_reanchor_shift.assert_called_once_with()
    player.stream.stop.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_replace_clears_skip_and_shift_state() -> None:
    """A warm replace re-anchors everyone, so skip counters and shifts reset."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.config.get_value = MagicMock(return_value=0)
    stream = player.stream
    stream.running = True
    stream.connected = True
    stream.flush = AsyncMock(return_value=True)
    stream.wait_audio_present = AsyncMock(return_value=True)
    stream.start = AsyncMock()
    session._client_skip_bytes[player.player_id] = 999

    with (
        patch.object(session, "_start_player_ffmpeg", new_callable=AsyncMock),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=100.0),
    ):
        assert await session.replace(MagicMock(), MagicMock(elapsed_time=0))

    assert session._client_skip_bytes == {}
    stream.reset_reanchor_shift.assert_called()


@pytest.mark.asyncio
async def test_cleanup_after_removal_skips_idle_when_player_has_new_session_stream() -> None:
    """Cleanup must not idle a player that was already re-added to another session."""
    now = time.time()
    session = _make_session(now - 10, 12.5)
    player = _make_late_joiner()
    other_session = object()
    player.set_state_from_stream = MagicMock()
    player.stream = MagicMock()
    player.stream.session = other_session
    session.sync_clients.clear()

    with (
        patch.object(session, "stop_client", new_callable=AsyncMock),
        patch.object(session, "stop", new_callable=AsyncMock),
    ):
        await session._cleanup_after_removal(player)

    player.set_state_from_stream.assert_not_called()
