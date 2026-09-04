"""Regression tests for delayed next-track enqueueing in the player queue stream feeder."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.constants import STREAM_SLOT_WAIT_TIMEOUT
from music_assistant.models.music_provider import MusicProvider, ProviderStreamLimitError


@pytest.mark.parametrize(
    "index_in_buffer",
    [0, 1],
    ids=["aligned-buffer-index", "dynamic-queue-reindexed"],
)
async def test_enqueue_next_item_waits_for_playing_player_update(index_in_buffer: int) -> None:
    """Enqueue the expected next item after an update, even if its buffered index is stale."""
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.logger = MagicMock()

    wait_entered = asyncio.Event()
    release_wait = asyncio.Event()

    @asynccontextmanager
    async def wait_for_player_update(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
        wait_entered.set()
        await release_wait.wait()
        yield

    player_state = SimpleNamespace(
        playback_state=PlaybackState.IDLE,
        active_source="q1",
    )
    player = SimpleNamespace(state=player_state)

    mass = MagicMock()
    mass.players = MagicMock()
    mass.players.wait_for_player_update = MagicMock(side_effect=wait_for_player_update)
    mass.players.get_player = MagicMock(return_value=player)
    mass.players.enqueue_next_media = AsyncMock()
    controller.mass = mass

    current_item = _make_queue_item("q1", "nerin")
    next_item = _make_queue_item("q1", "another-love")
    future_item = _make_queue_item("q1", "future-track")
    queue_items = [current_item, next_item, future_item]
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=len(queue_items),
        state=PlaybackState.IDLE,
        current_index=0,
        index_in_buffer=index_in_buffer,
        current_item=current_item,
    )
    controller._queue_data = {
        "q1": PlayerQueueData(
            queue=queue,
            items=queue_items,
            session_id="session-1",
        )
    }

    controller._enqueue_next_item("q1", next_item)
    enqueue_callback = mass.call_later.call_args.args[1]
    enqueue_task = asyncio.create_task(enqueue_callback(next_item))
    await asyncio.sleep(0)

    assert wait_entered.is_set()
    mass.players.enqueue_next_media.assert_not_awaited()

    player_state.playback_state = PlaybackState.PLAYING
    release_wait.set()
    await enqueue_task

    mass.players.wait_for_player_update.assert_called_once_with(
        "q1",
        attribute_name="playback_state",
        attribute_value=PlaybackState.PLAYING,
    )
    mass.players.enqueue_next_media.assert_awaited_once()
    assert mass.players.enqueue_next_media.await_args.kwargs["player_id"] == "q1"
    assert (
        mass.players.enqueue_next_media.await_args.kwargs["media"].queue_item_id
        == next_item.queue_item_id
    )
    assert controller._queue_data["q1"].next_item_id_enqueued == next_item.queue_item_id


def _make_queue_item(queue_id: str, item_id: str) -> QueueItem:
    """Build a minimal playable queue item."""
    return QueueItem(
        queue_id=queue_id,
        queue_item_id=item_id,
        name=item_id,
        duration=60,
    )


def _controller_with_next_item() -> tuple[PlayerQueuesController, SimpleNamespace, MagicMock]:
    """Build a bare controller whose queue has an unprepared next item."""
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.logger = MagicMock()
    next_item = SimpleNamespace(
        queue_item_id="next",
        media_type=MediaType.TRACK,
        streamdetails=SimpleNamespace(buffer=None),
        name="Next",
        available=True,
    )
    queue = SimpleNamespace(
        current_item=SimpleNamespace(queue_item_id="current"),
        next_item=next_item,
        display_name="Queue",
    )
    controller.get = MagicMock(return_value=queue)  # type: ignore[method-assign]
    controller._queue_data = {
        "queue-1": cast("Any", SimpleNamespace(queue=queue, session_id="session-1"))
    }
    mass = MagicMock()
    controller.mass = mass
    return controller, next_item, mass


async def test_reusing_a_warm_buffer_claims_it_for_the_current_session() -> None:
    """
    A prewarm that is already warm still becomes this session's audio.

    Without the claim the buffer keeps the session that filled it, and that session's stop
    releases audio the current one is relying on.
    """
    controller, next_item, mass = _controller_with_next_item()
    warm = MagicMock()
    warm.is_valid.return_value = True
    next_item.streamdetails = SimpleNamespace(buffer=warm, queue_session_id="session-0")

    controller.prepare_next_audio_buffer("queue-1")

    assert next_item.streamdetails.queue_session_id == "session-1"
    mass.create_task.assert_not_called()


async def test_prepare_next_uses_the_speculative_capacity_budget() -> None:
    """Warming the next track never waits longer for capacity than a speculative attempt may."""
    controller, next_item, mass = _controller_with_next_item()
    mass.streams.audio.get_audio_buffer = AsyncMock()

    controller.prepare_next_audio_buffer("queue-1")
    await mass.create_task.call_args.args[0]()

    mass.streams.audio.get_audio_buffer.assert_awaited_once_with(
        next_item,
        reason="prepare_next",
        capacity_wait_timeout=STREAM_SLOT_WAIT_TIMEOUT,
        allow_provider_match=False,
    )
    assert mass.create_task.call_args.kwargs == {
        "task_id": "prepare_next_audio_buffer_queue-1",
        "abort_existing": True,
    }


@pytest.mark.parametrize(
    ("is_buffering", "expect_cleared"),
    [(True, True), (False, False)],
    ids=["still_filling", "completed"],
)
async def test_an_aborted_prepare_releases_its_half_filled_source(
    is_buffering: bool, expect_cleared: bool
) -> None:
    """Aborting a prewarm must free its slot instead of pinning it until the inactivity sweep."""
    controller, next_item, mass = _controller_with_next_item()
    buffer = MagicMock()
    buffer.is_buffering = is_buffering
    buffer.clear = AsyncMock()
    started = asyncio.Event()

    async def _hang(*_args: object, **_kwargs: object) -> None:
        # the producer is already running and owns a source slot at this point
        next_item.streamdetails.buffer = buffer
        started.set()
        await asyncio.Event().wait()

    mass.streams.audio.get_audio_buffer = _hang

    controller.prepare_next_audio_buffer("queue-1")
    prepare_task = asyncio.create_task(mass.create_task.call_args.args[0]())
    await started.wait()
    prepare_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await prepare_task

    assert buffer.clear.await_count == (1 if expect_cleared else 0)


async def test_prepare_next_gives_up_softly_on_a_capacity_failure() -> None:
    """A speculative source-capacity miss leaves the next item playable."""
    controller, next_item, mass = _controller_with_next_item()
    provider = MagicMock(spec=MusicProvider)
    provider.max_concurrent_streams = 1
    provider.name = "Limited"
    provider.instance_id = "limited--1"
    mass.streams.audio.get_audio_buffer = AsyncMock(
        side_effect=ProviderStreamLimitError(provider, STREAM_SLOT_WAIT_TIMEOUT)
    )

    controller.prepare_next_audio_buffer("queue-1")
    await mass.create_task.call_args.args[0]()

    assert next_item.available


async def test_preload_waits_for_a_current_item_that_is_not_set_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Preload must wait out a briefly-unset current item instead of abandoning the queue.

    Right after a queue replace the player still reports the previous queue's track, so
    index_by_id() finds no index and current_item is transiently None. Giving up there leaves
    nothing enqueued and playback stops at the end of the current track.
    """
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.logger = MagicMock()

    buffered_item = _make_queue_item("q1", "buffered")
    next_item = _make_queue_item("q1", "next")
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=2,
        state=PlaybackState.PLAYING,
        current_index=None,
        index_in_buffer=0,
        current_item=None,
    )
    controller._queue_data = {
        "q1": PlayerQueueData(
            queue=queue,
            items=[buffered_item, next_item],
            session_id="session-1",
        )
    }
    controller.get_item = MagicMock(return_value=buffered_item)  # type: ignore[method-assign]
    controller.load_next_queue_item = AsyncMock(return_value=next_item)  # type: ignore[method-assign]
    controller._enqueue_next_item = MagicMock()  # type: ignore[method-assign]
    mass = MagicMock()
    controller.mass = mass

    # the queue's current item appears a couple of ticks later, once the player stops
    # reporting the replaced queue's track
    slept = 0

    async def _fake_sleep(_delay: float) -> None:
        nonlocal slept
        slept += 1
        if slept == 2:
            queue.current_item = buffered_item

    monkeypatch.setattr(
        "music_assistant.controllers.player_queues.stream_feeder.asyncio.sleep",
        _fake_sleep,
    )

    controller._preload_next_item("q1", "buffered")
    preload = mass.create_task.call_args.args[0]
    await preload(mass.create_task.call_args.args[1])

    controller.load_next_queue_item.assert_awaited_once_with("q1", "buffered")
    controller._enqueue_next_item.assert_called_once_with("q1", next_item)


async def test_preload_stops_once_a_queue_that_had_a_current_item_drains() -> None:
    """A current item that disappears after being seen means the queue drained: stop waiting."""
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.logger = MagicMock()

    buffered_item = _make_queue_item("q1", "buffered")
    other_item = _make_queue_item("q1", "other")
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=2,
        state=PlaybackState.PLAYING,
        current_index=0,
        index_in_buffer=0,
        # a different item than the buffered one, so the wait loop keeps going
        current_item=other_item,
    )
    controller._queue_data = {
        "q1": PlayerQueueData(
            queue=queue,
            items=[buffered_item, other_item],
            session_id="session-1",
        )
    }
    controller.get_item = MagicMock(return_value=buffered_item)  # type: ignore[method-assign]
    controller.load_next_queue_item = AsyncMock()  # type: ignore[method-assign]
    controller._enqueue_next_item = MagicMock()  # type: ignore[method-assign]
    mass = MagicMock()
    controller.mass = mass

    async def _drain_then_sleep(_delay: float) -> None:
        queue.current_item = None

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "music_assistant.controllers.player_queues.stream_feeder.asyncio.sleep",
            _drain_then_sleep,
        )
        controller._preload_next_item("q1", "buffered")
        preload = mass.create_task.call_args.args[0]
        await preload(mass.create_task.call_args.args[1])

    controller.load_next_queue_item.assert_not_awaited()
    controller._enqueue_next_item.assert_not_called()


async def test_preload_gives_up_when_a_current_item_never_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Exhausting the wait without ever seeing a current item means playback never started.

    Falling through to the load would do provider I/O (with the throttler bypassed) and could
    flip items to unavailable on a queue nothing is playing - give up instead.
    """
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.logger = MagicMock()

    buffered_item = _make_queue_item("q1", "buffered")
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=1,
        state=PlaybackState.IDLE,
        current_index=None,
        index_in_buffer=0,
        current_item=None,
    )
    controller._queue_data = {
        "q1": PlayerQueueData(
            queue=queue,
            items=[buffered_item],
            session_id="session-1",
        )
    }
    controller.get_item = MagicMock(return_value=buffered_item)  # type: ignore[method-assign]
    controller.load_next_queue_item = AsyncMock()  # type: ignore[method-assign]
    controller._enqueue_next_item = MagicMock()  # type: ignore[method-assign]
    mass = MagicMock()
    controller.mass = mass

    async def _never_appears(_delay: float) -> None:
        return

    monkeypatch.setattr(
        "music_assistant.controllers.player_queues.stream_feeder.asyncio.sleep",
        _never_appears,
    )

    controller._preload_next_item("q1", "buffered")
    preload = mass.create_task.call_args.args[0]
    await preload(mass.create_task.call_args.args[1])

    controller.load_next_queue_item.assert_not_awaited()
    controller._enqueue_next_item.assert_not_called()
