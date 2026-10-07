"""Tests for locating the selected item in a windowed Plex play queue."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from music_assistant.providers.plex_connect.queue_commands import QueueCommandsMixin


class _QueueHandler(QueueCommandsMixin):
    """QueueCommandsMixin with the host-class attributes mocked."""

    def __init__(self) -> None:
        self.provider = Mock()
        self._ma_player_id = "player1"
        self._updating_from_plex = False
        self.play_queue_id = None
        self.play_queue_item_ids: dict[int, int] = {}


def _make_playqueue(
    first_track: int,
    last_track: int,
    selected_track: int | None,
    selected_offset: int | None,
) -> SimpleNamespace:
    """
    Build a fake windowed PlayQueue.

    Track number N gets playQueueItemID 1000+N; the items list covers
    first_track..last_track while selected_offset is the absolute (full-queue)
    offset as reported by the Plex server.
    """
    items = [SimpleNamespace(playQueueItemID=1000 + n) for n in range(first_track, last_track + 1)]
    return SimpleNamespace(
        items=items,
        playQueueSelectedItemID=1000 + selected_track if selected_track is not None else None,
        playQueueSelectedItemOffset=selected_offset,
    )


def test_selected_index_windowed_queue() -> None:
    """The absolute offset must not be used as index into a mid-queue window."""
    handler = _QueueHandler()
    # Track 94 selected in a 200-track album; server window starts at track 44.
    # Absolute offset 93 would land on track 137 (the reported 2x-51 bug).
    playqueue = _make_playqueue(44, 200, selected_track=94, selected_offset=93)
    index = handler._selected_item_index(playqueue)
    assert playqueue.items[index].playQueueItemID == 1000 + 94


def test_selected_index_full_queue() -> None:
    """When the window covers the full queue, ID match equals the offset."""
    handler = _QueueHandler()
    playqueue = _make_playqueue(1, 100, selected_track=52, selected_offset=51)
    assert handler._selected_item_index(playqueue) == 51


def test_selected_index_falls_back_to_offset() -> None:
    """Without a selected item ID (track radio), fall back to the raw offset."""
    handler = _QueueHandler()
    playqueue = _make_playqueue(1, 10, selected_track=None, selected_offset=3)
    assert handler._selected_item_index(playqueue) == 3


def test_selected_index_out_of_range_offset() -> None:
    """An offset beyond the fetched items must not raise; default to first item."""
    handler = _QueueHandler()
    playqueue = _make_playqueue(1, 10, selected_track=None, selected_offset=42)
    assert handler._selected_item_index(playqueue) == 0


def test_selected_index_negative_offset() -> None:
    """A negative offset must not index from the end of the list; default to first item."""
    handler = _QueueHandler()
    playqueue = _make_playqueue(1, 10, selected_track=None, selected_offset=-1)
    assert handler._selected_item_index(playqueue) == 0


def test_selected_index_none_offset() -> None:
    """A missing offset (None) defaults to the first item."""
    handler = _QueueHandler()
    playqueue = _make_playqueue(1, 10, selected_track=None, selected_offset=None)
    assert handler._selected_item_index(playqueue) == 0
