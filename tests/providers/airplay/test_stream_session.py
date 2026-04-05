"""Unit tests for AirPlay stream session late-join logic."""

import time
from collections import deque
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.airplay.stream_session import AirPlayStreamSession


def _make_session(
    start_time: float,
    seconds_streamed: float,
    chunk_positions: list[float],
) -> AirPlayStreamSession:
    """Create a stream session with pre-filled ring buffer for testing.

    :param start_time: The wall-clock time when the stream was started.
    :param seconds_streamed: How many seconds of audio have been streamed.
    :param chunk_positions: List of stream positions for each chunk in the ring buffer.
    """
    prov = MagicMock()

    pcm_format = MagicMock()
    pcm_format.pcm_sample_size = 176400  # 44.1kHz/16bit/2ch

    leader = MagicMock()
    leader.stream = MagicMock()
    leader.stream.running = True

    session = AirPlayStreamSession(prov, [leader], pcm_format)
    session.start_time = start_time
    session.seconds_streamed = seconds_streamed
    session.start_ntp = 1  # dummy
    session.wait_start = 2.0

    # Fill ring buffer with dummy chunks at specified positions
    session._chunk_buffer = deque(maxlen=12)
    for pos in chunk_positions:
        session._chunk_buffer.append((b"\x00" * 176400, pos))

    return session


def _make_late_joiner(wait_start_ms: int = 2000) -> MagicMock:
    """Create a mock AirPlay player for late-join testing.

    :param wait_start_ms: The wait_start value in milliseconds.
    """
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


async def _run_add_client(
    session: AirPlayStreamSession,
    player: MagicMock,
) -> tuple[float, list[tuple[bytes, float]]]:
    """Run add_client and return (captured_start_at, fed_chunks).

    Patches unix_time_to_ntp to capture the start_at value and
    _start_client/_feed_buffered_chunks to avoid real I/O.
    """
    captured_start_at: list[float] = []

    def capture_ntp(unix_ts: float) -> int:
        captured_start_at.append(unix_ts)
        return 1  # dummy NTP value

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_feed_buffered_chunks", new_callable=AsyncMock) as mock_feed,
        patch(
            "music_assistant.providers.airplay.stream_session.unix_time_to_ntp",
            side_effect=capture_ntp,
        ),
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

        fed_chunks: list[tuple[bytes, float]] = []
        if mock_feed.called:
            fed_chunks = list(mock_feed.call_args.args[1])

    assert captured_start_at, "unix_time_to_ntp was never called"
    return captured_start_at[0], fed_chunks


@pytest.mark.asyncio
async def test_late_join_start_at_is_in_the_future() -> None:
    """Test that a late joiner's start_at is always in the future."""
    now = time.time()
    wait_start_s = 2.0
    start_time = now - 50 + wait_start_s
    seconds_streamed = 50.0
    chunk_positions = [float(i) for i in range(40, 50)]

    session = _make_session(start_time, seconds_streamed, chunk_positions)
    player = _make_late_joiner(wait_start_ms=2000)

    start_at, _ = await _run_add_client(session, player)
    assert start_at > now, f"start_at should be in the future, got {start_at - now:.2f}s from now"


@pytest.mark.asyncio
async def test_late_join_start_at_aligns_to_chunk_position() -> None:
    """Test that start_at is aligned to actual ring buffer chunk positions."""
    now = time.time()
    wait_start_s = 2.0
    start_time = now - 50 + wait_start_s
    seconds_streamed = 50.0
    chunk_positions = [float(i) for i in range(40, 50)]

    session = _make_session(start_time, seconds_streamed, chunk_positions)
    player = _make_late_joiner(wait_start_ms=2000)

    start_at, fed_chunks = await _run_add_client(session, player)

    assert fed_chunks, "Expected buffered chunks to be fed"

    # All fed chunks should map to future wall-clock times
    for _chunk_data, pos in fed_chunks:
        chunk_wall_time = start_time + pos
        assert chunk_wall_time > now, (
            f"Chunk at position {pos} maps to wall time "
            f"{chunk_wall_time - now:.2f}s from now (should be positive)"
        )

    # start_at should match the first chunk's wall-clock time
    first_pos = fed_chunks[0][1]
    assert abs(start_at - (start_time + first_pos)) < 0.01


@pytest.mark.asyncio
async def test_late_join_postpones_when_insufficient_buffer() -> None:
    """Test that start_at is postponed when < 2 chunks are available at target position."""
    now = time.time()
    wait_start_s = 2.0
    start_time = now - 50 + wait_start_s
    seconds_streamed = 50.0
    chunk_positions = [49.0, 50.0]

    session = _make_session(start_time, seconds_streamed, chunk_positions)
    player = _make_late_joiner(wait_start_ms=2000)

    start_at, fed_chunks = await _run_add_client(session, player)

    assert fed_chunks, "Expected buffered chunks to be fed"
    assert len(fed_chunks) >= 1
    assert start_at > now, "start_at should be in the future after postpone"


@pytest.mark.asyncio
async def test_late_join_no_buffer_uses_preferred_start() -> None:
    """Test that with no ring buffer, preferred start_at is used."""
    now = time.time()
    wait_start_s = 2.0
    start_time = now - 50 + wait_start_s
    seconds_streamed = 50.0

    session = _make_session(start_time, seconds_streamed, chunk_positions=[])
    player = _make_late_joiner(wait_start_ms=2000)

    start_at, fed_chunks = await _run_add_client(session, player)

    assert not fed_chunks, "No chunks should be fed when buffer is empty"
    # start_at should be approximately now + wait_start
    assert 1.5 < (start_at - now) < 2.5, (
        f"Expected start_at ~2s from now, got {start_at - now:.2f}s"
    )
