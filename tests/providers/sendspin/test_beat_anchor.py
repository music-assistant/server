"""Tests for the sendspin per-track beat-schedule anchor offset."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from music_assistant_models.player_queue import PlayLogEntry

from music_assistant.providers.sendspin.player import SendspinPlayer

if TYPE_CHECKING:
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.controllers.player_queues.state import PlayerQueueData


def _queue_item(queue_item_id: str, seek: int = 0) -> QueueItem:
    return cast(
        "QueueItem",
        SimpleNamespace(
            queue_item_id=queue_item_id,
            streamdetails=SimpleNamespace(seek_position=seek),
        ),
    )


def _log_entry(queue_item_id: str, seconds_streamed: float | None) -> PlayLogEntry:
    return PlayLogEntry(queue_item_id=queue_item_id, seconds_streamed=seconds_streamed)


def test_flow_offset_sums_prior_tracks() -> None:
    """A later track anchors past the summed stream-time of all prior flow tracks."""
    data = cast(
        "PlayerQueueData",
        SimpleNamespace(
            flow_mode_stream_log=[
                _log_entry("t1", 100.0),
                _log_entry("t2", 200.5),
                _log_entry("t3", None),  # current track, still streaming
            ]
        ),
    )
    offset_us = SendspinPlayer._flow_track_offset_us(data, _queue_item("t3"), 400.0)
    assert offset_us == int((100.0 + 200.5) * 1_000_000)


def test_flow_offset_picks_the_playing_pass_when_the_stream_runs_ahead() -> None:
    """A repeated track resolves to the pass that is playing, not one already buffered."""
    data = cast(
        "PlayerQueueData",
        SimpleNamespace(
            flow_mode_stream_log=[
                _log_entry("a", 10.0),
                _log_entry("b", 10.0),
                _log_entry("a", 10.0),
                _log_entry("b", 10.0),
                _log_entry("a", None),
            ]
        ),
    )

    assert SendspinPlayer._flow_track_offset_us(data, _queue_item("b"), 12.0) == 10_000_000


@pytest.mark.parametrize(
    ("recorded_seek", "shared_seek", "expected"),
    [(59.0, 0, -59_000_000), (0.0, 59, 0), (None, 59, -59_000_000)],
)
def test_flow_offset_uses_recorded_seek(
    recorded_seek: float | None, shared_seek: int, expected: int
) -> None:
    """Recorded offsets survive repeat preparation; only None uses the shared fallback."""
    data = cast(
        "PlayerQueueData",
        SimpleNamespace(flow_mode_stream_log=[PlayLogEntry("book", seek_position=recorded_seek)]),
    )

    assert (
        SendspinPlayer._flow_track_offset_us(data, _queue_item("book", shared_seek), 0.0)
        == expected
    )


def test_repeated_flow_offset_includes_previous_pass() -> None:
    """A new repeat anchors after the previous pass without inheriting its seek offset."""
    data = cast(
        "PlayerQueueData",
        SimpleNamespace(
            flow_mode_stream_log=[
                PlayLogEntry("book", seconds_streamed=61, seek_position=59),
                PlayLogEntry("book", seek_position=0),
            ]
        ),
    )

    assert SendspinPlayer._flow_track_offset_us(data, _queue_item("book", 59), 70.0) == 61_000_000
