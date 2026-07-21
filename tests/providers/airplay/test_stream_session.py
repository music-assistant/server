"""Unit tests for AirPlay stream session late-join logic."""

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.airplay.player import AirPlayPlayer
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
    prov.bridge_manager.stop_streaming = AsyncMock(return_value=False)

    pcm_format = MagicMock()
    pcm_format.pcm_sample_size = PCM_SAMPLE_SIZE
    pcm_format.sample_rate = 44100
    pcm_format.bit_depth = 16
    pcm_format.channels = 2

    leader = MagicMock()
    leader.player_id = "leader"
    leader.wait_start = 2000
    leader.stream = MagicMock()
    leader.stream.running = True

    session = AirPlayStreamSession(prov, [leader], pcm_format, MagicMock())
    session._audio_source_task = cast(
        "Any",
        asyncio.get_running_loop().create_future(),
    )
    leader.stream.session = session
    session._player_streams[leader.player_id] = leader.stream
    session.start_time = start_time
    session.seconds_streamed = seconds_streamed
    session.start_unix_ms = 1  # dummy
    session.wait_start = 2.0

    return session


def _make_late_joiner(wait_start_ms: int = 2000) -> MagicMock:
    """Create a mock AirPlay player for late-join testing."""
    player = MagicMock()
    player.player_id = "late_joiner"
    player.wait_start = wait_start_ms
    player.stream = None
    player.config = MagicMock()
    player.config.get_value = MagicMock(return_value=0)

    @asynccontextmanager
    async def stream_replacement(
        stream: Any,
        is_current: Callable[[], bool] | None = None,
        expected_generation: int | None = None,
        before_replace: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncGenerator[None]:
        del expected_generation
        if is_current is not None and not is_current():
            raise PlayerCommandFailed("superseded")
        if before_replace is not None:
            await before_replace()
        player.stream = stream
        yield

    player.stream_replacement = stream_replacement
    player.device_info.mac_address = "AA:BB:CC:DD:EE:FF"
    player.is_stream_generation_current = MagicMock(return_value=True)
    return player


def _captured_start_at(mock_start: AsyncMock) -> float:
    """Return the start time (unix seconds) passed to the patched _start_client."""
    start_unix_ms = mock_start.call_args[0][1]
    assert isinstance(start_unix_ms, int)
    return start_unix_ms / 1000


@pytest.mark.asyncio
async def test_late_join_empty_buffer() -> None:
    """Test that with an empty buffer, start_at = start_time + seconds_streamed."""
    now = time.time()
    start_time = now - 10
    seconds_streamed = 12.5
    session = _make_session(start_time, seconds_streamed)
    player = _make_late_joiner(wait_start_ms=2000)

    with patch.object(session, "_start_client_processes", new_callable=AsyncMock) as mock_start:
        await session.add_client(player)

    assert mock_start.called, "_start_client was never called"
    expected = start_time + seconds_streamed
    assert abs(_captured_start_at(mock_start) - expected) < 0.1


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
        patch.object(session, "_start_client_processes", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_write_chunk_to_player", new_callable=AsyncMock),
    ):
        await session.add_client(player)

    assert mock_start.called, "_start_client was never called"
    # start_at should remain at start_time + (seconds_streamed - 3.0) — already in future
    expected = start_time + (seconds_streamed - 3.0)
    captured = _captured_start_at(mock_start)
    assert abs(captured - expected) < 0.1, (
        f"start_at should match buffer position, expected {expected}, got {captured}"
    )


@pytest.mark.asyncio
async def test_late_join_adds_to_sync_clients() -> None:
    """Test that the late joiner is added to sync_clients."""
    now = time.time()
    session = _make_session(now - 10, 12.5)
    player = _make_late_joiner()

    with patch.object(session, "_start_client_processes", new_callable=AsyncMock):
        await session.add_client(player)

    assert player in session.sync_clients


@pytest.mark.asyncio
async def test_late_join_no_running_session() -> None:
    """Test that add_client is a no-op when no session is running."""
    now = time.time()
    session = _make_session(now - 10, 12.5)
    # Make the leader's stream not running
    leader = session.sync_clients[0]
    stopped_stream = MagicMock()
    stopped_stream.running = False
    stopped_stream.session = session
    session._player_streams[leader.player_id] = stopped_stream
    leader.stream = stopped_stream
    player = _make_late_joiner()

    with patch.object(session, "_start_client_processes", new_callable=AsyncMock) as mock_start:
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
        patch.object(session, "_start_client_processes", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_write_chunk_to_player", side_effect=capture_write),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        await session.add_client(player)

    # start_at should be pushed to now + min_headroom (session.wait_start = 2.0)
    assert mock_start.called, "_start_client was never called"
    assert _captured_start_at(mock_start) - now == pytest.approx(2.0, abs=0.01), (
        f"start_at should be at now + min_headroom, "
        f"got offset {_captured_start_at(mock_start) - now:.4f}s"
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
        patch.object(session, "_start_client_processes", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_write_chunk_to_player", side_effect=capture_write),
        patch("music_assistant.providers.airplay.stream_session.time.time", return_value=now),
    ):
        await session.add_client(player)

    # No bytes should be written (buffer fully trimmed)
    assert written_chunks == [], "Buffer should have been dropped entirely"
    # start_at should be exactly now + min_headroom (since the next-chunk anchor would
    # have landed at now - 1.0, which is below the target).
    assert _captured_start_at(mock_start) - now == pytest.approx(2.0, abs=0.01)


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


@pytest.mark.asyncio
async def test_connection_failure_from_removed_client_is_ignored() -> None:
    """A removed connecting member cannot fail startup for surviving clients."""
    session = _make_session(time.time(), 0)
    player = session.sync_clients.pop()
    stream = MagicMock()
    stream.wait_for_connection = AsyncMock(
        side_effect=PlayerCommandFailed("removed while connecting")
    )
    session._player_streams[player.player_id] = stream

    await session._wait_for_client_connection(player, stream)

    stream.wait_for_connection.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_stop_client_uses_session_owned_stream_after_replacement() -> None:
    """Session teardown stops its own stream without touching a replacement."""
    session = _make_session(time.time(), 0)
    player = session.sync_clients[0]
    owned_stream = session._player_streams[player.player_id]
    replacement_stream = MagicMock()
    replacement_stream.session = object()
    replacement_stream.stop = AsyncMock()
    player.stream = replacement_stream

    with patch.object(owned_stream, "stop", new_callable=AsyncMock) as owned_stop:
        await session.stop_client(player, reason="superseded session")

    owned_stop.assert_awaited_once_with(force=True)
    replacement_stream.stop.assert_not_awaited()
    assert player.stream is replacement_stream


@pytest.mark.asyncio
async def test_session_start_cancellation_stops_partially_started_client() -> None:
    """Cancelling session startup tears down every stream it already created."""
    session = _make_session(time.time(), 0)
    session._audio_source_task = None
    player = session.sync_clients[0]
    session._player_streams.clear()
    player.stream = None
    client_started = asyncio.Event()
    owned_stream = MagicMock()
    owned_stream.session = session
    owned_stream.stop = AsyncMock()

    async def stalled_start(*_args: Any, **_kwargs: Any) -> None:
        session._player_streams[player.player_id] = owned_stream
        player.stream = owned_stream
        client_started.set()
        await asyncio.Event().wait()

    async def audio_source() -> AsyncGenerator[bytes]:
        yield b"audio"

    with (
        patch.object(session, "_resolve_shared_ptp", new=AsyncMock(return_value=False)),
        patch.object(session, "_start_client", side_effect=stalled_start),
    ):
        start_task = asyncio.create_task(session.start(audio_source()))
        await client_started.wait()
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task

    owned_stream.stop.assert_awaited_once_with(force=True)
    assert session._player_streams == {}
    assert player.stream is None


@pytest.mark.asyncio
async def test_superseded_ffmpeg_start_kills_local_process_handle() -> None:
    """A superseded start kills ffmpeg even after another session removed its mapping."""
    session = _make_session(time.time(), 0)
    player = cast("Any", session.sync_clients[0])
    session._player_streams.clear()
    player.stream = None
    player.config.get_value.return_value = 0
    player.get_stream_pcm_format.return_value = session.pcm_format
    ffmpeg_spawned = asyncio.Event()
    release_ffmpeg_start = asyncio.Event()

    async def start_ffmpeg() -> None:
        ffmpeg_spawned.set()
        await release_ffmpeg_start.wait()

    stream = MagicMock()
    stream.pcm_format = session.pcm_format
    stream.start = AsyncMock()
    stdin_pipe = MagicMock()
    stdin_pipe.fileno.return_value = 1
    stream._cli_proc.proc.stdin.transport.get_extra_info.return_value = stdin_pipe
    ffmpeg = MagicMock()
    ffmpeg.start = AsyncMock(side_effect=start_ffmpeg)
    ffmpeg.kill = AsyncMock()
    session._player_streams[player.player_id] = stream
    player.stream = stream
    cast("Any", session.mass).streams.audio.get_player_output_plan.return_value.filter_params = []

    with (
        patch(
            "music_assistant.providers.airplay.stream_session.FFMpeg",
            return_value=ffmpeg,
        ),
        patch(
            "music_assistant.providers.airplay.stream_session.get_final_output_format",
            return_value=session.pcm_format,
        ),
        patch(
            "music_assistant.providers.airplay.stream_session.get_media_session_id",
            return_value="session",
        ),
    ):
        start_task = asyncio.create_task(session._start_client_processes(player, 1, False, stream))
        await ffmpeg_spawned.wait()
        session._player_ffmpeg.pop(player.player_id)
        session._player_streams.pop(player.player_id)
        replacement_stream = MagicMock()
        replacement_stream.session = object()
        player.stream = replacement_stream
        release_ffmpeg_start.set()
        with pytest.raises(PlayerCommandFailed):
            await start_task

    ffmpeg.kill.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_teardown_waits_for_inflight_ffmpeg_start() -> None:
    """Client teardown waits until ffmpeg has a process handle it can kill."""
    session = _make_session(time.time(), 0)
    player = cast("Any", session.sync_clients[0])
    session._player_streams.clear()
    player.config.get_value.return_value = 0
    ffmpeg_spawned = asyncio.Event()
    release_ffmpeg_start = asyncio.Event()

    async def start_ffmpeg() -> None:
        ffmpeg_spawned.set()
        await release_ffmpeg_start.wait()

    stream = MagicMock()
    stream.pcm_format = session.pcm_format
    stream.start = AsyncMock()
    stream.stop = AsyncMock()
    stdin_pipe = MagicMock()
    stdin_pipe.fileno.return_value = 1
    stream._cli_proc.proc.stdin.transport.get_extra_info.return_value = stdin_pipe
    ffmpeg = MagicMock()
    ffmpeg.start = AsyncMock(side_effect=start_ffmpeg)
    ffmpeg.kill = AsyncMock()
    session._player_streams[player.player_id] = stream
    player.stream = stream
    cast("Any", session.mass).streams.audio.get_player_output_plan.return_value.filter_params = []

    with (
        patch(
            "music_assistant.providers.airplay.stream_session.FFMpeg",
            return_value=ffmpeg,
        ),
        patch(
            "music_assistant.providers.airplay.stream_session.get_final_output_format",
            return_value=session.pcm_format,
        ),
        patch(
            "music_assistant.providers.airplay.stream_session.get_media_session_id",
            return_value="session",
        ),
    ):
        start_task = asyncio.create_task(session._start_client_processes(player, 1, False, stream))
        await ffmpeg_spawned.wait()
        stop_task = asyncio.create_task(session.stop_client(player, reason="regroup"))
        await asyncio.sleep(0)
        ffmpeg.kill.assert_not_awaited()
        release_ffmpeg_start.set()
        await start_task
        await stop_task

    ffmpeg.kill.assert_awaited_once_with()
    assert player.player_id not in session._player_ffmpeg
    assert player.player_id not in session._player_streams


@pytest.mark.asyncio
async def test_cancelled_eof_kills_tracked_ffmpeg() -> None:
    """Cancellation during EOF keeps the ffmpeg handle long enough to kill it."""
    session = _make_session(time.time(), 0)
    player = session.sync_clients[0]
    eof_started = asyncio.Event()

    async def write_eof() -> None:
        eof_started.set()
        await asyncio.Event().wait()

    ffmpeg = MagicMock()
    ffmpeg.write_eof = AsyncMock(side_effect=write_eof)
    ffmpeg.kill = AsyncMock()
    stream = cast("Any", session._player_streams[player.player_id])
    stream.stop = AsyncMock()
    session._player_ffmpeg[player.player_id] = ffmpeg

    eof_task = asyncio.create_task(session._write_eof_to_player(player))
    await eof_started.wait()
    eof_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await eof_task

    ffmpeg.kill.assert_awaited_once_with()
    stream.stop.assert_awaited_once_with(force=True)
    assert player.player_id not in session._player_ffmpeg


@pytest.mark.asyncio
async def test_late_join_does_not_hold_session_lock_while_waiting_for_player() -> None:
    """A replacement holding the player lock can still finish session removal."""
    session = _make_session(time.time(), 0)
    player = MagicMock()
    player.player_id = "late_joiner"
    player._stream_lock = asyncio.Lock()
    player._stream_generation = 0
    player.stream_generation = 0
    player._stream_stopping = False
    player._unloading = False
    player.reserve_stream_generation = AirPlayPlayer.reserve_stream_generation.__get__(
        player, AirPlayPlayer
    )
    player.stream_replacement = AirPlayPlayer.stream_replacement.__get__(player, AirPlayPlayer)
    player.start_stream = AirPlayPlayer.start_stream.__get__(player, AirPlayPlayer)
    remove_started = asyncio.Event()
    allow_remove = asyncio.Event()

    previous_stream = MagicMock()
    previous_stream.session = session
    previous_stream.stop = AsyncMock()
    player.stream = previous_stream

    async def remove_previous(*_args: Any, **_kwargs: Any) -> None:
        remove_started.set()
        await allow_remove.wait()
        async with session._lock:
            pass

    replacement_stream = MagicMock()
    replacement_stream.session = None
    replacement_stream.start = AsyncMock()
    replacement_stream.stop = AsyncMock()
    late_join_stream = MagicMock()
    late_join_stream.session = session
    late_join_stream.stop = AsyncMock()

    with (
        patch.object(session, "remove_client", side_effect=remove_previous),
        patch.object(
            session,
            "_prepare_client_stream",
            new=MagicMock(return_value=late_join_stream),
        ),
        patch.object(
            session,
            "_start_late_joiner",
            new=AsyncMock(return_value=time.time()),
        ),
    ):
        replacement_task = asyncio.create_task(player.start_stream(replacement_stream, 1))
        await remove_started.wait()
        add_task = asyncio.create_task(session.add_client(player))
        await asyncio.sleep(0)
        allow_remove.set()
        results = await asyncio.wait_for(
            asyncio.gather(replacement_task, add_task, return_exceptions=True),
            timeout=1,
        )

    assert isinstance(results[0], PlayerCommandFailed)
    assert player.stream is late_join_stream


@pytest.mark.asyncio
async def test_queued_client_start_cannot_run_after_session_stop() -> None:
    """A start waiting for the player lock is invalidated by session teardown."""
    session = _make_session(time.time(), 0)
    player = cast("Any", session.sync_clients[0])
    session._player_streams.clear()
    player.stream = None
    player._stream_lock = asyncio.Lock()
    player._stream_generation = 0
    player.stream_generation = 0
    player._stream_stopping = False
    player._unloading = False
    player.reserve_stream_generation = AirPlayPlayer.reserve_stream_generation.__get__(
        player, AirPlayPlayer
    )
    player.stream_replacement = AirPlayPlayer.stream_replacement.__get__(player, AirPlayPlayer)
    stream = MagicMock()
    stream.session = session
    stream.start = AsyncMock()
    stream.stop = AsyncMock()

    await player._stream_lock.acquire()
    try:
        with patch.object(
            session,
            "_prepare_client_stream",
            new=MagicMock(return_value=stream),
        ):
            start_task = asyncio.create_task(session._start_client(player, 1, False))
            await asyncio.sleep(0)
            await session.stop()
            player._stream_lock.release()
            with pytest.raises(PlayerCommandFailed):
                await start_task
    finally:
        if player._stream_lock.locked():
            player._stream_lock.release()

    stream.start.assert_not_awaited()
    assert session._player_streams == {}


@pytest.mark.asyncio
async def test_session_start_reserves_generation_before_ptp_wait() -> None:
    """PTP readiness cannot make a pre-stop session adopt a newer generation."""
    session = _make_session(time.time(), 0)
    session._audio_source_task = None
    player = cast("Any", session.sync_clients[0])
    player._stream_generation = 0
    player.reserve_stream_generation = AirPlayPlayer.reserve_stream_generation.__get__(
        player, AirPlayPlayer
    )
    session._player_streams.clear()
    player.stream = None
    ptp_waiting = asyncio.Event()
    release_ptp = asyncio.Event()

    async def resolve_ptp() -> bool:
        ptp_waiting.set()
        await release_ptp.wait()
        return False

    async def audio_source() -> AsyncGenerator[bytes]:
        yield b"audio"

    with (
        patch.object(session, "_resolve_shared_ptp", side_effect=resolve_ptp),
        patch.object(
            session,
            "_start_client",
            new=AsyncMock(side_effect=PlayerCommandFailed("superseded")),
        ) as start_client,
    ):
        start_task = asyncio.create_task(session.start(audio_source()))
        await ptp_waiting.wait()
        assert player._stream_generation == 1
        player._stream_generation += 1
        release_ptp.set()
        with pytest.raises(PlayerCommandFailed):
            await start_task

    assert start_client.call_args.args[3] == 1


@pytest.mark.asyncio
async def test_stale_session_start_does_not_cleanup_newer_bridge() -> None:
    """Generation validation runs before destructive bridge preparation."""
    session = _make_session(time.time(), 0)
    player = cast("Any", session.sync_clients[0])
    player._stream_lock = asyncio.Lock()
    player._stream_generation = 2
    player._stream_stopping = False
    player._unloading = False
    player.stream_replacement = AirPlayPlayer.stream_replacement.__get__(player, AirPlayPlayer)
    bridge_stream = MagicMock()
    bridge_stream.session = None
    bridge_stream.stop = AsyncMock()
    player.stream = bridge_stream
    prepared_stream = MagicMock()
    prepared_stream.session = session
    prepared_stream.stop = AsyncMock()
    bridge_stop = cast("Any", session.prov.bridge_manager.stop_streaming)

    with (
        patch.object(
            session,
            "_prepare_client_stream",
            return_value=prepared_stream,
        ),
        pytest.raises(PlayerCommandFailed),
    ):
        await session._start_client(
            player,
            1,
            False,
            expected_generation=1,
        )

    bridge_stop.assert_not_awaited()
    bridge_stream.stop.assert_not_awaited()
    assert player.stream is bridge_stream


@pytest.mark.asyncio
async def test_session_start_cannot_publish_audio_after_stop() -> None:
    """A session stopped during client startup cannot publish its audio task later."""
    session = _make_session(time.time(), 0)
    session._audio_source_task = None
    player = session.sync_clients[0]
    session._player_streams.clear()
    player.stream = None
    client_starting = asyncio.Event()
    release_client_start = asyncio.Event()

    async def delayed_start(*_args: Any, **_kwargs: Any) -> None:
        client_starting.set()
        await release_client_start.wait()

    async def audio_source() -> AsyncGenerator[bytes]:
        yield b"audio"

    with (
        patch.object(session, "_resolve_shared_ptp", new=AsyncMock(return_value=False)),
        patch.object(session, "_start_client", side_effect=delayed_start),
    ):
        start_task = asyncio.create_task(session.start(audio_source()))
        await client_starting.wait()
        await session.stop()
        release_client_start.set()
        with pytest.raises(PlayerCommandFailed):
            await start_task

    assert session._audio_source_task is None


@pytest.mark.asyncio
async def test_removal_invalidates_queued_late_join() -> None:
    """Removing a pending late join prevents that generation from starting."""
    session = _make_session(time.time(), 0)
    player = MagicMock()
    player.player_id = "late_joiner"
    player._stream_lock = asyncio.Lock()
    player._stream_generation = 0
    player.stream_generation = 0
    player._stream_stopping = False
    player._unloading = False
    player.reserve_stream_generation = AirPlayPlayer.reserve_stream_generation.__get__(
        player, AirPlayPlayer
    )
    player.stream_replacement = AirPlayPlayer.stream_replacement.__get__(player, AirPlayPlayer)
    player.stream = None
    stream_prepared = asyncio.Event()
    late_join_stream = MagicMock()
    late_join_stream.session = session
    late_join_stream.stop = AsyncMock()

    def prepare_stream(*_args: Any, **_kwargs: Any) -> Any:
        stream_prepared.set()
        return late_join_stream

    await player._stream_lock.acquire()
    try:
        with (
            patch.object(session, "_prepare_client_stream", side_effect=prepare_stream),
            patch.object(
                session,
                "_start_client_processes",
                new_callable=AsyncMock,
            ) as start_processes,
        ):
            add_task = asyncio.create_task(session.add_client(player))
            await stream_prepared.wait()
            await session.remove_client(player, reason="pending join removed")
            replacement_session = MagicMock()
            replacement_session.remove_client = AsyncMock()
            replacement_stream = MagicMock()
            replacement_stream.session = replacement_session
            replacement_stream.stop = AsyncMock()
            player.stream = replacement_stream
            player._stream_lock.release()
            await add_task
    finally:
        if player._stream_lock.locked():
            player._stream_lock.release()

    start_processes.assert_not_awaited()
    replacement_session.remove_client.assert_not_awaited()
    replacement_stream.stop.assert_not_awaited()
    assert player.stream is replacement_stream
    assert player not in session.sync_clients
    assert player.player_id not in session._starting_clients


@pytest.mark.asyncio
async def test_pending_removal_does_not_stop_later_stream_generation() -> None:
    """A removal that expected no stream cannot tear down a later publication."""
    session = _make_session(time.time(), 0)
    player = _make_late_joiner()
    reservation = object()
    session._starting_clients[player.player_id] = reservation
    stop_lock = session._client_stop_locks.setdefault(player.player_id, asyncio.Lock())
    removal_started = asyncio.Event()
    original_cleanup = session._cleanup_after_removal

    async def cleanup_after_removal(*args: Any, **kwargs: Any) -> None:
        removal_started.set()
        await original_cleanup(*args, **kwargs)

    await stop_lock.acquire()
    try:
        with patch.object(
            session,
            "_cleanup_after_removal",
            side_effect=cleanup_after_removal,
        ):
            remove_task = asyncio.create_task(
                session.remove_client(
                    player,
                    reason="pending join removed",
                    reservation=reservation,
                )
            )
            await removal_started.wait()
            replacement_stream = MagicMock()
            replacement_stream.session = session
            replacement_stream.stop = AsyncMock()
            session._player_streams[player.player_id] = replacement_stream
            player.stream = replacement_stream
    finally:
        stop_lock.release()

    await remove_task

    replacement_stream.stop.assert_not_awaited()
    assert session._player_streams[player.player_id] is replacement_stream
    assert player.stream is replacement_stream


@pytest.mark.asyncio
async def test_cancelled_late_join_priming_stops_ffmpeg() -> None:
    """Cancelling buffered priming tears down the unpublished client pipeline."""
    session = _make_session(time.time() + 5, 12.5)
    session._pcm_buffer = bytearray(b"\x00" * PCM_SAMPLE_SIZE)
    player = _make_late_joiner()
    write_started = asyncio.Event()

    ffmpeg = MagicMock()
    ffmpeg.kill = AsyncMock()

    async def start_processes(*_args: Any, **_kwargs: Any) -> None:
        session._player_ffmpeg[player.player_id] = ffmpeg

    async def write_buffer(*_args: Any, **_kwargs: Any) -> None:
        write_started.set()
        await asyncio.Event().wait()

    with (
        patch.object(session, "_start_client_processes", side_effect=start_processes),
        patch.object(session, "_write_chunk_to_player", side_effect=write_buffer),
    ):
        add_task = asyncio.create_task(session.add_client(player))
        await write_started.wait()
        add_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await add_task

    ffmpeg.kill.assert_awaited_once_with()
    assert player.player_id not in session._player_ffmpeg
    assert player.player_id not in session._player_streams
    assert player not in session.sync_clients


@pytest.mark.asyncio
async def test_cancelled_late_join_connection_stops_exact_pipeline() -> None:
    """Cancelling connection wait removes the published late-join generation."""
    session = _make_session(time.time(), 0)
    player = _make_late_joiner()
    connection_waiting = asyncio.Event()

    async def wait_for_connection() -> None:
        connection_waiting.set()
        await asyncio.Event().wait()

    stream = MagicMock()
    stream.session = session
    stream.stop = AsyncMock()
    stream.wait_for_connection = AsyncMock(side_effect=wait_for_connection)
    ffmpeg = MagicMock()
    ffmpeg.kill = AsyncMock()

    async def start_late_joiner(
        _player: Any,
        owned_stream: Any,
        _reservation: object,
    ) -> float:
        session._players[player.player_id] = player
        session._player_streams[player.player_id] = owned_stream
        session._player_ffmpeg[player.player_id] = ffmpeg
        session.sync_clients.append(player)
        return time.time()

    with (
        patch.object(
            session,
            "_prepare_client_stream",
            new=MagicMock(return_value=stream),
        ),
        patch.object(session, "_start_late_joiner", side_effect=start_late_joiner),
    ):
        add_task = asyncio.create_task(session.add_client(player))
        await connection_waiting.wait()
        add_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await add_task

    ffmpeg.kill.assert_awaited_once_with()
    stream.stop.assert_awaited_once_with(force=True)
    assert player not in session.sync_clients
    assert player.player_id not in session._player_streams


@pytest.mark.asyncio
async def test_stale_late_join_failure_does_not_remove_replacement() -> None:
    """An old connection failure cannot tear down a newer late-join generation."""
    session = _make_session(time.time(), 0)
    player = _make_late_joiner()
    connection_waiting = asyncio.Event()
    fail_connection = asyncio.Event()

    async def wait_for_connection() -> None:
        connection_waiting.set()
        await fail_connection.wait()
        raise PlayerCommandFailed("old stream failed")

    old_stream = MagicMock()
    old_stream.session = session
    old_stream.stop = AsyncMock()
    old_stream.wait_for_connection = AsyncMock(side_effect=wait_for_connection)
    captured_reservation: object | None = None

    async def start_late_joiner(
        _player: Any,
        owned_stream: Any,
        reservation: object,
    ) -> float:
        nonlocal captured_reservation
        captured_reservation = reservation
        session._players[player.player_id] = player
        session._player_streams[player.player_id] = owned_stream
        session.sync_clients.append(player)
        return time.time()

    with (
        patch.object(
            session,
            "_prepare_client_stream",
            new=MagicMock(return_value=old_stream),
        ),
        patch.object(session, "_start_late_joiner", side_effect=start_late_joiner),
    ):
        add_task = asyncio.create_task(session.add_client(player))
        await connection_waiting.wait()
        assert captured_reservation is not None
        replacement_reservation = object()
        replacement_stream = MagicMock()
        replacement_stream.session = session
        replacement_stream.stop = AsyncMock()
        async with session._lock:
            session._starting_clients[player.player_id] = replacement_reservation
            session._player_streams[player.player_id] = replacement_stream
            player.stream = replacement_stream
        fail_connection.set()
        await add_task

    replacement_stream.stop.assert_not_awaited()
    assert player in session.sync_clients
    assert session._player_streams[player.player_id] is replacement_stream
    assert session._starting_clients[player.player_id] is replacement_reservation


@pytest.mark.asyncio
async def test_cancelled_remove_retains_handle_and_stops_audio_source() -> None:
    """Cancelled removal finishes process and last-client source teardown."""
    session = _make_session(time.time(), 0)
    player = session.sync_clients[0]
    kill_started = asyncio.Event()
    release_kill = asyncio.Event()

    async def kill_ffmpeg() -> None:
        kill_started.set()
        await release_kill.wait()

    async def audio_source_task() -> None:
        await asyncio.Event().wait()

    ffmpeg = MagicMock()
    ffmpeg.kill = AsyncMock(side_effect=kill_ffmpeg)
    stream = MagicMock()
    stream.session = session
    stream.stop = AsyncMock()
    session._player_ffmpeg[player.player_id] = ffmpeg
    session._player_streams[player.player_id] = stream
    player.stream = stream
    session._audio_source_task = asyncio.create_task(audio_source_task())

    stop_task = asyncio.create_task(session.remove_client(player, reason="test cancellation"))
    await kill_started.wait()
    stop_task.cancel()
    await asyncio.sleep(0)

    assert session._player_ffmpeg[player.player_id] is ffmpeg
    release_kill.set()
    with pytest.raises(asyncio.CancelledError):
        await stop_task

    ffmpeg.kill.assert_awaited_once_with()
    stream.stop.assert_awaited_once_with(force=True)
    assert player.player_id not in session._player_ffmpeg
    assert player.player_id not in session._player_streams
    assert session._audio_source_task.done()


@pytest.mark.asyncio
async def test_cancelled_remove_waiting_for_client_lock_still_tears_down() -> None:
    """Cancellation while queued on the client lock leaves teardown running."""
    session = _make_session(time.time(), 0)
    player = session.sync_clients[0]
    ffmpeg = MagicMock()
    ffmpeg.kill = AsyncMock()
    stream = MagicMock()
    stream.session = session
    stream.stop = AsyncMock()
    session._player_ffmpeg[player.player_id] = ffmpeg
    session._player_streams[player.player_id] = stream
    player.stream = stream
    stop_lock = session._client_stop_locks.setdefault(player.player_id, asyncio.Lock())

    await stop_lock.acquire()
    try:
        remove_task = asyncio.create_task(
            session.remove_client(player, reason="cancelled while queued")
        )
        await asyncio.sleep(0)
        remove_task.cancel()
        await asyncio.sleep(0)
        assert not remove_task.done()
    finally:
        stop_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await remove_task

    ffmpeg.kill.assert_awaited_once_with()
    stream.stop.assert_awaited_once_with(force=True)
    assert player.player_id not in session._player_ffmpeg
    assert player.player_id not in session._player_streams


@pytest.mark.asyncio
async def test_cancelled_remove_waiting_for_session_lock_still_tears_down() -> None:
    """Cancellation before membership mutation cannot abandon the removal."""
    session = _make_session(time.time(), 0)
    player = session.sync_clients[0]
    stream = MagicMock()
    stream.session = session
    stream.stop = AsyncMock()
    session._player_streams[player.player_id] = stream
    player.stream = stream

    await session._lock.acquire()
    try:
        remove_task = asyncio.create_task(
            session.remove_client(player, reason="cancelled before lock")
        )
        await asyncio.sleep(0)
        remove_task.cancel()
        await asyncio.sleep(0)
        assert not remove_task.done()
    finally:
        session._lock.release()

    with pytest.raises(asyncio.CancelledError):
        await remove_task

    stream.stop.assert_awaited_once_with(force=True)
    assert player not in session.sync_clients
    assert player.player_id not in session._player_streams
