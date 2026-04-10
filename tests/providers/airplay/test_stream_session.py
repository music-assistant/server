"""Unit tests for AirPlay stream session late-join logic."""

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.airplay.stream_session import AirPlayStreamSession

PCM_SAMPLE_SIZE = 176400  # 44.1kHz / 16-bit / 2ch


def _make_session(
    start_time: float,
    seconds_streamed: float,
) -> AirPlayStreamSession:
    """Create a stream session for testing.

    :param start_time: The wall-clock time when the stream was started.
    :param seconds_streamed: How many seconds of audio have been streamed.
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


@pytest.mark.asyncio
async def test_late_join_start_at_matches_seconds_streamed() -> None:
    """Test that start_at = start_time + seconds_streamed.

    The late joiner starts from the current stream position. The NTP
    tells the device to play byte 0 at start_time + seconds_streamed.
    """
    now = time.time()
    start_time = now - 10
    seconds_streamed = 12.5
    session = _make_session(start_time, seconds_streamed)
    player = _make_late_joiner(wait_start_ms=2000)

    captured_start_at: list[float] = []

    def capture_ntp(unix_ts: float) -> int:
        captured_start_at.append(unix_ts)
        return 1

    with (
        patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start,
        patch(
            "music_assistant.providers.airplay.stream_session.unix_time_to_ntp",
            side_effect=capture_ntp,
        ),
    ):
        mock_start.side_effect = _setup_stream(player)
        await session.add_client(player)

    assert captured_start_at, "unix_time_to_ntp was never called"
    expected = start_time + seconds_streamed
    assert abs(captured_start_at[0] - expected) < 0.1, (
        f"start_at should be start_time + seconds_streamed, "
        f"got diff={captured_start_at[0] - expected:.4f}s"
    )


@pytest.mark.asyncio
async def test_late_join_adds_to_sync_clients() -> None:
    """Test that the late joiner is added to sync_clients."""
    now = time.time()
    session = _make_session(now - 10, 12.5)
    player = _make_late_joiner()

    with patch.object(session, "_start_client", new_callable=AsyncMock) as mock_start:
        mock_start.side_effect = _setup_stream(player)
        with patch(
            "music_assistant.providers.airplay.stream_session.unix_time_to_ntp",
            return_value=1,
        ):
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
