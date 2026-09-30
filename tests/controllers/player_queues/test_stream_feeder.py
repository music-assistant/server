"""Regression tests for delayed next-track enqueueing in the player queue stream feeder."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState, RepeatMode
from music_assistant_models.errors import QueueEmpty
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.constants import STREAM_SLOT_WAIT_TIMEOUT
from music_assistant.models.music_provider import MusicProvider, ProviderStreamLimitError

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


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
    """Build a bare controller whose streamed item is followed by an unprepared next item."""
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.logger = MagicMock()
    current_item = SimpleNamespace(
        queue_item_id="current",
        media_type=MediaType.TRACK,
        streamdetails=None,
        name="Current",
        available=True,
    )
    next_item = SimpleNamespace(
        queue_item_id="next",
        media_type=MediaType.TRACK,
        streamdetails=SimpleNamespace(buffer=None),
        name="Next",
        available=True,
    )
    queue = SimpleNamespace(
        current_item=current_item,
        next_item=next_item,
        current_index=0,
        index_in_buffer=0,
        repeat_mode=RepeatMode.OFF,
        display_name="Queue",
    )
    controller.get = MagicMock(return_value=queue)  # type: ignore[method-assign]
    controller._queue_data = {
        "queue-1": cast(
            "Any",
            SimpleNamespace(
                queue=queue,
                items=[current_item, next_item],
                session_id="session-1",
                next_item_id_preparing=None,
                last_served_item_id=None,
            ),
        )
    }

    async def _load_next(queue_id: str, item_id: str) -> Any:
        if (item := controller.get_next_item(queue_id, item_id)) is None:
            raise QueueEmpty
        return item

    controller.load_next_queue_item = AsyncMock(side_effect=_load_next)  # type: ignore[method-assign]
    mass = MagicMock()
    controller.mass = mass
    return controller, next_item, mass


def _streamed_item(controller: PlayerQueuesController) -> SimpleNamespace:
    """Return the item whose stream precedes the next item."""
    return cast("SimpleNamespace", controller._queue_data["queue-1"].items[0])


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

    controller.prepare_next_audio_buffer("queue-1", "current")

    assert next_item.streamdetails.queue_session_id == "session-1"
    mass.create_task.assert_not_called()


async def test_prepare_next_uses_the_speculative_capacity_budget() -> None:
    """Warming the next track never waits longer for capacity than a speculative attempt may."""
    controller, next_item, mass = _controller_with_next_item()
    mass.streams.audio.get_audio_buffer = AsyncMock()

    controller.prepare_next_audio_buffer("queue-1", "current")
    await mass.create_task.call_args.args[0]

    mass.streams.audio.get_audio_buffer.assert_awaited_once_with(
        next_item,
        reason="prepare_next",
        capacity_wait_timeout=STREAM_SLOT_WAIT_TIMEOUT,
        allow_provider_match=False,
        stop_paused_queues=False,
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

    controller.prepare_next_audio_buffer("queue-1", "current")
    prepare_task = asyncio.create_task(mass.create_task.call_args.args[0])
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

    controller.prepare_next_audio_buffer("queue-1", "current")
    await mass.create_task.call_args.args[0]

    assert next_item.available


async def test_prepare_next_defers_while_the_streamed_item_holds_the_only_source_slot() -> None:
    """A preload for the same single-slot source waits for the boundary, not for a timeout."""
    controller, next_item, mass = _controller_with_next_item()
    next_item.streamdetails = SimpleNamespace(buffer=None, provider="limited--1")
    playing = MagicMock()
    playing.eof = False
    _streamed_item(controller).streamdetails = SimpleNamespace(
        provider="limited--1", buffer=playing, is_realtime=True
    )
    # the audible item trails the stream and holds no source
    cast("Any", controller.get("queue-1")).current_item = SimpleNamespace(streamdetails=None)
    provider = MagicMock(spec=MusicProvider)
    provider.max_concurrent_streams = 1
    provider.has_available_stream_slot = False
    mass.get_provider = MagicMock(return_value=provider)
    mass.streams.audio.get_audio_buffer = AsyncMock()

    controller.prepare_next_audio_buffer("queue-1", "current")
    await mass.create_task.call_args.args[0]

    mass.streams.audio.get_audio_buffer.assert_not_awaited()


async def test_prepare_next_only_defers_for_a_realtime_source() -> None:
    """A source that fills ahead of playback frees its slot in time and gets no deferral."""
    controller, next_item, mass = _controller_with_next_item()
    next_item.streamdetails = SimpleNamespace(buffer=None, provider="limited--1")
    playing = MagicMock()
    playing.eof = False
    _streamed_item(controller).streamdetails = SimpleNamespace(
        provider="limited--1", buffer=playing, is_realtime=False
    )
    provider = MagicMock(spec=MusicProvider)
    provider.max_concurrent_streams = 1
    provider.has_available_stream_slot = False
    mass.get_provider = MagicMock(return_value=provider)
    mass.streams.audio.get_audio_buffer = AsyncMock()

    controller.prepare_next_audio_buffer("queue-1", "current")
    await mass.create_task.call_args.args[0]

    mass.streams.audio.get_audio_buffer.assert_awaited_once()


async def test_prepare_next_runs_once_the_streamed_item_released_the_slot() -> None:
    """The same preload goes ahead when the streamed item's source has finished."""
    controller, next_item, mass = _controller_with_next_item()
    next_item.streamdetails = SimpleNamespace(buffer=None, provider="limited--1")
    finished = MagicMock()
    finished.eof = True
    _streamed_item(controller).streamdetails = SimpleNamespace(
        provider="limited--1", buffer=finished, is_realtime=True
    )
    provider = MagicMock(spec=MusicProvider)
    provider.max_concurrent_streams = 1
    provider.has_available_stream_slot = False
    mass.get_provider = MagicMock(return_value=provider)
    mass.streams.audio.get_audio_buffer = AsyncMock()

    controller.prepare_next_audio_buffer("queue-1", "current")
    await mass.create_task.call_args.args[0]

    mass.streams.audio.get_audio_buffer.assert_awaited_once()


async def test_prepare_next_skips_an_item_that_left_the_queue_while_it_was_fetched() -> None:
    """
    A replace that lands while the stream details are still being fetched ends the prewarm.

    The item the prewarm was scheduled for is no longer on the queue, so warming its audio
    would decode a track nobody will play and pin a source slot on an orphaned buffer.
    """
    controller, next_item, mass = _controller_with_next_item()
    next_item.streamdetails = None

    async def _replace_queue_meanwhile(*_args: object) -> SimpleNamespace:
        controller._queue_data["queue-1"].items.clear()
        next_item.streamdetails = SimpleNamespace(buffer=None)
        return next_item

    controller.load_next_queue_item = _replace_queue_meanwhile  # type: ignore[method-assign, assignment]
    mass.streams.audio.get_audio_buffer = AsyncMock()

    controller.prepare_next_audio_buffer("queue-1", "current")
    await mass.create_task.call_args.args[0]

    mass.streams.audio.get_audio_buffer.assert_not_awaited()


async def test_prepare_next_releases_a_buffer_whose_item_left_the_queue_mid_fill() -> None:
    """
    A removal that lands while the buffer fills still releases the warmed audio.

    Replace-next and delete do not cancel the prewarm, so without the check after the fill
    the finished buffer stays attached to an item no cleanup reaches any more, holding its
    audio until the inactivity sweep.
    """
    controller, next_item, mass = _controller_with_next_item()
    buffer = MagicMock()
    buffer.clear = AsyncMock()

    async def _remove_item_meanwhile(*_args: object, **_kwargs: object) -> MagicMock:
        controller._queue_data["queue-1"].items.clear()
        next_item.streamdetails.buffer = buffer
        return buffer

    mass.streams.audio.get_audio_buffer = _remove_item_meanwhile

    controller.prepare_next_audio_buffer("queue-1", "current")
    await mass.create_task.call_args.args[0]

    buffer.clear.assert_awaited_once()
    assert next_item.streamdetails.buffer is None


async def test_prepare_next_leaves_the_buffer_of_an_item_still_on_the_queue() -> None:
    """A fill that raced nothing keeps its buffer attached for the upcoming track."""
    controller, next_item, mass = _controller_with_next_item()
    buffer = MagicMock()
    buffer.clear = AsyncMock()

    async def _fill(*_args: object, **_kwargs: object) -> MagicMock:
        next_item.streamdetails.buffer = buffer
        return buffer

    mass.streams.audio.get_audio_buffer = _fill

    controller.prepare_next_audio_buffer("queue-1", "current")
    await mass.create_task.call_args.args[0]

    buffer.clear.assert_not_awaited()
    assert next_item.streamdetails.buffer is buffer


async def test_prepare_next_creates_no_buffer_once_the_session_ended() -> None:
    """
    A stop that lands while the next item is resolved ends the prewarm.

    The stop already released its session's audio, so a buffer warmed now would stay attached
    to a stopped queue that nothing cleans up any more.
    """
    controller, next_item, mass = _controller_with_next_item()

    async def _stop_meanwhile(*_args: object) -> SimpleNamespace:
        controller._queue_data["queue-1"].session_id = None
        return next_item

    controller.load_next_queue_item = _stop_meanwhile  # type: ignore[method-assign, assignment]
    mass.streams.audio.get_audio_buffer = AsyncMock()

    controller.prepare_next_audio_buffer("queue-1", "current")
    await mass.create_task.call_args.args[0]

    mass.streams.audio.get_audio_buffer.assert_not_awaited()


async def test_prepare_next_releases_a_buffer_that_filled_after_the_session_ended() -> None:
    """
    A stop that lands while the buffer fills still releases the warmed audio.

    The stop's cleanup has already run by the time the fill finishes, so the buffer is
    detached and released here instead of holding its source on a stopped queue.
    """
    controller, next_item, mass = _controller_with_next_item()
    buffer = MagicMock()
    detached_on_clear: list[bool] = []
    buffer.clear = AsyncMock(
        side_effect=lambda: detached_on_clear.append(next_item.streamdetails.buffer is None)
    )

    async def _stop_meanwhile(*_args: object, **_kwargs: object) -> MagicMock:
        controller._queue_data["queue-1"].session_id = None
        next_item.streamdetails.buffer = buffer
        return buffer

    mass.streams.audio.get_audio_buffer = _stop_meanwhile

    controller.prepare_next_audio_buffer("queue-1", "current")
    await mass.create_task.call_args.args[0]

    buffer.clear.assert_awaited_once()
    assert next_item.streamdetails.buffer is None
    assert detached_on_clear == [True]


async def test_prepare_next_keeps_the_buffer_when_the_session_rotated_mid_fill() -> None:
    """A skip that starts a new session while the buffer fills keeps the audio for that session."""
    controller, next_item, mass = _controller_with_next_item()
    buffer = MagicMock()
    buffer.clear = AsyncMock()

    async def _skip_meanwhile(*_args: object, **_kwargs: object) -> MagicMock:
        controller._queue_data["queue-1"].session_id = "session-2"
        next_item.streamdetails.buffer = buffer
        return buffer

    mass.streams.audio.get_audio_buffer = _skip_meanwhile

    controller.prepare_next_audio_buffer("queue-1", "current")
    await mass.create_task.call_args.args[0]

    buffer.clear.assert_not_awaited()
    assert next_item.streamdetails.buffer is buffer


async def test_prepare_next_follows_the_streamed_item_not_the_audible_one() -> None:
    """
    The item after the streamed one is prepared while the player still plays an earlier one.

    A player that reads a whole track ahead reports an audible item that trails the stream,
    so the queue's next item is the streamed item itself.
    """
    controller, next_item, mass = _controller_with_next_item()
    audible_item = SimpleNamespace(
        queue_item_id="audible",
        media_type=MediaType.TRACK,
        streamdetails=None,
        name="Audible",
        available=True,
    )
    queue_data = controller._queue_data["queue-1"]
    streamed_item = _streamed_item(controller)
    queue_data.items.insert(0, cast("Any", audible_item))
    queue = cast("Any", queue_data.queue)
    queue.current_item = audible_item
    queue.next_item = streamed_item
    mass.streams.audio.get_audio_buffer = AsyncMock()

    controller.prepare_next_audio_buffer("queue-1", "current")
    await mass.create_task.call_args.args[0]

    cast("AsyncMock", controller.load_next_queue_item).assert_awaited_once_with(
        "queue-1", "current"
    )
    mass.streams.audio.get_audio_buffer.assert_awaited_once()
    assert mass.streams.audio.get_audio_buffer.await_args.args[0] is next_item


async def test_a_prepare_aborted_while_resolving_the_item_just_stops() -> None:
    """Aborting a prewarm before its item is resolved has no source to release."""
    controller, _next_item, mass = _controller_with_next_item()
    started = asyncio.Event()

    async def _hang(*_args: object) -> None:
        started.set()
        await asyncio.Event().wait()

    controller.load_next_queue_item = _hang  # type: ignore[method-assign, assignment]
    mass.streams.audio.get_audio_buffer = AsyncMock()

    controller.prepare_next_audio_buffer("queue-1", "current")
    prepare_task = asyncio.create_task(mass.create_task.call_args.args[0])
    await started.wait()
    prepare_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await prepare_task

    mass.streams.audio.get_audio_buffer.assert_not_awaited()


async def test_prepare_next_does_nothing_when_the_item_repeats_itself() -> None:
    """With repeat-one the streamed item follows itself, and its own audio is not prepared."""
    controller, _next_item, mass = _controller_with_next_item()
    cast("Any", controller._queue_data["queue-1"].queue).repeat_mode = RepeatMode.ONE

    controller.prepare_next_audio_buffer("queue-1", "current")

    mass.create_task.assert_not_called()


def _fully_buffered_controller(
    *, is_realtime: bool, served: str | None
) -> tuple[PlayerQueuesController, MagicMock]:
    """
    Build a controller whose streamed item has fully arrived, with a stubbed preparation.

    :param is_realtime: Whether the streamed item's source hands over its audio just-in-time.
    :param served: The item the player is fetching, or None when it fetched nothing yet.
    """
    controller, _next_item, _mass = _controller_with_next_item()
    _streamed_item(controller).streamdetails = SimpleNamespace(
        is_realtime=is_realtime, media_type=MediaType.TRACK
    )
    controller._queue_data["queue-1"].last_served_item_id = served
    prepare = MagicMock()
    controller.prepare_next_audio_buffer = prepare  # type: ignore[method-assign]
    return controller, prepare


async def test_a_fully_arrived_realtime_track_prepares_its_successor() -> None:
    """A realtime track the player is fetching chains into preparing the next item."""
    controller, prepare = _fully_buffered_controller(is_realtime=True, served="current")

    controller.track_fully_buffered("queue-1", "current")

    prepare.assert_called_once_with("queue-1", "current")


async def test_a_fully_arrived_source_that_fills_ahead_prepares_nothing() -> None:
    """A source that fills ahead of playback leaves its successor to the end of its stream."""
    controller, prepare = _fully_buffered_controller(is_realtime=False, served="current")

    controller.track_fully_buffered("queue-1", "current")

    prepare.assert_not_called()


async def test_a_fully_arrived_track_the_player_has_not_fetched_prepares_nothing() -> None:
    """
    The fills do not chain ahead of what the player has fetched.

    The crossfade path raises the buffered index to the incoming track before the player
    asks for it, so that index does not count as the player fetching the track.
    """
    controller, prepare = _fully_buffered_controller(is_realtime=True, served="audible")
    queue_data = controller._queue_data["queue-1"]
    queue_data.items.insert(
        0, cast("Any", SimpleNamespace(queue_item_id="audible", available=True))
    )
    queue = cast("Any", queue_data.queue)
    queue.current_index = 0
    queue.index_in_buffer = 1

    controller.track_fully_buffered("queue-1", "current")

    prepare.assert_not_called()


async def test_a_fully_arrived_track_without_a_served_item_prepares_nothing() -> None:
    """A queue whose player fetched nothing yet gives the fills nothing to chain on."""
    controller, prepare = _fully_buffered_controller(is_realtime=True, served=None)

    controller.track_fully_buffered("queue-1", "current")

    prepare.assert_not_called()


async def test_a_repeated_prepare_for_the_same_item_joins_the_running_one() -> None:
    """Asking again for the audio of the same next item does not restart its preparation."""
    controller, _next_item, mass = _controller_with_next_item()

    controller.prepare_next_audio_buffer("queue-1", "current")
    controller.prepare_next_audio_buffer("queue-1", "current")

    assert [call.kwargs["abort_existing"] for call in mass.create_task.call_args_list] == [
        True,
        False,
    ]
    for call in mass.create_task.call_args_list:
        call.args[0].close()


async def test_a_prepare_for_another_item_replaces_the_running_one() -> None:
    """A queue that changed under a preparation gets the new next item prepared instead."""
    controller, _next_item, mass = _controller_with_next_item()
    other_item = SimpleNamespace(
        queue_item_id="other",
        media_type=MediaType.TRACK,
        streamdetails=None,
        name="Other",
        available=True,
    )

    controller.prepare_next_audio_buffer("queue-1", "current")
    # the item following the streamed one changes before the second call
    controller._queue_data["queue-1"].items.insert(1, cast("Any", other_item))
    controller.prepare_next_audio_buffer("queue-1", "current")

    assert [call.kwargs["abort_existing"] for call in mass.create_task.call_args_list] == [
        True,
        True,
    ]
    for call in mass.create_task.call_args_list:
        call.args[0].close()


async def test_a_repeated_prepare_hands_back_the_preparation_already_running(
    mass_minimal: MusicAssistant,
) -> None:
    """The second call for the same item returns the running preparation, uncancelled."""
    controller, _next_item, _mass = _controller_with_next_item()
    controller.mass = mass_minimal
    resolving = asyncio.Event()

    async def _hang(*_args: object) -> None:
        resolving.set()
        await asyncio.Event().wait()

    controller.load_next_queue_item = _hang  # type: ignore[method-assign, assignment]

    first = controller.prepare_next_audio_buffer("queue-1", "current")
    await resolving.wait()
    second = controller.prepare_next_audio_buffer("queue-1", "current")

    assert first is not None
    assert second is first
    assert not first.cancelled()
    assert not first.done()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_a_repeated_prepare_joins_a_preparation_that_skipped_ahead() -> None:
    """A preparation that skipped an unplayable item is joined, not restarted."""
    controller, next_item, mass = _controller_with_next_item()
    later_item = SimpleNamespace(
        queue_item_id="later",
        media_type=MediaType.TRACK,
        streamdetails=None,
        name="Later",
        available=True,
    )
    controller._queue_data["queue-1"].items.append(cast("Any", later_item))
    filling = asyncio.Event()

    async def _skip_unplayable(*_args: object) -> SimpleNamespace:
        next_item.available = False
        return later_item

    async def _hang(*_args: object, **_kwargs: object) -> None:
        # no buffer is attached yet while the source is being opened
        filling.set()
        await asyncio.Event().wait()

    controller.load_next_queue_item = _skip_unplayable  # type: ignore[method-assign, assignment]
    mass.streams.audio.get_audio_buffer = _hang

    controller.prepare_next_audio_buffer("queue-1", "current")
    preparation = asyncio.create_task(mass.create_task.call_args.args[0])
    await filling.wait()
    controller.prepare_next_audio_buffer("queue-1", "current")

    assert [call.kwargs["abort_existing"] for call in mass.create_task.call_args_list] == [
        True,
        False,
    ]
    mass.create_task.call_args.args[0].close()
    preparation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await preparation
