"""
Tests for the shared boundary index every queue mutation inserts behind.

The concept used to be spelled out inline at each call site in four different ways, only some of
which survived a repeat-all wrap putting the buffered index in front of the playing one.
"""

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
        (4, 0, 4),
        (3, None, 3),
        (None, 1, 1),
        (None, None, None),
        (0, 0, 0),
    ],
)
def test_committed_index(
    current_index: int | None, index_in_buffer: int | None, expected: int | None
) -> None:
    """The boundary is whichever of the two positions is furthest into the queue."""
    assert committed_index(_queue(current_index, index_in_buffer)) == expected


def test_a_wrapped_buffer_never_reports_a_boundary_behind_the_playing_track() -> None:
    """
    Repeat-all wraps the buffered index to the front, and the boundary must not follow it.

    Reporting index 0 while the player is on the last track sends every insert behind the
    playhead, where the queue's own bounds check drops it.
    """
    assert committed_index(_queue(current_index=4, index_in_buffer=0)) == 4
