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
        player.stream.wait_for_connection = AsyncMock()
        player.stream.prepare_generation = AsyncMock()
        player.stream.wait_generation_primed = AsyncMock(return_value=True)
        player.stream.start_generation = AsyncMock()

    return _side_effect


def _captured_start_at(player: MagicMock) -> float:
    """Return the start time passed to the player's generation-0 START."""
    start_unix_ms = player.stream.start_generation.call_args[0][2]
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
    """Generation 0 starts at one instant only after every member connects and primes."""
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

        async def prepare_generation(generation: int, audio_path: str, position_ms: int) -> None:
            assert (generation, audio_path, position_ms) == (0, "-", 12_000)
            operations.append(f"prepared:{player.player_id}")

        async def wait_generation_primed(_generation: int) -> bool:
            operations.append(f"primed:{player.player_id}")
            return True

        async def start_generation(_generation: int, _position_ms: int, start_unix_ms: int) -> None:
            operations.append(f"started:{player.player_id}:{start_unix_ms}")

        stream.wait_for_connection = AsyncMock(side_effect=wait_for_connection)
        stream.prepare_generation = AsyncMock(side_effect=prepare_generation)
        stream.wait_generation_primed = AsyncMock(side_effect=wait_generation_primed)
        stream.start_generation = AsyncMock(side_effect=start_generation)
        player.stream = stream

    with (
        patch.object(session, "_start_client", side_effect=start_client),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch.object(session, "_resolve_shared_ptp", new_callable=AsyncMock, return_value=False),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=100.0),
    ):
        await session.start(MagicMock())

    first_prepare = min(i for i, op in enumerate(operations) if op.startswith("prepared:"))
    last_connected = max(i for i, op in enumerate(operations) if op.startswith("connected:"))
    first_start = min(i for i, op in enumerate(operations) if op.startswith("started:"))
    last_primed = max(i for i, op in enumerate(operations) if op.startswith("primed:"))
    assert last_connected < first_prepare
    assert last_primed < first_start
    starts = {int(op.rsplit(":", 1)[1]) for op in operations if op.startswith("started:")}
    assert starts == {100_750}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol",
    [StreamingProtocol.RAOP, StreamingProtocol.AIRPLAY2],
)
async def test_initial_single_player_uses_commanded_generation_zero(
    protocol: StreamingProtocol,
) -> None:
    """A standalone player follows the same generation-0 flow with the solo lead."""
    session = _make_session(0, 0)
    player: Any = session.sync_clients[0]
    player.player_id = "solo"
    player.protocol = protocol
    player.wait_start = 2500
    player.config.get_value = MagicMock(return_value=0)
    stream = MagicMock(running=True)
    stream.wait_for_connection = AsyncMock()
    stream.prepare_generation = AsyncMock()
    stream.wait_generation_primed = AsyncMock(return_value=True)
    stream.start_generation = AsyncMock()

    async def start_client(_player: MagicMock, _use_shared_ptp: bool) -> None:
        player.stream = stream

    with (
        patch.object(session, "_start_client", side_effect=start_client),
        patch.object(session, "_audio_streamer", new_callable=AsyncMock),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=200.0),
    ):
        await session.start(MagicMock())

    stream.prepare_generation.assert_awaited_once_with(0, "-", 0)
    stream.wait_generation_primed.assert_awaited_once_with(0)
    stream.start_generation.assert_awaited_once_with(0, 0, 200_400)


@pytest.mark.asyncio
async def test_initial_prime_timeout_never_starts_partial_group() -> None:
    """If any member fails to prime, no member receives START and the group is stopped."""
    session = _make_session(0, 0)
    first_player: Any = session.sync_clients[0]
    first_player.protocol = StreamingProtocol.RAOP
    first_player.wait_start = 2500
    second_player = MagicMock(player_id="second", wait_start=2500)
    second_player.protocol = StreamingProtocol.RAOP
    session.sync_clients.append(second_player)

    async def start_client(player: MagicMock, _use_shared_ptp: bool) -> None:
        stream = MagicMock(running=True)
        stream.wait_for_connection = AsyncMock()
        stream.prepare_generation = AsyncMock()
        stream.wait_generation_primed = AsyncMock(return_value=player is not second_player)
        stream.start_generation = AsyncMock()
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
        stream.start_generation.assert_not_awaited()
    stop_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_initial_connection_cancellation_never_prepares_group() -> None:
    """Cancellation while connecting cleans up without preparing or starting."""
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
    stream.prepare_generation = AsyncMock()
    stream.start_generation = AsyncMock()

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

    stream.prepare_generation.assert_not_awaited()
    stream.start_generation.assert_not_awaited()
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
    ("protocols", "standby_supported"),
    [
        ((StreamingProtocol.AIRPLAY2,), True),
        ((StreamingProtocol.RAOP,), False),
        ((StreamingProtocol.RAOP, StreamingProtocol.AIRPLAY2), False),
    ],
)
async def test_standby_requires_every_member_to_support_airplay2(
    protocols: tuple[StreamingProtocol, ...],
    standby_supported: bool,
) -> None:
    """Generation support does not imply legacy RAOP standby support."""
    session = _make_session(0, 0)
    players: Any = []
    for index, protocol in enumerate(protocols):
        player = MagicMock()
        player.player_id = f"player-{index}"
        player.protocol = protocol
        player.stream = MagicMock(running=True)
        player.stream.send_cli_command = AsyncMock()
        players.append(player)
    session.sync_clients = players

    assert await session.standby() is standby_supported
    for player in players:
        if standby_supported:
            player.stream.send_cli_command.assert_awaited_once_with("ACTION=STANDBY")
            player.set_state_from_stream.assert_called_once()
        else:
            player.stream.send_cli_command.assert_not_awaited()
            player.set_state_from_stream.assert_not_called()


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
async def test_late_join_with_buffered_pcm_in_future() -> None:
    """Late join with start_at already in the future leaves start_at unmodified."""
    now = time.time()
    # Place start_at well in the future: start_time = now + 5, buffer = 0 →
    # start_at = now + 5 + (seconds_streamed - 0) = now + 17.5 (>> min_headroom)
    start_time = now + 5
    seconds_streamed = 12.5
    session = _make_session(start_time, seconds_streamed)
    # Fill the ring buffer with 3 seconds of PCM
    session._pcm_buffer = bytearray(b"\x00" * PCM_SAMPLE_SIZE * 3)
    player = _make_late_joiner(wait_start_ms=2000)

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_write_chunk_to_player", new_callable=AsyncMock),
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    assert mock_start.called, "_start_client was never called"
    # start_at should remain at start_time + (seconds_streamed - 3.0) — already in future
    expected = start_time + (seconds_streamed - 3.0)
    captured = _captured_start_at(player)
    assert abs(captured - expected) < 0.1, (
        f"start_at should match buffer position, expected {expected}, got {captured}"
    )


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
async def test_late_join_trims_and_shifts_when_start_at_in_past() -> None:
    """When start_at would be in the past, trim from buffer head and shift start_at forward."""
    # Freeze time so both the test and the code under test agree on `now`.
    now = 1_000_000.0
    start_time = now - 0.5
    seconds_streamed = 5.0
    session = _make_session(start_time, seconds_streamed)
    # Fill ring buffer with 5 seconds of non-silent PCM (so trim has something to take)
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

    # start_at should be pushed to now + min_headroom (session.wait_start = 2.0)
    assert mock_start.called, "_start_client was never called"
    assert _captured_start_at(player) - now == pytest.approx(2.0, abs=0.01), (
        f"start_at should be at now + min_headroom, "
        f"got offset {_captured_start_at(player) - now:.4f}s"
    )

    # Buffer should have been trimmed: 2.5s out of 5s (start_at shifted from -0.5 to +2.0)
    assert written_chunks, "No data was written to the player"
    written = written_chunks[0]
    remaining_seconds = len(written) / PCM_SAMPLE_SIZE
    assert remaining_seconds == pytest.approx(2.5, abs=0.01), (
        f"expected 2.5s remaining, got {remaining_seconds:.4f}s"
    )


@pytest.mark.asyncio
async def test_late_join_drops_buffer_when_trim_exceeds_buffer() -> None:
    """When the required trim exceeds the buffer, the buffer is dropped entirely."""
    # Freeze time so both the test and the code under test agree on `now`.
    now = 1_000_000.0
    # start_at = now - 4.0 (way in the past). With 3s buffer the required trim
    # is 6s but buffer only holds 3s → buffer fully consumed.
    start_time = now - 4.0
    seconds_streamed = 3.0
    session = _make_session(start_time, seconds_streamed)
    session._pcm_buffer = bytearray(b"\x01" * PCM_SAMPLE_SIZE * 3)
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

    # No bytes should be written (buffer fully trimmed)
    assert written_chunks == [], "Buffer should have been dropped entirely"
    # start_at should be exactly now + min_headroom (since the next-chunk anchor would
    # have landed at now - 1.0, which is below the target).
    assert _captured_start_at(player) - now == pytest.approx(2.0, abs=0.01)


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
