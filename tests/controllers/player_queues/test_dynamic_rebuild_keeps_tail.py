"""
Tests that rebuilding a playing dynamic queue keeps its items in place while the pool is fetched.

Players read the queue by position (a cloud queue, the stream server), so the upcoming items must
stay where they are while the dynamic sources are fetched, which can take many seconds when a
provider throttles. The new pool only takes the tail's place once it is in.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import QueueOption

from music_assistant.controllers.music.recency import RecencySnapshot
from music_assistant.controllers.player_queues.constants import MANAGED_POOL_TARGET

from .test_play_next_on_dynamic_queue import (
    NOW,
    _controller,
    _dynamic_playing_queue,
    _queue_item,
    _radio,
    _track,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.controllers.player_queues import PlayerQueuesController

UPCOMING = [_track(f"u{i}", artist=f"U{i}") for i in range(10)]
FRESH = [_track(f"d{i}", artist=f"D{i}") for i in range(40)]


def _playing_queue_with_tail(ctrl: PlayerQueuesController) -> list[QueueItem]:
    """Play the first item of a dynamic queue with its next item buffered, and return its items."""
    queue = _dynamic_playing_queue(ctrl, [])
    queue_data = ctrl._queue_data["q1"]
    queue_data.items += [_queue_item("q1", track) for track in UPCOMING]
    queue.items = len(queue_data.items)
    queue.index_in_buffer = 1
    return list(queue_data.items)


def _hold_fetch(
    ctrl: PlayerQueuesController, batch: list[Track]
) -> tuple[asyncio.Event, asyncio.Event]:
    """
    Make the dynamic sources' fetch wait until released, then return the given batch.

    :param ctrl: The controller whose dynamic sources are fetched.
    :param batch: The tracks the fetch returns once released.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    async def _fetch(*_args: object) -> list[Track]:
        started.set()
        await release.wait()
        return batch

    ctrl.get_dynamic_source_tracks = AsyncMock(side_effect=_fetch)  # type: ignore[method-assign]
    return started, release


def _served(ctrl: PlayerQueuesController, count: int) -> list[QueueItem | None]:
    """Return the items a player asking for the first count positions gets now."""
    return [ctrl.get_item("q1", index) for index in range(count)]


def _track_ids(ctrl: PlayerQueuesController) -> list[str]:
    """Return the item ids of the tracks in the queue, in play order."""
    return [item.media_item.item_id for item in ctrl._queue_data["q1"].items if item.media_item]


async def test_an_add_keeps_serving_the_upcoming_items_while_the_pool_is_fetched() -> None:
    """Every upcoming position keeps its item during the fetch; the new tail follows it."""
    random.seed(4)
    ctrl = _controller(RecencySnapshot(now=NOW))
    before = _playing_queue_with_tail(ctrl)
    # the batch offers the old tail's tracks again: the tail being replaced holds none of them back
    started, release = _hold_fetch(ctrl, UPCOMING[1:] + FRESH)

    add = asyncio.create_task(
        ctrl._handle_play_media("q1", _track("added", artist="Add"), QueueOption.ADD)
    )
    await asyncio.wait_for(started.wait(), 1)
    assert _served(ctrl, len(before)) == before

    release.set()
    await add
    queue_data = ctrl._queue_data["q1"]
    assert queue_data.items[:2] == before[:2]
    ids = _track_ids(ctrl)
    assert len(ids) == len(set(ids)), f"a track is queued twice: {ids}"
    assert "added" in ids
    assert {track.item_id for track in UPCOMING[1:]} <= set(ids[2:])
    # sized against the kept head (one unplayed item), not against the tail it replaced
    assert len(queue_data.items) == 2 + MANAGED_POOL_TARGET - 1


async def test_an_add_goes_behind_the_items_the_player_moved_on_to_during_the_fetch() -> None:
    """A track change during the fetch keeps the item the player took, and it is not repeated."""
    random.seed(4)
    ctrl = _controller(RecencySnapshot(now=NOW))
    before = _playing_queue_with_tail(ctrl)
    started, release = _hold_fetch(ctrl, UPCOMING[1:] + FRESH)

    add = asyncio.create_task(
        ctrl._handle_play_media("q1", _track("added", artist="Add"), QueueOption.ADD)
    )
    await asyncio.wait_for(started.wait(), 1)
    # the player buffers the next old tail item while the pool is still being fetched
    ctrl._queue_data["q1"].queue.index_in_buffer = 2
    assert _served(ctrl, len(before)) == before

    release.set()
    await add
    assert ctrl._queue_data["q1"].items[:3] == before[:3]
    ids = _track_ids(ctrl)
    assert len(ids) == len(set(ids)), f"a track is queued twice: {ids}"
    assert "added" in ids[3:]


async def test_an_add_keeps_an_item_the_player_asked_for_before_its_audio_came() -> None:
    """An item with an open stream request stays, though the player has no audio of it yet."""
    random.seed(4)
    ctrl = _controller(RecencySnapshot(now=NOW))
    before = _playing_queue_with_tail(ctrl)
    ctrl._queue_data["q1"].session_id = "s1"
    started, release = _hold_fetch(ctrl, FRESH)

    add = asyncio.create_task(
        ctrl._handle_play_media("q1", _track("added", artist="Add"), QueueOption.ADD)
    )
    await asyncio.wait_for(started.wait(), 1)
    # the player asks for the item after the buffered one; its first audio is still being prepared
    requested = Mock(return_value={before[2].queue_item_id})
    ctrl.mass.streams.requested_item_ids = requested  # type: ignore[method-assign]

    release.set()
    await add
    requested.assert_called_with("q1", "s1")
    assert ctrl._queue_data["q1"].items[:3] == before[:3]
    assert "added" in _track_ids(ctrl)[3:]


async def test_an_add_leaves_out_another_release_of_a_song_the_player_moved_on_to() -> None:
    """A pool track that is another release of a song the player took during the fetch is dropped."""
    random.seed(4)
    ctrl = _controller(RecencySnapshot(now=NOW))
    before = _playing_queue_with_tail(ctrl)
    # the old tail's second item, which the player buffers during the fetch below
    taken = UPCOMING[1]
    other_release = _track("other-release", artist=taken.artists[0].name)
    other_release.name = taken.name
    started, release = _hold_fetch(ctrl, [other_release, *FRESH])

    add = asyncio.create_task(
        ctrl._handle_play_media("q1", _track("added", artist="Add"), QueueOption.ADD)
    )
    await asyncio.wait_for(started.wait(), 1)
    ctrl._queue_data["q1"].queue.index_in_buffer = 2

    release.set()
    await add
    assert ctrl._queue_data["q1"].items[:3] == before[:3]
    assert "other-release" not in _track_ids(ctrl)


async def test_a_replace_keeps_the_old_queue_playing_until_the_pool_is_in() -> None:
    """The old items, their audio and the buffered position stay until the new pool is in."""
    random.seed(4)
    ctrl = _controller(RecencySnapshot(now=NOW))
    before = _playing_queue_with_tail(ctrl)
    cleanup = AsyncMock()
    ctrl._cleanup_queue_audio_data = cleanup  # type: ignore[method-assign]
    play_index = AsyncMock()
    ctrl.play_index = play_index  # type: ignore[method-assign]
    started, release = _hold_fetch(ctrl, FRESH)

    replace = asyncio.create_task(
        ctrl._handle_play_media("q1", _radio("other"), QueueOption.REPLACE)
    )
    await asyncio.wait_for(started.wait(), 1)
    assert _served(ctrl, len(before)) == before
    assert ctrl._queue_data["q1"].queue.index_in_buffer == 1
    cleanup.assert_not_awaited()

    release.set()
    await replace
    cleanup.assert_awaited_once_with("q1")
    ids = _track_ids(ctrl)
    assert len(ids) == MANAGED_POOL_TARGET
    assert set(ids) <= {track.item_id for track in FRESH}
    play_index.assert_awaited_once_with("q1", 0)


async def test_an_add_releases_the_audio_of_the_items_it_drops() -> None:
    """A dropped item's warmed audio is released and its prewarm ended; kept items keep theirs."""
    random.seed(4)
    ctrl = _controller(RecencySnapshot(now=NOW))
    before = _playing_queue_with_tail(ctrl)
    kept_buffer, dropped_buffer = Mock(clear=AsyncMock()), Mock(clear=AsyncMock())
    before[1].streamdetails = Mock(buffer=kept_buffer)
    before[2].streamdetails = Mock(buffer=dropped_buffer)
    # the next old tail item was warmed up while the pool was being fetched
    ctrl._queue_data["q1"].next_item_id_preparing = before[2].queue_item_id
    cancel_task = Mock()
    ctrl.mass.cancel_task = cancel_task  # type: ignore[method-assign]
    _hold_fetch(ctrl, FRESH)[1].set()

    await ctrl._handle_play_media("q1", _track("added", artist="Add"), QueueOption.ADD)

    assert before[2] not in ctrl._queue_data["q1"].items
    dropped_buffer.clear.assert_awaited_once()
    assert before[2].streamdetails.buffer is None
    cancel_task.assert_any_call("prepare_next_audio_buffer_q1")
    kept_buffer.clear.assert_not_awaited()
    assert before[1].streamdetails.buffer is kept_buffer


async def test_the_next_item_is_the_one_that_replaced_an_item_dropped_while_it_loaded() -> None:
    """An item the queue drops while it loads as the next one gives way to its replacement."""
    ctrl = _controller(RecencySnapshot(now=NOW))
    before = _playing_queue_with_tail(ctrl)
    queue_data = ctrl._queue_data["q1"]
    replacement = _queue_item("q1", FRESH[0])
    loaded: list[str] = []

    async def _load(queue_item: QueueItem, **_kwargs: object) -> None:
        loaded.append(queue_item.queue_item_id)
        if queue_item is before[1]:
            # a refill swaps the upcoming items while this one loads
            ctrl.update_items("q1", [before[0], replacement])

    ctrl._load_item = AsyncMock(side_effect=_load)  # type: ignore[method-assign]
    ctrl._get_resume_position = AsyncMock(return_value=0)  # type: ignore[method-assign]
    queue_data.queue.index_in_buffer = 0

    next_item = await ctrl.load_next_queue_item("q1", before[0].queue_item_id)

    assert next_item is replacement
    assert loaded == [before[1].queue_item_id, replacement.queue_item_id]
