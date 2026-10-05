"""Tests for Flow Mode queue progress tracking."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

from music_assistant_models.enums import PlaybackState

from music_assistant.controllers.player_queues.playback_tracker import PlaybackTrackerMixin

QUEUE_ID = "queue-1"


def _queue_item(item_id: str, seconds_streamed: float | None) -> SimpleNamespace:
    """Create a minimal queue item with stream bookkeeping."""
    return SimpleNamespace(
        queue_item_id=item_id,
        streamdetails=SimpleNamespace(
            seconds_streamed=seconds_streamed,
            seek_position=0,
        ),
        extra_attributes={},
    )


def _flow_progress(
    elapsed: float,
    log: list[SimpleNamespace],
    items: list[SimpleNamespace],
) -> tuple[int | None, float]:
    """Calculate Flow Mode progress with a minimal playback tracker."""
    queue = SimpleNamespace(queue_id=QUEUE_ID, current_index=0, elapsed_time=0)
    tracker = MagicMock()
    tracker._queue_data = {
        QUEUE_ID: SimpleNamespace(flow_mode_stream_log=log),
    }
    tracker.index_by_id.side_effect = lambda _queue_id, item_id: next(
        (index for index, item in enumerate(items) if item.queue_item_id == item_id),
        None,
    )
    tracker.get_item.side_effect = lambda _queue_id, id_or_index: (
        items[id_or_index]
        if isinstance(id_or_index, int)
        else next((item for item in items if item.queue_item_id == id_or_index), None)
    )
    player = SimpleNamespace(
        state=SimpleNamespace(
            corrected_elapsed_time=elapsed,
            playback_state=PlaybackState.PLAYING,
        )
    )

    return PlaybackTrackerMixin._get_flow_queue_stream_index(
        cast("Any", tracker), cast("Any", queue), cast("Any", player)
    )


def test_flow_progress_recovers_unfinished_non_tail_probe_entry() -> None:
    """An abandoned probe entry must not pin progress to the previous track."""
    items = [_queue_item("track-1", 100.0), _queue_item("track-2", None)]
    log = [
        SimpleNamespace(queue_item_id="track-1", seconds_streamed=None),
        SimpleNamespace(queue_item_id="track-2", seconds_streamed=None),
    ]

    index, elapsed = _flow_progress(125.0, log, items)

    assert index == 1
    assert elapsed == 25.0


def test_flow_progress_keeps_unfinished_tail_as_active_entry() -> None:
    """The unfinished tail entry remains the sentinel for the active track."""
    items = [_queue_item("track-1", None)]
    log = [SimpleNamespace(queue_item_id="track-1", seconds_streamed=None)]

    index, elapsed = _flow_progress(25.0, log, items)

    assert index == 0
    assert elapsed == 25.0
