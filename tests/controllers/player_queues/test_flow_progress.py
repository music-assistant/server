"""Tests for Flow Mode queue progress tracking."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.player_queue import PlayLogEntry

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
    log: list[PlayLogEntry],
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
        PlayLogEntry(queue_item_id="track-1", seconds_streamed=None),
        PlayLogEntry(queue_item_id="track-2", seconds_streamed=None),
    ]

    index, elapsed = _flow_progress(125.0, log, items)

    assert index == 1
    assert elapsed == 25.0


@pytest.mark.parametrize(("seek_position", "expected"), [(None, 84.0), (0.0, 25.0), (10.5, 35.5)])
def test_flow_progress_uses_optional_recorded_offset(
    seek_position: float | None, expected: float
) -> None:
    """Only an unspecified offset falls back to the queue item's mutable offset."""
    item = _queue_item("track-1", None)
    item.streamdetails.seek_position = 59
    log = [PlayLogEntry("track-1", seek_position=seek_position)]

    index, elapsed = _flow_progress(25.0, log, [item])

    assert index == 0
    assert elapsed == expected


def test_flow_progress_keeps_unfinished_tail_as_active_entry() -> None:
    """The unfinished tail entry remains the sentinel for the active track."""
    items = [_queue_item("track-1", None)]
    log = [PlayLogEntry(queue_item_id="track-1", seconds_streamed=None)]

    index, elapsed = _flow_progress(25.0, log, items)

    assert index == 0
    assert elapsed == 25.0


class _Tracker(PlaybackTrackerMixin):
    """Playback tracker over a fixed list of queue items."""

    def __init__(self, items: list[SimpleNamespace], log: list[PlayLogEntry]) -> None:
        self.items = items
        self.mass = MagicMock()
        self.mass.streams.base_url = "http://ma:8097"
        self._queue_data = {QUEUE_ID: SimpleNamespace(flow_mode_stream_log=log)}  # type: ignore[dict-item]

    def get_item(self, queue_id: str, item_id_or_index: int | str | None) -> Any:
        if isinstance(item_id_or_index, int):
            return self.items[item_id_or_index]
        return next((i for i in self.items if i.queue_item_id == item_id_or_index), None)

    def index_by_id(self, queue_id: str, queue_item_id: str) -> int | None:
        return next((n for n, i in enumerate(self.items) if i.queue_item_id == queue_item_id), None)

    def get_next_item(self, queue_id: str, cur_index: int | str) -> Any:
        return None


@pytest.mark.parametrize(
    ("uri", "updated", "expected_index"),
    [
        ("http://ma:8097/command/sess-1/queue-1/next.mp3", False, 1),
        ("http://ma:8097/flow/sess-1/queue-1/track-1/player-1.mp3", True, 0),
    ],
    ids=["command_item", "flow_stream"],
)
def test_flow_progress_ignores_the_command_item(
    uri: str, updated: bool, expected_index: int
) -> None:
    """
    A player on the command item after the flow stream keeps the queue at its last track.

    A Cast receiver that drops the flow stream at the end moves on to the 'next' command item
    and reports it at position 0, which is no position in the flow stream.
    """
    items = [_queue_item("track-1", 100.0), _queue_item("track-2", 100.0)]
    tracker = _Tracker(items, [PlayLogEntry("track-1"), PlayLogEntry("track-2")])
    queue = SimpleNamespace(
        queue_id=QUEUE_ID,
        active=True,
        flow_mode=True,
        state=PlaybackState.PLAYING,
        current_index=1,
        elapsed_time=95.0,
    )
    player = SimpleNamespace(
        active_output_protocol="native",
        current_media=SimpleNamespace(uri=uri, source_id=None, queue_item_id=None),
        state=SimpleNamespace(corrected_elapsed_time=0.0, playback_state=PlaybackState.PLAYING),
    )

    result = tracker._update_current_index_from_player(cast("Any", queue), cast("Any", player))

    assert result is updated
    assert queue.current_index == expected_index
