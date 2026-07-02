"""Tests for the sendspin per-track beat-schedule anchor offset."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

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


def _log_entry(queue_item_id: str, seconds_streamed: float | None) -> SimpleNamespace:
    return SimpleNamespace(queue_item_id=queue_item_id, seconds_streamed=seconds_streamed)


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
    offset_us = SendspinPlayer._flow_track_offset_us(data, _queue_item("t3"))
    assert offset_us == int((100.0 + 200.5) * 1_000_000)
