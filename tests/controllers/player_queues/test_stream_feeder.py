"""Regression tests for delayed next-track enqueueing in the player queue stream feeder."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
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


def _controller_with_live_source_queue() -> tuple[PlayerQueuesController, list[Any], MagicMock]:
    """Build a bare controller whose queue holds two tracks on one provider instance."""
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.logger = MagicMock()
    items = [_live_source_item("playing", "aaa"), _live_source_item("upcoming", "bbb")]
    queue = SimpleNamespace(current_index=0, display_name="Queue")
    controller.get = MagicMock(return_value=queue)  # type: ignore[method-assign]
    controller.get_item = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda _queue_id, index: items[index] if index < len(items) else None
    )
    controller._queue_data = {
        "queue-1": cast("Any", SimpleNamespace(queue=queue, session_id="session-1"))
    }
    mass = MagicMock()
    controller.mass = mass
    return controller, items, mass


def _live_source_item(queue_item_id: str, provider_item_id: str) -> Any:
    """Build a queue item stand-in mapped to the live-source provider instance."""
    media_item = SimpleNamespace(
        provider="spotify--a", item_id=provider_item_id, provider_mappings=[]
    )
    return SimpleNamespace(
        queue_item_id=queue_item_id,
        media_type=MediaType.TRACK,
        media_item=media_item,
        streamdetails=None,
        name=queue_item_id,
    )


async def test_a_live_source_gets_the_buffer_of_the_item_it_is_producing() -> None:
    """The buffer is opened for the upcoming item the produced audio actually belongs to."""
    controller, items, mass = _controller_with_live_source_queue()
    streamdetails = SimpleNamespace(provider="spotify--a", buffer=None, queue_session_id=None)
    mass.streams.audio.get_stream_details = AsyncMock(return_value=streamdetails)
    fill = MagicMock()
    with patch.object(AudioBuffer, "open_provider_fill", return_value=fill) as open_fill:
        result = await controller.open_provider_audio_fill("queue-1", "spotify--a", "bbb")

    assert result is fill
    assert items[1].streamdetails is streamdetails
    # the buffer becomes this session's audio, so its stop releases it
    assert streamdetails.queue_session_id == "session-1"
    open_fill.assert_called_once_with(mass, streamdetails, reason="source_live")


async def test_a_provider_mapping_locates_the_item_the_queue_reordered() -> None:
    """An item is found by its provider mapping, not by the position it was fed at."""
    controller, items, mass = _controller_with_live_source_queue()
    items[1].media_item.provider = "library"
    items[1].media_item.item_id = "42"
    items[1].media_item.provider_mappings = [
        SimpleNamespace(provider_instance="spotify--a", item_id="bbb")
    ]
    streamdetails = SimpleNamespace(provider="spotify--a", buffer=None, queue_session_id=None)
    mass.streams.audio.get_stream_details = AsyncMock(return_value=streamdetails)
    with patch.object(AudioBuffer, "open_provider_fill", return_value=MagicMock()):
        assert await controller.open_provider_audio_fill("queue-1", "spotify--a", "bbb") is not None


async def test_no_buffer_for_audio_the_queue_has_no_item_for() -> None:
    """Audio for an item that is not (or no longer) upcoming is not buffered."""
    controller, _items, mass = _controller_with_live_source_queue()
    mass.streams.audio.get_stream_details = AsyncMock()
    with patch.object(AudioBuffer, "open_provider_fill") as open_fill:
        assert await controller.open_provider_audio_fill("queue-1", "spotify--a", "zzz") is None
    open_fill.assert_not_called()
    mass.streams.audio.get_stream_details.assert_not_awaited()


async def test_no_buffer_once_the_queue_resolved_the_item_elsewhere() -> None:
    """A queue that settled on another provider for the item is not handed this audio."""
    controller, items, _mass = _controller_with_live_source_queue()
    items[1].streamdetails = SimpleNamespace(provider="tidal--b", buffer=None)
    with patch.object(AudioBuffer, "open_provider_fill") as open_fill:
        assert await controller.open_provider_audio_fill("queue-1", "spotify--a", "bbb") is None
    open_fill.assert_not_called()


async def test_no_second_buffer_for_an_item_that_already_has_one() -> None:
    """A buffer another request is already filling must not be split with a second one."""
    controller, items, _mass = _controller_with_live_source_queue()
    warm = MagicMock()
    warm.has_error = False
    warm.is_valid.return_value = True
    items[1].streamdetails = SimpleNamespace(provider="spotify--a", buffer=warm)
    with patch.object(AudioBuffer, "open_provider_fill") as open_fill:
        assert await controller.open_provider_audio_fill("queue-1", "spotify--a", "bbb") is None
    open_fill.assert_not_called()


async def test_a_failed_buffer_is_replaced_rather_than_left_holding_the_item() -> None:
    """A buffer whose fill failed is no reason to leave the live audio nowhere to go."""
    controller, items, _mass = _controller_with_live_source_queue()
    broken = MagicMock()
    broken.has_error = True
    broken.is_valid.return_value = True
    broken.clear = AsyncMock()
    items[1].streamdetails = SimpleNamespace(
        provider="spotify--a", buffer=broken, queue_session_id=None
    )
    fill = MagicMock()
    with patch.object(AudioBuffer, "open_provider_fill", return_value=fill):
        assert await controller.open_provider_audio_fill("queue-1", "spotify--a", "bbb") is fill
    # the superseded buffer is cleared, not orphaned with its chunks and monitor
    broken.clear.assert_awaited_once()


async def test_a_stopped_queue_declines_live_audio() -> None:
    """
    A queue whose playback session ended must not accept a live fill.

    The stop just released everything this would re-arm, and nothing will read
    the buffer - the source daemon would linger holding the account's stream.
    """
    controller, _items, _mass = _controller_with_live_source_queue()
    controller._queue_data["queue-1"].session_id = None
    with patch.object(AudioBuffer, "open_provider_fill") as open_fill:
        assert await controller.open_provider_audio_fill("queue-1", "spotify--a", "bbb") is None
    open_fill.assert_not_called()


async def test_a_duplicate_of_the_playing_track_is_declined_not_taken_over() -> None:
    """The playing occurrence's audio belongs to its own stream, whatever its buffer's state."""
    controller, items, _mass = _controller_with_live_source_queue()
    # the playing item IS the announced item (a duplicate up next), with a buffer
    # whose head has been evicted - taking it over would cut playback mid-track
    stale = MagicMock()
    stale.has_error = False
    stale.is_valid.return_value = False
    items[0].streamdetails = SimpleNamespace(
        provider="spotify--a", buffer=stale, queue_session_id=None
    )
    with patch.object(AudioBuffer, "open_provider_fill") as open_fill:
        assert await controller.open_provider_audio_fill("queue-1", "spotify--a", "aaa") is None
    open_fill.assert_not_called()


async def test_unresolvable_stream_details_leave_the_live_audio_unbuffered() -> None:
    """An item whose stream details cannot be resolved gets no buffer, and no exception."""
    controller, _items, mass = _controller_with_live_source_queue()
    mass.streams.audio.get_stream_details = AsyncMock(side_effect=MediaNotFoundError("gone"))
    with patch.object(AudioBuffer, "open_provider_fill") as open_fill:
        assert await controller.open_provider_audio_fill("queue-1", "spotify--a", "bbb") is None
    open_fill.assert_not_called()
