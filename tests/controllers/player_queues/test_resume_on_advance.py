"""Tests that a queue advancing on its own resumes an audiobook at its saved position."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, RepeatMode
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Audiobook, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue, PlayLogEntry
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.helpers import CompareState
from music_assistant.controllers.player_queues.state import FlowPlayLogEntry, PlayerQueueData

QUEUE_ID = "queue-1"


def _mappings(item_id: str) -> set[ProviderMapping]:
    return {
        ProviderMapping(
            item_id=item_id,
            provider_domain="filesystem_local",
            provider_instance="filesystem_local--1",
        )
    }


def _book(item_id: str, resume_position_ms: int = 0) -> QueueItem:
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=item_id,
        name=item_id,
        duration=120,
        media_item=Audiobook(
            item_id=item_id,
            provider="filesystem_local--1",
            name=item_id,
            duration=120,
            resume_position_ms=resume_position_ms,
            provider_mappings=_mappings(item_id),
        ),
    )


def _track(item_id: str) -> QueueItem:
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=item_id,
        name=item_id,
        duration=120,
        media_item=Track(
            item_id=item_id,
            provider="filesystem_local--1",
            name=item_id,
            duration=120,
            provider_mappings=_mappings(item_id),
        ),
    )


def _controller(
    items: list[QueueItem], repeat_mode: RepeatMode = RepeatMode.OFF
) -> tuple[PlayerQueuesController, AsyncMock]:
    """Build a bare controller holding the given items, returning its stream details mock."""
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.logger = MagicMock()
    controller._queue_data = {
        QUEUE_ID: PlayerQueueData(
            queue=PlayerQueue(
                queue_id=QUEUE_ID,
                active=True,
                display_name="Test queue",
                available=True,
                items=len(items),
                repeat_mode=repeat_mode,
            ),
            items=items,
        )
    }
    mass = MagicMock()
    mass.music.get_library_item_by_prov_id = AsyncMock(return_value=None)
    mass.music.get_item_by_uri = AsyncMock(
        side_effect=lambda uri: next(i.media_item for i in items if i.uri == uri)
    )
    get_stream_details = AsyncMock(return_value=MagicMock(duration=None))
    mass.streams.audio.get_stream_details = get_stream_details
    controller.mass = mass
    return controller, get_stream_details


def _report_played(
    controller: PlayerQueuesController, played: QueueItem, seconds_played: int, now: QueueItem
) -> None:
    """Have the playback tracker report an item played up to a position, then moved on."""

    def state(item: QueueItem, elapsed: int) -> CompareState:
        return CompareState(
            queue_id=QUEUE_ID,
            state=PlaybackState.PLAYING,
            current_item_id=item.queue_item_id,
            next_item_id=None,
            current_item=item,
            elapsed_time=elapsed,
            last_playing_elapsed_time=elapsed,
            stream_title=None,
            codec_type=None,
            output_player_ids=None,
        )

    queue = controller._queue_data[QUEUE_ID].queue
    queue.state = PlaybackState.PLAYING
    # the stream server records an item once its first chunk went out to the player
    controller.mark_item_served(QUEUE_ID, played.queue_item_id)
    controller._handle_playback_progress_report(queue, state(played, seconds_played), state(now, 0))


async def test_next_audiobook_starts_at_its_resume_point() -> None:
    """The next audiobook starts where it was left off, not at 0:00."""
    items = [_book("book-a"), _book("book-b", resume_position_ms=60000)]
    controller, get_stream_details = _controller(items)

    await controller.load_next_queue_item(QUEUE_ID, "book-a")

    assert get_stream_details.call_args.kwargs["seek_position"] == 59


async def test_repeated_audiobook_starts_from_the_beginning() -> None:
    """Repeat all wraps around to the start of the first audiobook, not its old bookmark."""
    items = [_book("book-a", resume_position_ms=60000), _book("book-b")]
    controller, get_stream_details = _controller(items, repeat_mode=RepeatMode.ALL)

    await controller.load_next_queue_item(QUEUE_ID, "book-b")

    assert get_stream_details.call_args.kwargs["seek_position"] == 0


async def test_repeated_single_audiobook_starts_from_the_beginning() -> None:
    """Repeat one plays the audiobook over from its start, not from its old bookmark."""
    items = [_book("book-a", resume_position_ms=60000), _book("book-b")]
    controller, get_stream_details = _controller(items, repeat_mode=RepeatMode.ONE)

    await controller.load_next_queue_item(QUEUE_ID, "book-a")

    assert get_stream_details.call_args.kwargs["seek_position"] == 0


@pytest.mark.parametrize("repeat_mode", [RepeatMode.ONE, RepeatMode.ALL])
async def test_repeat_preload_preserves_playing_audiobook_offset(repeat_mode: RepeatMode) -> None:
    """Preparing a repeat must not reset the offset of the audiobook still playing."""
    item = _book("book-a", resume_position_ms=60000)
    controller, get_stream_details = _controller([item], repeat_mode)
    item.streamdetails = MagicMock(seek_position=59)
    playing_details = item.streamdetails
    queue = controller._queue_data[QUEUE_ID].queue
    queue.current_item = item
    queue.flow_mode = True
    tasks: list[asyncio.Task[None]] = []
    cast("MagicMock", controller.mass).create_task.side_effect = lambda coro, **_kwargs: (
        tasks.append(asyncio.create_task(coro))
    )
    controller._enqueue_next_item = MagicMock()  # type: ignore[method-assign]

    controller._preload_next_item(QUEUE_ID, item.queue_item_id)
    await asyncio.gather(*tasks)

    get_stream_details.assert_not_awaited()
    assert item.streamdetails is playing_details
    assert item.streamdetails.seek_position == 59
    controller._enqueue_next_item.assert_not_called()

    await controller.load_next_queue_item(QUEUE_ID, item.queue_item_id)

    assert get_stream_details.call_args.kwargs["seek_position"] == 0


@pytest.mark.parametrize("repeat_mode", [RepeatMode.ONE, RepeatMode.ALL])
async def test_flow_repeat_preserves_previous_playback_offset(repeat_mode: RepeatMode) -> None:
    """Buffered audio from the previous pass keeps its seek offset after repeat loading."""
    item = _book("book-a", resume_position_ms=60000)
    controller, get_stream_details = _controller([item], repeat_mode)
    item.streamdetails = MagicMock(seek_position=59)
    queue_data = controller._queue_data[QUEUE_ID]
    queue_data.queue.current_item = item
    queue_data.queue.current_index = 0
    queue_data.queue.flow_mode = True
    previous_entry = FlowPlayLogEntry(item.queue_item_id, seconds_streamed=61, seek_position=59)
    queue_data.flow_mode_stream_log = [previous_entry]
    player = MagicMock()
    player.state = SimpleNamespace(corrected_elapsed_time=10, playback_state=PlaybackState.PLAYING)
    get_stream_details.return_value.seek_position = 0

    await controller.load_next_queue_item(QUEUE_ID, item.queue_item_id)
    queue_data.flow_mode_stream_log.append(PlayLogEntry(item.queue_item_id))

    assert get_stream_details.call_args.kwargs["seek_position"] == 0
    assert controller._get_flow_queue_stream_index(queue_data.queue, player) == (0, 69)
    player.state.corrected_elapsed_time = 66
    assert controller._get_flow_queue_stream_index(queue_data.queue, player) == (0, 5)


@pytest.mark.parametrize("repeat_mode", [RepeatMode.ONE, RepeatMode.ALL])
async def test_non_flow_repeat_preloads_and_enqueues(repeat_mode: RepeatMode) -> None:
    """Non-flow players need the repeated item enqueued to continue playback."""
    item = _book("book-a", resume_position_ms=60000)
    controller, get_stream_details = _controller([item], repeat_mode)
    queue = controller._queue_data[QUEUE_ID].queue
    queue.current_item = item
    queue.flow_mode = False
    item.streamdetails = MagicMock(seek_position=59)
    tasks: list[asyncio.Task[None]] = []
    cast("MagicMock", controller.mass).create_task.side_effect = lambda coro, **_kwargs: (
        tasks.append(asyncio.create_task(coro))
    )
    controller._enqueue_next_item = MagicMock()  # type: ignore[method-assign]

    controller._preload_next_item(QUEUE_ID, item.queue_item_id)
    await asyncio.gather(*tasks)

    get_stream_details.assert_awaited_once()
    assert get_stream_details.call_args.kwargs["seek_position"] == 0
    controller._enqueue_next_item.assert_called_once_with(QUEUE_ID, item)


@pytest.mark.parametrize("flow_mode", [False, True])
async def test_preload_next_audiobook_still_resumes_and_enqueues(flow_mode: bool) -> None:
    """A distinct upcoming audiobook is still prepared at its bookmark and enqueued."""
    items = [_book("book-a"), _book("book-b", resume_position_ms=60000)]
    controller, get_stream_details = _controller(items)
    controller._queue_data[QUEUE_ID].queue.current_item = items[0]
    controller._queue_data[QUEUE_ID].queue.flow_mode = flow_mode
    tasks: list[asyncio.Task[None]] = []
    cast("MagicMock", controller.mass).create_task.side_effect = lambda coro, **_kwargs: (
        tasks.append(asyncio.create_task(coro))
    )
    controller._enqueue_next_item = MagicMock()  # type: ignore[method-assign]

    controller._preload_next_item(QUEUE_ID, items[0].queue_item_id)
    await asyncio.gather(*tasks)

    assert get_stream_details.call_args.kwargs["seek_position"] == 59
    controller._enqueue_next_item.assert_called_once_with(QUEUE_ID, items[1])


@pytest.mark.parametrize("flow_mode", [False, True])
async def test_preload_reaches_item_beyond_next_item_scan(flow_mode: bool) -> None:
    """Preloading still tries the loader when the short next-item scan finds no candidate."""
    items = [_book("playing"), *[_book(f"unavailable-{idx}") for idx in range(5)], _book("next")]
    for item in items[1:6]:
        item.available = False
    controller, get_stream_details = _controller(items)
    queue = controller._queue_data[QUEUE_ID].queue
    queue.current_item = items[0]
    queue.flow_mode = flow_mode
    tasks: list[asyncio.Task[None]] = []
    cast("MagicMock", controller.mass).create_task.side_effect = lambda coro, **_kwargs: (
        tasks.append(asyncio.create_task(coro))
    )
    controller._enqueue_next_item = MagicMock()  # type: ignore[method-assign]

    assert controller.get_next_item(QUEUE_ID, items[0].queue_item_id) is None
    controller._preload_next_item(QUEUE_ID, items[0].queue_item_id)
    await asyncio.gather(*tasks)

    assert get_stream_details.call_args.kwargs["queue_item"] is items[-1]
    controller._enqueue_next_item.assert_called_once_with(QUEUE_ID, items[-1])


async def test_flow_preload_does_not_wrap_past_unavailable_items() -> None:
    """A deeper preload scan must not wrap around and reset the playing flow item's offset."""
    items = [_book("playing"), *[_book(f"unavailable-{idx}") for idx in range(5)]]
    for item in items[1:]:
        item.available = False
    controller, get_stream_details = _controller(items, repeat_mode=RepeatMode.ALL)
    queue = controller._queue_data[QUEUE_ID].queue
    queue.current_item = items[0]
    queue.flow_mode = True
    details = MagicMock(seek_position=59)
    items[0].streamdetails = details
    tasks: list[asyncio.Task[None]] = []
    cast("MagicMock", controller.mass).create_task.side_effect = lambda coro, **_kwargs: (
        tasks.append(asyncio.create_task(coro))
    )
    controller._enqueue_next_item = MagicMock()  # type: ignore[method-assign]

    assert controller.get_next_item(QUEUE_ID, items[0].queue_item_id) is None
    controller._preload_next_item(QUEUE_ID, items[0].queue_item_id)
    await asyncio.gather(*tasks)

    get_stream_details.assert_not_awaited()
    controller._enqueue_next_item.assert_not_called()
    assert items[0].streamdetails is details
    assert details.seek_position == 59


@pytest.mark.parametrize("unavailable_count", [0, 5])
async def test_flow_preload_failed_candidate_does_not_reload_playing_item(
    unavailable_count: int,
) -> None:
    """A candidate failing to load must not let repeat all reset the playing item's offset."""
    items = [
        _book("playing"),
        *[_book(f"unavailable-{idx}") for idx in range(unavailable_count)],
        _book("next"),
    ]
    for item in items[1:-1]:
        item.available = False
    controller, get_stream_details = _controller(items, repeat_mode=RepeatMode.ALL)
    queue = controller._queue_data[QUEUE_ID].queue
    queue.current_item = items[0]
    queue.flow_mode = True
    details = MagicMock(seek_position=59)
    items[0].streamdetails = details
    get_stream_details.side_effect = MediaNotFoundError("Candidate cannot be loaded")
    controller.update_items = MagicMock(wraps=controller.update_items)  # type: ignore[method-assign]
    tasks: list[asyncio.Task[None]] = []
    cast("MagicMock", controller.mass).create_task.side_effect = lambda coro, **_kwargs: (
        tasks.append(asyncio.create_task(coro))
    )
    controller._enqueue_next_item = MagicMock()  # type: ignore[method-assign]

    controller._preload_next_item(QUEUE_ID, "playing")
    await asyncio.gather(*tasks)

    get_stream_details.assert_awaited_once()
    assert get_stream_details.call_args.kwargs["queue_item"] is items[-1]
    assert not items[-1].available
    controller.update_items.assert_called_once_with(QUEUE_ID, items)
    controller._enqueue_next_item.assert_not_called()
    assert items[0].streamdetails is details
    assert details.seek_position == 59


async def test_audiobook_played_to_the_end_restarts_on_the_next_repeat_pass() -> None:
    """An audiobook finished during this queue starts at 0:00 on the next pass, not its bookmark."""
    items = [_book("book-a"), _book("book-b", resume_position_ms=60000)]
    controller, get_stream_details = _controller(items, repeat_mode=RepeatMode.ALL)
    _report_played(controller, items[1], seconds_played=118, now=items[0])

    await controller.load_next_queue_item(QUEUE_ID, "book-a")

    assert get_stream_details.call_args.kwargs["seek_position"] == 0


async def test_partly_played_audiobook_resumes_where_it_was_left_this_time() -> None:
    """An audiobook left part way during this queue resumes there, not at its enqueue bookmark."""
    items = [_book("book-a"), _book("book-b", resume_position_ms=10000)]
    controller, get_stream_details = _controller(items)
    _report_played(controller, items[1], seconds_played=40, now=items[0])

    await controller.load_next_queue_item(QUEUE_ID, "book-a")

    assert get_stream_details.call_args.kwargs["seek_position"] == 39


async def test_next_track_starts_from_the_beginning() -> None:
    """A music track that follows an audiobook plays from its start."""
    items = [_book("book-a", resume_position_ms=60000), _track("track-b")]
    controller, get_stream_details = _controller(items)

    await controller.load_next_queue_item(QUEUE_ID, "book-a")

    assert get_stream_details.call_args.kwargs["seek_position"] == 0
