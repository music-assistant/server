"""
Tests that rebuilding a playing dynamic queue keeps its items in place while the pool is fetched.

Players read the queue by position (a cloud queue, the stream server), so the upcoming items must
stay where they are while the dynamic sources are fetched, which can take many seconds when a
provider throttles. The new pool only takes the tail's place once it is in. The audio of an item
the rebuild drops goes with it, unless a stream still reads it or decided to play it.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncGenerator
from functools import partial
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import QueueOption

import music_assistant.controllers.streams.audio as audio_mod
from music_assistant.controllers.music.recency import RecencySnapshot
from music_assistant.controllers.player_queues.constants import MANAGED_POOL_TARGET
from tests.controllers.streams.stream_slot_rig import (
    QUEUE_ID,
    SESSION_ID,
    SlotRig,
    allow_commands,
    realtime_capture,
    source,
    track_item,
)

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
    from music_assistant.controllers.streams.audio_buffer import AudioBuffer

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


def _playing_with_the_next_track_fetched(rig: SlotRig) -> list[QueueItem]:
    """Play the first of four tracks on the rig, with the second one fetched by the player."""
    tracks = rig.play("track 0", "track 1", "track 2", "track 3")
    queue = rig.queues._queue_data[QUEUE_ID].queue
    queue.is_dynamic = True
    queue.index_in_buffer = 1
    return tracks


async def _rebuild(rig: SlotRig) -> None:
    """Rebuild the upcoming tail of the rig's queue from a fresh pool, as an add does."""
    pool = [track_item(f"fresh {index}").media_item for index in range(3)]
    rig.queues._managed_pool = MagicMock(fill=AsyncMock(return_value=pool))
    allow_commands(rig)
    await rig.queues._enter_dynamic_mode(QUEUE_ID, QueueOption.ADD)
    await rig.settle()


async def _released(rig: SlotRig, buffer: AudioBuffer) -> bool:
    """Return whether a buffer gave its audio up and its source its stream slot."""
    if (release := buffer._release_task) is not None:
        # a release that was started runs to its end first
        await release
    return (
        buffer.cancelled
        and buffer.seconds_available == 0
        and not rig.fills(buffer)
        and rig.slot_free()
    )


async def test_an_add_releases_the_audio_of_the_items_it_drops(rig: SlotRig) -> None:
    """A dropped item's warmed audio is released and its prewarm ended; kept items keep theirs."""
    tracks = _playing_with_the_next_track_fetched(rig)
    rig.sources["track 1"] = partial(source, 5)
    kept = await rig.ask(tracks[1])
    assert kept is not None
    await rig.delivered(tracks[1])
    opened = asyncio.Event()

    async def _slow_to_start() -> AsyncGenerator[bytes]:
        opened.set()
        await asyncio.Event().wait()
        yield b""

    # the next old tail item is still being warmed up when the pool comes in
    rig.sources["track 2"] = _slow_to_start
    prewarm = rig.queues.prepare_next_audio_buffer(QUEUE_ID, tracks[1].queue_item_id)
    assert prewarm is not None
    await asyncio.wait_for(opened.wait(), 1)
    dropped = rig.buffer(tracks[2])

    await _rebuild(rig)

    assert tracks[2] not in rig.queues._queue_data[QUEUE_ID].items
    assert await _released(rig, dropped)
    assert prewarm.done()
    assert rig.buffer(tracks[1]) is kept
    assert not kept.cancelled
    assert kept.seconds_available == 5


@pytest.mark.parametrize("complete", [False, True], ids=["still filling", "fully loaded"])
async def test_an_add_releases_the_audio_of_a_dropped_item_nobody_reads(
    rig: SlotRig, complete: bool
) -> None:
    """Audio that was prepared for a dropped item goes at the rebuild, loaded or not."""
    tracks = _playing_with_the_next_track_fetched(rig)
    rig.sources["track 2"] = partial(source, 5) if complete else realtime_capture
    prepared = await rig.prepare_next(tracks[1])
    if complete:
        await rig.delivered(tracks[2])
        assert prepared.seconds_available == 5

    await _rebuild(rig)

    assert tracks[2] not in rig.queues._queue_data[QUEUE_ID].items
    assert await _released(rig, prepared)


async def test_an_add_spares_the_audio_of_a_dropped_item_a_stream_still_reads(
    rig: SlotRig,
) -> None:
    """A stream that reads a track the rebuild drops is not cut off; the audio goes when it ends."""
    tracks = _playing_with_the_next_track_fetched(rig)
    prepared = await rig.prepare_next(tracks[1])
    reader = prepared.get_raw_stream()
    await anext(reader)

    await _rebuild(rig)

    assert tracks[2] not in rig.queues._queue_data[QUEUE_ID].items
    assert not prepared.cancelled
    assert rig.fills(prepared)
    await anext(reader)
    await reader.aclose()
    await rig.settle()
    assert await _released(rig, prepared)


async def test_an_add_spares_the_audio_of_a_dropped_item_a_stream_decided_to_play(
    rig: SlotRig,
) -> None:
    """A track a stream took for its next transition keeps its audio until that stream lets go."""
    tracks = _playing_with_the_next_track_fetched(rig)
    prepared = await rig.prepare_next(tracks[1])
    claim = audio_mod._ItemClaim("flow stream of Queue A", SESSION_ID)
    rig.audio._claim_item(claim, tracks[2])

    await _rebuild(rig)

    assert tracks[2] not in rig.queues._queue_data[QUEUE_ID].items
    assert not prepared.cancelled
    assert rig.fills(prepared)
    rig.audio._claim_item(claim, None)
    await rig.settle()
    assert await _released(rig, prepared)


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
