"""Unit tests for AirPlay stream session late-join logic."""

import time
from collections import deque
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.airplay.stream_session import AirPlayStreamSession

# 44.1kHz / 16-bit / 2ch
PCM_SAMPLE_SIZE = 176400
BYTES_PER_FRAME = 4  # 16-bit * 2ch


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
    pcm_format.pcm_sample_size = PCM_SAMPLE_SIZE
    pcm_format.sample_rate = 44100

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
        session._chunk_buffer.append((b"\x00" * PCM_SAMPLE_SIZE, pos))

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
async def test_late_join_start_at_equals_preferred() -> None:
    """Test that start_at equals now + wait_start when buffer is available."""
    now = time.time()
    wait_start_s = 2.0
    start_time = now - 50 + wait_start_s
    seconds_streamed = 50.0
    chunk_positions = [float(i) for i in range(40, 50)]

    session = _make_session(start_time, seconds_streamed, chunk_positions)
    player = _make_late_joiner(wait_start_ms=2000)

    start_at, _ = await _run_add_client(session, player)

    # start_at should be exactly preferred (now + wait_start), not just "in the future"
    preferred = now + wait_start_s
    assert abs(start_at - preferred) < 0.1, (
        f"start_at should be ~preferred ({preferred:.2f}), got {start_at:.2f}"
    )


@pytest.mark.asyncio
async def test_late_join_trims_first_chunk_to_align() -> None:
    """Test that the first chunk is trimmed so byte 0 aligns with start_at."""
    now = time.time()
    # Use a larger wait_start so target_position lands inside a chunk
    # (not at the very end of the ring buffer).
    # start_time + target_position = now + wait_start
    # target_position = now + wait_start - start_time
    # With wait_start=5s, start_time = now - 50 + 2, target_position = 53
    # but seconds_streamed=55, so chunk at pos 53 exists and is mid-buffer.
    wait_start_s = 5.0
    start_time = now - 55 + 2.0
    seconds_streamed = 55.0
    # Ring buffer: positions 45-54 (10 chunks)
    chunk_positions = [float(i) for i in range(45, 55)]

    session = _make_session(start_time, seconds_streamed, chunk_positions)
    # target_position = now + 5 - (now - 53) = 58 ... hmm let me recalculate
    # start_time = now - 53, target = (now + 5) - (now - 53) = 58? No.
    # Let me be more precise:
    # start_time = now - 55 + 2 = now - 53
    # target_position = (now + 5) - (now - 53) = 58
    # That's way beyond seconds_streamed (55). Need to adjust.
    #
    # For target to be ~52.5 (mid-chunk at pos 52):
    # target = now + wait_start - start_time
    # We want target = 52.5
    # 52.5 = now + wait_start - start_time
    # start_time = now + wait_start - 52.5
    start_time_fixed = now + wait_start_s - 52.5
    session.start_time = start_time_fixed

    player = _make_late_joiner(wait_start_ms=int(wait_start_s * 1000))

    start_at, fed_chunks = await _run_add_client(session, player)

    # start_at should match preferred (now + wait_start)
    assert abs(start_at - (now + wait_start_s)) < 0.1

    assert fed_chunks, "Expected buffered chunks to be fed"

    first_chunk_data, first_chunk_pos = fed_chunks[0]

    # First chunk position should be target_position (52.5)
    assert abs(first_chunk_pos - 52.5) < 0.01

    # The first chunk should be trimmed to roughly half (target is mid-chunk)
    assert len(first_chunk_data) < PCM_SAMPLE_SIZE
    half = PCM_SAMPLE_SIZE // 2
    assert abs(len(first_chunk_data) - half) < PCM_SAMPLE_SIZE * 0.05

    # Trimmed size should be frame-aligned
    assert len(first_chunk_data) % BYTES_PER_FRAME == 0

    # Subsequent chunks should be full-size
    if len(fed_chunks) > 1:
        assert len(fed_chunks[1][0]) == PCM_SAMPLE_SIZE


@pytest.mark.asyncio
async def test_late_join_fallback_when_buffer_too_old() -> None:
    """Test fallback when ring buffer doesn't contain target_position."""
    now = time.time()
    wait_start_s = 2.0
    start_time = now - 50 + wait_start_s
    seconds_streamed = 50.0
    # Only old chunks that are still in the future
    chunk_positions = [49.0, 50.0]

    session = _make_session(start_time, seconds_streamed, chunk_positions)
    player = _make_late_joiner(wait_start_ms=2000)

    start_at, fed_chunks = await _run_add_client(session, player)

    # Should still use chunks and start_at should be in the future
    assert fed_chunks, "Expected buffered chunks to be fed"
    assert start_at > now, "start_at should be in the future"


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
    assert 1.5 < (start_at - now) < 2.5, (
        f"Expected start_at ~2s from now, got {start_at - now:.2f}s"
    )
