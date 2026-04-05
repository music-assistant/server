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


def _get_debug_log_args(session: AirPlayStreamSession) -> tuple[Any, ...]:
    """Get the positional args from the first debug log call."""
    mock_logger: MagicMock = session.prov.logger  # type: ignore[assignment]
    debug_calls = mock_logger.debug.call_args_list
    assert len(debug_calls) >= 1
    result: tuple[Any, ...] = debug_calls[0].args
    return result


@pytest.mark.asyncio
async def test_late_join_start_at_is_in_the_future() -> None:
    """Test that a late joiner's start_at is always in the future."""
    now = time.time()
    wait_start_s = 2.0
    # Stream started 50 seconds ago with 2s wait_start
    start_time = now - 50 + wait_start_s
    seconds_streamed = 50.0
    # Ring buffer: last 10 chunks (positions 40-49)
    chunk_positions = [float(i) for i in range(40, 50)]

    session = _make_session(start_time, seconds_streamed, chunk_positions)
    player = _make_late_joiner(wait_start_ms=2000)

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_feed_buffered_chunks", new_callable=AsyncMock),
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    log_args = _get_debug_log_args(session)
    assert "start_at is" in log_args[0]
    # The "%.2fs from now" argument (start_at - now)
    start_at_from_now = log_args[6]
    assert start_at_from_now > 0, f"start_at should be in the future, got {start_at_from_now}s"


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

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_feed_buffered_chunks", new_callable=AsyncMock) as mock_feed,
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

        # Verify buffered chunks were fed
        assert mock_feed.called
        fed_chunks = mock_feed.call_args.args[1]

        # All fed chunks should have positions that map to future wall-clock times
        for _chunk_data, pos in fed_chunks:
            chunk_wall_time = start_time + pos
            assert chunk_wall_time > now, (
                f"Chunk at position {pos} maps to wall time "
                f"{chunk_wall_time - now:.2f}s from now (should be positive)"
            )


@pytest.mark.asyncio
async def test_late_join_postpones_when_insufficient_buffer() -> None:
    """Test that start_at is postponed when < 2 chunks are available at target position."""
    now = time.time()
    wait_start_s = 2.0
    start_time = now - 50 + wait_start_s
    seconds_streamed = 50.0
    # Only 2 chunks, both very recent (positions map to future times)
    chunk_positions = [49.0, 50.0]

    session = _make_session(start_time, seconds_streamed, chunk_positions)
    player = _make_late_joiner(wait_start_ms=2000)

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_feed_buffered_chunks", new_callable=AsyncMock) as mock_feed,
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

        # Should still send chunks (postponed but usable)
        assert mock_feed.called
        fed_chunks = mock_feed.call_args.args[1]
        assert len(fed_chunks) >= 1

        # start_at should still be in the future
        log_args = _get_debug_log_args(session)
        start_at_from_now = log_args[6]
        assert start_at_from_now > 0


@pytest.mark.asyncio
async def test_late_join_no_buffer_uses_preferred_start() -> None:
    """Test that with no ring buffer, preferred start_at is used."""
    now = time.time()
    wait_start_s = 2.0
    start_time = now - 50 + wait_start_s
    seconds_streamed = 50.0

    session = _make_session(start_time, seconds_streamed, chunk_positions=[])
    player = _make_late_joiner(wait_start_ms=2000)

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch.object(session, "_feed_buffered_chunks", new_callable=AsyncMock) as mock_feed,
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

        # No buffer to feed
        assert not mock_feed.called

        # start_at should be approximately now + wait_start
        log_args = _get_debug_log_args(session)
        start_at_from_now = log_args[3]
        assert 1.5 < start_at_from_now < 2.5, (
            f"Expected start_at ~2s from now, got {start_at_from_now}s"
        )
