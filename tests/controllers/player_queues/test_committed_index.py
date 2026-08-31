"""Tests for the shared boundary index every queue insert path places new items behind."""

from __future__ import annotations

import pytest
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues.helpers import committed_index


def _queue(current_index: int | None, index_in_buffer: int | None) -> PlayerQueue:
    """Build a queue parked at the given playing and buffered indexes."""
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=5)
    queue.current_index = current_index
    queue.index_in_buffer = index_in_buffer
    return queue


@pytest.mark.parametrize(
    ("current_index", "index_in_buffer", "expected"),
    [
        (2, 2, 2),
        (2, 3, 3),
        (0, 4, 4),
        (3, None, 3),
        (None, 1, 1),
        (None, None, None),
        (0, 0, 0),
    ],
)
def test_committed_index(
    current_index: int | None, index_in_buffer: int | None, expected: int | None
) -> None:
    """The buffered track is the boundary, falling back to the playing one."""
    assert committed_index(_queue(current_index, index_in_buffer)) == expected


def test_a_wrapped_buffer_reports_the_buffered_track_at_the_front() -> None:
    """Repeat loops the buffered track to the front and the boundary follows it there."""
    assert committed_index(_queue(current_index=4, index_in_buffer=0)) == 0
