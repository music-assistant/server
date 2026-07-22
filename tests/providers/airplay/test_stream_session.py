"""Unit tests for AirPlay stream session late-join logic."""

import asyncio
import time
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import PlayerCommandFailed

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
    leader.stream = MagicMock()
    leader.stream.running = True

    session = AirPlayStreamSession(prov, [leader], pcm_format, MagicMock())
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
    return player


def _setup_stream(player: MagicMock) -> Any:
    """Return a side_effect callable that sets up the stream mock on the player."""

    def _side_effect(*_args: Any, **_kwargs: Any) -> None:
        player.stream = MagicMock()
        player.stream.running = True
        player.stream.wait_for_connection = AsyncMock()

    return _side_effect


def _captured_start_at(mock_start: AsyncMock) -> float:
    """Return the start time (unix seconds) passed to the patched _start_client."""
    start_unix_ms = mock_start.call_args[0][1]
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
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_write_chunk_to_player", new_callable=AsyncMock),
    ):
        mock_start.side_effect = _setup_stream(player)
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
