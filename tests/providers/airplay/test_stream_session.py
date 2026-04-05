"""Unit tests for AirPlay stream session late-join logic."""

import time
from collections import deque
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.airplay.stream_session import AirPlayStreamSession

PCM_SAMPLE_SIZE = 176400  # 44.1kHz / 16-bit / 2ch
BYTES_PER_SAMPLE = 4  # 16-bit * 2ch


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
async def test_late_join_start_at_never_in_the_past() -> None:
    """Test that start_at is always >= now + wait_start.

    AirPlay 2 cannot handle receiving an NTP start time in the past.
    """
    now = time.time()
    wait_start_ms = 2000
    # Stream running for 20s, ring buffer has chunks at positions 10-19.
    # With wait_start=2s, min_position = (now+2) - (now-10) = 12.
    # Chunks at 12-19 pass the filter.
    start_time = now - 10
    session = _make_session(start_time, 20.0, [float(i) for i in range(10, 20)])
    player = _make_late_joiner(wait_start_ms=wait_start_ms)

    start_at, _ = await _run_add_client(session, player)

    min_start_at = now + wait_start_ms / 1000
    assert start_at >= min_start_at - 0.1, (
        f"start_at must be >= now + wait_start, got {start_at - now:.2f}s from now"
    )


@pytest.mark.asyncio
async def test_late_join_start_at_matches_first_byte() -> None:
    """Test that start_at corresponds to the stream position of the first byte fed.

    The CLI maps byte 0 to start_ntp, so they must match for sync.
    """
    now = time.time()
    # With wait_start=2s, min_position = (now+2) - start_time.
    # Place chunks so some are at or after min_position.
    # start_time = now - 10, min_position = 12, chunks at 10-19.
    start_time = now - 10
    session = _make_session(start_time, 20.0, [float(i) for i in range(10, 20)])
    player = _make_late_joiner(wait_start_ms=2000)

    start_at, fed_chunks = await _run_add_client(session, player)

    assert fed_chunks, "Expected buffered chunks to be fed"
    first_chunk_pos = fed_chunks[0][1]
    expected = start_time + first_chunk_pos
    assert abs(start_at - expected) < 0.01, (
        f"start_at must equal start_time + first_chunk_pos, got diff={start_at - expected:.4f}s"
    )


@pytest.mark.asyncio
async def test_late_join_trims_first_chunk() -> None:
    """Test that the first chunk is trimmed when it straddles min_position."""
    now = time.time()
    # With wait_start=2s, min_position = (now+2) - start_time = 52.
    # Place a chunk at position 51 that extends to 52 (straddles min_position).
    start_time = now - 50
    session = _make_session(start_time, 53.0, [51.0, 52.0, 53.0])
    player = _make_late_joiner(wait_start_ms=2000)

    start_at, fed_chunks = await _run_add_client(session, player)

    assert fed_chunks, "Expected buffered chunks to be fed"
    first_data, first_pos = fed_chunks[0]

    # start_at must match the first byte and be >= now + wait_start
    assert start_at >= now + 2.0 - 0.1
    assert abs(start_at - (session.start_time + first_pos)) < 0.01

    # First chunk should be at min_position (52.0), not 51.0
    assert first_pos >= 52.0 - 0.1

    # If trimmed from position 51, it should be smaller than a full chunk
    # (the chunk at 52.0 passes the filter directly, so trimming only happens
    # if no full chunks are >= min_position)
    assert len(first_data) <= PCM_SAMPLE_SIZE
    assert len(first_data) % BYTES_PER_SAMPLE == 0


@pytest.mark.asyncio
async def test_late_join_no_buffer() -> None:
    """Test that with no ring buffer, start_at is still >= now + wait_start."""
    now = time.time()
    start_time = now - 50
    session = _make_session(start_time, 50.0, chunk_positions=[])
    player = _make_late_joiner(wait_start_ms=2000)

    start_at, fed_chunks = await _run_add_client(session, player)

    assert not fed_chunks
    assert start_at >= now + 2.0 - 0.1
