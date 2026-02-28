"""Tests for SendspinPlaybackSession drain-delay helpers."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from music_assistant.providers.sendspin.playback import SendspinPlaybackSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CH_A = uuid4()
CH_B = uuid4()
CH_STALE = uuid4()  # channel for a disconnected member


def _make_session(
    *,
    push_stream: Any = None,
) -> SendspinPlaybackSession:
    """Build a minimal SendspinPlaybackSession with mocked player."""
    player = MagicMock()
    player.player_id = "test-player-1"
    player.logger = logging.getLogger("test.sendspin.playback")

    session = object.__new__(SendspinPlaybackSession)
    session.player = player
    session._push_stream = push_stream
    return session


def _make_push_stream(
    *,
    channel_timing: dict[UUID, int] | None = None,
    active_channels: set[UUID] | None = None,
    now_us: int = 0,
) -> MagicMock:
    """Build a mock PushStream with controllable timing internals."""
    ps = MagicMock()
    ps._channel_timing = channel_timing or {}
    ps._get_active_audio_channels.return_value = active_channels or set()
    ps._clock.now_us.return_value = now_us
    return ps


def _make_session_with_queue(
    *,
    queue: object | None = None,
    next_item: object | None = None,
    index_by_id_result: int | None = None,
) -> SendspinPlaybackSession:
    """Build a session with mocked queue API."""
    session = _make_session()
    pq = cast("MagicMock", session.player.mass.player_queues)
    pq.get_active_queue.return_value = queue
    pq.get_next_item.return_value = next_item
    pq.index_by_id.return_value = index_by_id_result
    return session


# ---------------------------------------------------------------------------
# _get_stream_end_delay_s
# ---------------------------------------------------------------------------


def test_delay_no_push_stream_returns_zero() -> None:
    """No push stream means no delay."""
    session = _make_session(push_stream=None)
    assert session._get_stream_end_delay_s() == 0.0


def test_delay_no_active_channels_returns_zero() -> None:
    """Stale channels with no active audio should not produce a delay."""
    ps = _make_push_stream(
        channel_timing={CH_STALE: 5_000_000},
        active_channels=set(),
        now_us=0,
    )
    session = _make_session(push_stream=ps)
    assert session._get_stream_end_delay_s() == 0.0


def test_delay_clock_past_end_returns_zero() -> None:
    """If the clock is past the last committed audio, no delay needed."""
    ps = _make_push_stream(
        channel_timing={CH_A: 10_000_000},
        active_channels={CH_A},
        now_us=12_000_000,
    )
    session = _make_session(push_stream=ps)
    assert session._get_stream_end_delay_s() == 0.0


def test_delay_clock_at_end_returns_zero() -> None:
    """Exactly at the end timestamp — no wait required."""
    ps = _make_push_stream(
        channel_timing={CH_A: 10_000_000},
        active_channels={CH_A},
        now_us=10_000_000,
    )
    session = _make_session(push_stream=ps)
    assert session._get_stream_end_delay_s() == 0.0


def test_delay_basic_computation() -> None:
    """2.7 seconds of buffered audio remaining."""
    ps = _make_push_stream(
        channel_timing={CH_A: 12_700_000},
        active_channels={CH_A},
        now_us=10_000_000,
    )
    session = _make_session(push_stream=ps)
    assert session._get_stream_end_delay_s() == pytest.approx(2.7)


def test_delay_multiple_channels_uses_max() -> None:
    """With multiple channels, use the one that finishes last."""
    ps = _make_push_stream(
        channel_timing={CH_A: 11_000_000, CH_B: 13_000_000},
        active_channels={CH_A, CH_B},
        now_us=10_000_000,
    )
    session = _make_session(push_stream=ps)
    assert session._get_stream_end_delay_s() == pytest.approx(3.0)


def test_delay_stale_channels_excluded() -> None:
    """Disconnected member channels should not inflate the wait."""
    ps = _make_push_stream(
        channel_timing={CH_A: 11_000_000, CH_STALE: 20_000_000},
        active_channels={CH_A},
        now_us=10_000_000,
    )
    session = _make_session(push_stream=ps)
    assert session._get_stream_end_delay_s() == pytest.approx(1.0)


def test_delay_capped_at_producer_buffer_limit() -> None:
    """Delay should never exceed the 30s producer buffer limit."""
    ps = _make_push_stream(
        channel_timing={CH_A: 70_000_000},
        active_channels={CH_A},
        now_us=10_000_000,
    )
    session = _make_session(push_stream=ps)
    assert session._get_stream_end_delay_s() == pytest.approx(30.0)


def test_delay_exception_returns_zero() -> None:
    """Graceful degradation: any exception falls back to 0.0."""
    ps = _make_push_stream()
    ps._get_active_audio_channels.side_effect = RuntimeError("boom")
    session = _make_session(push_stream=ps)
    assert session._get_stream_end_delay_s() == 0.0


# ---------------------------------------------------------------------------
# _peek_next_queue_index
# ---------------------------------------------------------------------------


def test_peek_no_active_queue() -> None:
    """No active queue returns None."""
    session = _make_session_with_queue(queue=None)
    assert session._peek_next_queue_index() is None


def test_peek_no_current_index() -> None:
    """Queue with no current_index returns None."""
    queue = SimpleNamespace(queue_id="q1", current_index=None)
    session = _make_session_with_queue(queue=queue)
    assert session._peek_next_queue_index() is None


def test_peek_no_next_item() -> None:
    """No next item in the queue returns None (end of album)."""
    queue = SimpleNamespace(queue_id="q1", current_index=5)
    session = _make_session_with_queue(queue=queue, next_item=None)
    assert session._peek_next_queue_index() is None


def test_peek_returns_queue_id_and_index() -> None:
    """Happy path: next item exists, returns (queue_id, index) tuple."""
    queue = SimpleNamespace(queue_id="q1", current_index=5)
    next_item = SimpleNamespace(queue_item_id="item-6")
    session = _make_session_with_queue(
        queue=queue,
        next_item=next_item,
        index_by_id_result=6,
    )
    assert session._peek_next_queue_index() == ("q1", 6)
    # Verify correct args were passed through
    pq = cast("MagicMock", session.player.mass.player_queues)
    pq.get_next_item.assert_called_once_with("q1", 5)
    pq.index_by_id.assert_called_once_with("q1", "item-6")


def test_peek_index_by_id_returns_none() -> None:
    """Item existed but was removed before index_by_id — returns None."""
    queue = SimpleNamespace(queue_id="q1", current_index=5)
    next_item = SimpleNamespace(queue_item_id="item-gone")
    session = _make_session_with_queue(
        queue=queue,
        next_item=next_item,
        index_by_id_result=None,
    )
    assert session._peek_next_queue_index() is None
