"""Tests for de-duplicating the doubled end-of-queue play report."""

from __future__ import annotations

from music_assistant.controllers.player_queues import PlayerQueuesController


def _controller() -> PlayerQueuesController:
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl._last_counted_play = {}
    return ctrl


def test_completed_play_counts_once() -> None:
    """The doubled end-of-queue completion report is only counted once."""
    ctrl = _controller()
    # first completion report -> counted
    assert ctrl._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is True
    # second (duplicate) completion report for the same item -> skipped
    assert ctrl._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is False


def test_repeat_counts_again_after_restart() -> None:
    """A looped track is counted again once it restarts."""
    ctrl = _controller()
    assert ctrl._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is True
    # the track restarts (a not-fully-played report) -> guard resets
    assert ctrl._should_mark_played("q1", "item1", fully_played=False, is_playing=True) is True
    # the next completion is counted again
    assert ctrl._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is True


def test_now_playing_reports_are_never_deduplicated() -> None:
    """Now-playing / partial reports are always forwarded."""
    ctrl = _controller()
    assert ctrl._should_mark_played("q1", "item1", fully_played=False, is_playing=True) is True
    assert ctrl._should_mark_played("q1", "item1", fully_played=False, is_playing=True) is True


def test_distinct_items_each_count() -> None:
    """Two different queue items are counted independently."""
    ctrl = _controller()
    assert ctrl._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is True
    assert ctrl._should_mark_played("q1", "item2", fully_played=True, is_playing=False) is True


def test_partial_report_for_other_item_does_not_reset_guard() -> None:
    """A not-fully-played report for a different item must not re-arm another item's guard."""
    ctrl = _controller()
    assert ctrl._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is True
    # a partial report for a *different* item must not reset item1's guard
    assert ctrl._should_mark_played("q1", "item2", fully_played=False, is_playing=True) is True
    # so the duplicate completion of item1 is still skipped
    assert ctrl._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is False


def test_queues_are_independent() -> None:
    """Each queue tracks its own last-counted play."""
    ctrl = _controller()
    assert ctrl._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is True
    # the same item id on a different queue is counted independently
    assert ctrl._should_mark_played("q2", "item1", fully_played=True, is_playing=False) is True
    # and each queue's own duplicate is skipped
    assert ctrl._should_mark_played("q1", "item1", fully_played=True, is_playing=False) is False
    assert ctrl._should_mark_played("q2", "item1", fully_played=True, is_playing=False) is False


def test_not_fully_played_first_report_is_forwarded() -> None:
    """A not-fully-played report with nothing counted yet is forwarded without error."""
    ctrl = _controller()
    assert ctrl._should_mark_played("q1", "item1", fully_played=False, is_playing=False) is True
