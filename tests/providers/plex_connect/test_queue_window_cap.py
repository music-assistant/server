"""Tests for anchoring the play queue cap on the selected item instead of the head."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from music_assistant.providers.plex_connect import queue_commands
from music_assistant.providers.plex_connect.queue_commands import (
    MAX_QUEUE_ITEMS,
    QueueCommandsMixin,
)


class _QueueHandler(QueueCommandsMixin):
    """QueueCommandsMixin with the host-class attributes mocked."""

    def __init__(self) -> None:
        self.provider = Mock()
        self.provider._plex_server = Mock()
        self._ma_player_id = "player1"
        self._updating_from_plex = False
        self.play_queue_id = None
        self.play_queue_item_ids: dict[int, int] = {}


def _make_items(first_track: int, last_track: int) -> list[SimpleNamespace]:
    """Build fake queue items; track number N gets playQueueItemID 1000+N."""
    return [SimpleNamespace(playQueueItemID=1000 + n) for n in range(first_track, last_track + 1)]


def _make_playqueue(
    first_track: int,
    last_track: int,
    selected_track: int | None,
    selected_offset: int | None,
    total_count: int | None,
) -> Any:
    """Build a fake windowed PlayQueue with a real dict for the items patch."""
    return SimpleNamespace(
        items=_make_items(first_track, last_track),
        playQueueSelectedItemID=1000 + selected_track if selected_track is not None else None,
        playQueueSelectedItemOffset=selected_offset,
        playQueueTotalCount=total_count,
    )


class _FakePlayQueue:
    """Stand-in for plexapi's PlayQueue.get, returning prebuilt fake responses."""

    def __init__(self, initial: Any, pages: dict[int, Any] | None = None) -> None:
        self.initial = initial
        self.pages = pages or {}
        self.get_calls: list[dict[str, Any]] = []

    def get(self, *_args: Any, **kwargs: Any) -> Any:
        self.get_calls.append(kwargs)
        if "center" in kwargs:
            return self.pages.get(kwargs["center"])
        return self.initial


async def _fetch(monkeypatch: pytest.MonkeyPatch, fake: _FakePlayQueue) -> Any:
    handler = _QueueHandler()
    monkeypatch.setattr(queue_commands, "PlayQueue", fake)
    return await handler._fetch_full_play_queue("123")


@pytest.mark.asyncio
async def test_selection_past_cap_keeps_selected_and_fills_from_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting track 101 of 142 keeps tracks 1..58 and 101..142, selected at index 58."""
    playqueue = _make_playqueue(1, 142, selected_track=101, selected_offset=100, total_count=142)
    fake = _FakePlayQueue(initial=playqueue)

    result = await _fetch(monkeypatch, fake)

    assert len(result.items) == MAX_QUEUE_ITEMS
    handler = _QueueHandler()
    index = handler._selected_item_index(result)
    assert result.items[index].playQueueItemID == 1000 + 101
    assert index == 58
    kept_tracks = [item.playQueueItemID - 1000 for item in result.items]
    assert kept_tracks == [*range(1, 59), *range(101, 143)]


@pytest.mark.asyncio
async def test_last_track_selected_keeps_full_tail_and_wraps_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting the last track of 142 keeps that track plus 99 head tracks."""
    playqueue = _make_playqueue(1, 142, selected_track=142, selected_offset=141, total_count=142)
    fake = _FakePlayQueue(initial=playqueue)

    result = await _fetch(monkeypatch, fake)

    assert len(result.items) == MAX_QUEUE_ITEMS
    handler = _QueueHandler()
    index = handler._selected_item_index(result)
    assert index == 99
    assert result.items[index].playQueueItemID == 1000 + 142
    kept_tracks = [item.playQueueItemID - 1000 for item in result.items]
    assert kept_tracks == [*range(1, 100), 142]


@pytest.mark.asyncio
async def test_selection_early_prefers_upcoming_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting track 5 of 142 keeps tracks 5..104, preferring upcoming tracks."""
    playqueue = _make_playqueue(1, 142, selected_track=5, selected_offset=4, total_count=142)
    fake = _FakePlayQueue(initial=playqueue)

    result = await _fetch(monkeypatch, fake)

    assert len(result.items) == MAX_QUEUE_ITEMS
    handler = _QueueHandler()
    index = handler._selected_item_index(result)
    assert index == 0
    kept_tracks = [item.playQueueItemID - 1000 for item in result.items]
    assert kept_tracks == list(range(5, 105))


@pytest.mark.asyncio
async def test_off_head_window_does_not_fill_from_stale_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A window not starting at the queue head must not be treated as head-fill material.

    The initial response here covers tracks 150..400 (not the true head), so
    all_items[0] is track 150, not the real head - filling from it would be wrong.
    """
    playqueue = _make_playqueue(150, 400, selected_track=350, selected_offset=349, total_count=400)
    fake = _FakePlayQueue(initial=playqueue)

    result = await _fetch(monkeypatch, fake)

    handler = _QueueHandler()
    index = handler._selected_item_index(result)
    assert result.items[index].playQueueItemID == 1000 + 350
    kept_tracks = [item.playQueueItemID - 1000 for item in result.items]
    # No head-fill: nothing before the selected track is kept.
    assert min(kept_tracks) == 350


@pytest.mark.asyncio
async def test_track_radio_behaves_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """Track-radio queues (no selected ID, no total count) keep the head-anchored cap."""
    playqueue = _make_playqueue(1, 150, selected_track=None, selected_offset=None, total_count=None)
    fake = _FakePlayQueue(initial=playqueue)

    result = await _fetch(monkeypatch, fake)

    assert len(result.items) == MAX_QUEUE_ITEMS
    kept_tracks = [item.playQueueItemID - 1000 for item in result.items]
    assert kept_tracks == list(range(1, 101))


@pytest.mark.asyncio
async def test_track_radio_untouched_when_under_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A track-radio queue with fewer than MAX_QUEUE_ITEMS items is left untouched."""
    playqueue = _make_playqueue(1, 10, selected_track=None, selected_offset=None, total_count=None)
    fake = _FakePlayQueue(initial=playqueue)

    result = await _fetch(monkeypatch, fake)

    assert len(result.items) == 10


@pytest.mark.asyncio
async def test_pagination_fetches_forward_pages_past_the_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When the initial window holds too few items after the anchor, fetch more pages.

    The initial response covers tracks 1..90 with selection at track 60 (30 items after
    the anchor); a second page centered on the last item's ID supplies the rest.
    """
    initial = _make_playqueue(1, 90, selected_track=60, selected_offset=59, total_count=142)
    next_page = SimpleNamespace(items=_make_items(91, 142))
    fake = _FakePlayQueue(initial=initial, pages={1000 + 90: next_page})

    result = await _fetch(monkeypatch, fake)

    assert any("center" in call for call in fake.get_calls)
    assert len(result.items) == MAX_QUEUE_ITEMS
    handler = _QueueHandler()
    index = handler._selected_item_index(result)
    assert result.items[index].playQueueItemID == 1000 + 60
    kept_tracks = [item.playQueueItemID - 1000 for item in result.items]
    # Paginated tracks (91..142) must be present in the kept window.
    assert set(range(91, 143)).issubset(set(kept_tracks))
