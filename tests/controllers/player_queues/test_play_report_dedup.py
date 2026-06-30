"""Tests for de-duplicating the doubled end-of-queue play report."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue


def _tracker() -> PlayerQueuesController:
    """Build a bare controller with the two reported queues registered."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl._queue_data = {
        queue_id: PlayerQueueData(queue=cast("PlayerQueue", Mock(queue_id=queue_id)))
        for queue_id in ("q1", "q2")
    }
    return ctrl


def test_completed_play_counts_once() -> None:
    """The doubled end-of-queue completion report is only counted once."""
    tracker = _tracker()
    # first completion report -> counted
    assert tracker._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is True
    # second (duplicate) completion report for the same item -> skipped
    assert tracker._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is False


def test_repeat_counts_again_after_restart() -> None:
    """A looped track is counted again once it restarts."""
    tracker = _tracker()
    assert tracker._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is True
    # the track restarts (a not-fully-played report) -> guard resets
    assert tracker._should_mark_played("q1", "item1", fully_played=False, is_playing=True) is True
    # the next completion is counted again
    assert tracker._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is True


def test_now_playing_reports_are_never_deduplicated() -> None:
    """Now-playing / partial reports are always forwarded."""
    tracker = _tracker()
    assert tracker._should_mark_played("q1", "item1", fully_played=False, is_playing=True) is True
    assert tracker._should_mark_played("q1", "item1", fully_played=False, is_playing=True) is True


def test_distinct_items_each_count() -> None:
    """Two different queue items are counted independently."""
    tracker = _tracker()
    assert tracker._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is True
    assert tracker._should_mark_played("q1", "item2", fully_played=True, is_playing=False) is True


def test_partial_report_for_other_item_does_not_reset_guard() -> None:
    """A not-fully-played report for a different item must not re-arm another item's guard."""
    tracker = _tracker()
    assert tracker._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is True
    # a partial report for a *different* item must not reset item1's guard
    assert tracker._should_mark_played("q1", "item2", fully_played=False, is_playing=True) is True
    # so the duplicate completion of item1 is still skipped
    assert tracker._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is False


def test_queues_are_independent() -> None:
    """Each queue tracks its own last-counted play."""
    tracker = _tracker()
    assert tracker._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is True
    # the same item id on a different queue is counted independently
    assert tracker._should_mark_played("q2", "item1", fully_played=True, is_playing=False) is True
    # and each queue's own duplicate is skipped
    assert tracker._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is False
    assert tracker._should_mark_played("q2", "item1", fully_played=True, is_playing=False) is False


def test_not_fully_played_first_report_is_forwarded() -> None:
    """A not-fully-played report with nothing counted yet is forwarded without error."""
    tracker = _tracker()
    assert tracker._should_mark_played("q1", "item1", fully_played=False, is_playing=False) is True
