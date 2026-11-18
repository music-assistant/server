"""
Tests for the stop of a queue whose items get no audio from their source.

When the source of SOURCE_FAILURE_LIMIT items in a row fails to deliver audio (a music service or
the network is down), the queue stops instead of running on through its items, and is set back to
the first of them. The row is kept after the stop, so a play during the outage fails at its
first item without audio. Audio of an item that reaches the player, or new media that replaces
the queue, starts the count over.
"""

from __future__ import annotations

import time
from collections.abc import Coroutine
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.errors import AudioError, MediaNotFoundError, QueueEmpty
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.constants import SOURCE_FAILURE_LIMIT
from music_assistant.controllers.player_queues.helpers import CompareState
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "queue-1"
SESSION = "session-1"
INSTANCE = "svc--1"
OTHER_INSTANCE = "other--1"
ITEM_IDS = ["a", "b", "c", "d", "e"]


def _item(item_id: str, provider: str = INSTANCE) -> QueueItem:
    """Build a track queue item that streams from the given provider instance."""
    item = QueueItem(
        queue_id=QUEUE_ID, queue_item_id=item_id, name=f"Track {item_id}", duration=180
    )
    item.streamdetails = StreamDetails(
        provider=provider, item_id=item_id, audio_format=AudioFormat(), media_type=MediaType.TRACK
    )
    return item


class _Rig:
    """A bare queue controller with one queue that plays session-1, on its fourth item."""

    def __init__(self, providers: dict[str, str] | None = None) -> None:
        """
        Build the controller.

        :param providers: The provider instance of each item that does not stream from INSTANCE.
        """
        providers = providers or {}
        self.items = [_item(item_id, providers.get(item_id, INSTANCE)) for item_id in ITEM_IDS]
        queue = PlayerQueue(
            queue_id=QUEUE_ID, active=True, display_name="Queue", available=True, items=5
        )
        queue.state = PlaybackState.PLAYING
        queue.current_index = 3
        queue.current_item = self.items[3]
        queue.elapsed_time = 42
        queue.elapsed_time_last_updated = time.time()
        queue.resume_pos = 17
        self.queue = queue
        self.queue_data = PlayerQueueData(queue=queue, items=self.items)
        self.queue_data.session_id = SESSION
        ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
        ctrl._queue_data = {QUEUE_ID: self.queue_data}
        self.logger = MagicMock()
        ctrl.logger = self.logger
        self.mass = MagicMock()
        ctrl.mass = self.mass
        self.tasks: dict[str | None, Coroutine[Any, Any, None]] = {}
        self.mass.create_task = Mock(side_effect=self._create_task)
        names = {INSTANCE: "Svc", OTHER_INSTANCE: "Other"}
        self.mass.get_provider = Mock(
            side_effect=lambda instance, **_kwargs: _provider(names[instance])
        )
        # a stop ends the playback session of the queue
        self.handle_stop = AsyncMock(
            side_effect=lambda _queue_id: setattr(self.queue_data, "session_id", None)
        )
        ctrl._handle_stop = self.handle_stop  # type: ignore[method-assign]
        ctrl.signal_update = Mock()  # type: ignore[method-assign]
        ctrl.update_items = Mock()  # type: ignore[method-assign]
        ctrl._get_resume_position = AsyncMock(return_value=0)  # type: ignore[method-assign]
        self.ctrl = ctrl

    def fail(self, item_id: str, session_id: str | None = SESSION) -> bool:
        """Report that the source of the given item failed, as a stream does."""
        return self.ctrl.report_source_failure(self.item(item_id), session_id)

    def item(self, item_id: str) -> QueueItem:
        """Return the queue item with the given id."""
        return self.items[ITEM_IDS.index(item_id)]

    async def run_tasks(self) -> None:
        """Run the tasks the controller started."""
        while self.tasks:
            await self.tasks.pop(next(iter(self.tasks)))

    def _create_task(self, coro: Coroutine[Any, Any, None], task_id: str | None = None) -> None:
        """Start a task as the server does: not again while one with its id is pending."""
        if task_id is not None and task_id in self.tasks:
            coro.close()
            return
        self.tasks[task_id] = coro


async def _stop_for_a_row(rig: _Rig) -> None:
    """Have the first three items fail in a row, and let the queue stop for them."""
    for item_id in ("a", "b", "c"):
        rig.fail(item_id)
    await rig.run_tasks()


def _provider(name: str) -> MagicMock:
    """Build a provider stand-in with the given name."""
    provider = MagicMock()
    provider.name = name
    return provider


async def test_items_without_audio_in_a_row_stop_the_queue_at_the_first_of_them() -> None:
    """The queue stops and goes back to the first failed item, from its start and playable."""
    rig = _Rig()
    # the next item path marks the items it could not load as unplayable
    rig.item("b").available = False
    rig.item("c").available = False
    rig.queue_data.prev_state = CompareState(
        queue_id=QUEUE_ID,
        state=PlaybackState.PLAYING,
        current_item_id="a",
        next_item_id="b",
        current_item=rig.item("a"),
        elapsed_time=150,
        last_playing_elapsed_time=150,
        stream_title=None,
        codec_type=None,
        output_player_ids=None,
    )

    assert [rig.fail(item_id) for item_id in ("a", "b", "c")] == [False, False, True]
    rig.handle_stop.assert_not_awaited()
    await rig.run_tasks()

    rig.handle_stop.assert_awaited_once_with(QUEUE_ID)
    wait = rig.mass.players.wait_for_player_update
    assert wait.call_args.kwargs["attribute_value"] == PlaybackState.IDLE
    assert rig.queue.current_index == 0
    assert rig.queue.current_item is rig.item("a")
    assert rig.queue.elapsed_time == 0
    assert rig.queue.resume_pos == 0
    assert rig.queue_data.prev_state["last_playing_elapsed_time"] == 0
    assert all(rig.item(item_id).available for item_id in ITEM_IDS)
    # the row is kept, so a play during the outage stops at its first item without audio
    assert list(rig.queue_data.failed_source_items) == ["a", "b", "c"]
    warning = rig.logger.warning.call_args.args
    assert "Queue" in warning
    assert "Svc" in warning


async def test_the_playback_that_stops_is_stopped_once() -> None:
    """Items the stopping playback still reports stop nothing more, before or after the stop."""
    rig = _Rig()
    for item_id in ("a", "b", "c"):
        rig.fail(item_id)

    # reported before the stop ran: the stop that is on its way is the one for it
    assert rig.fail("d") is True
    await rig.run_tasks()
    # reported after the stop: its playback ended
    assert rig.fail("e") is False

    task_ids = {call.kwargs["task_id"] for call in rig.mass.create_task.call_args_list}
    assert task_ids == {f"stop_for_failed_sources_{QUEUE_ID}"}
    rig.handle_stop.assert_awaited_once_with(QUEUE_ID)
    assert rig.tasks == {}
    assert list(rig.queue_data.failed_source_items) == ["a", "b", "c", "d"]


async def test_audio_that_reaches_the_player_starts_the_count_over() -> None:
    """An item whose audio went out between failures breaks the row."""
    rig = _Rig()
    rig.fail("a")
    rig.fail("b")

    rig.ctrl.mark_item_served(QUEUE_ID, "c")

    assert rig.fail("d") is False
    assert rig.fail("e") is False
    assert rig.tasks == {}
    assert list(rig.queue_data.failed_source_items) == ["d", "e"]


async def test_an_empty_stream_of_a_failed_item_keeps_the_count() -> None:
    """The output of an item whose source failed can send out an empty stream; it is no audio."""
    rig = _Rig()
    rig.fail("a")
    rig.fail("b")
    # the stream of b failed
    streamdetails = rig.item("b").streamdetails
    assert streamdetails is not None
    streamdetails.stream_error = True

    rig.ctrl.mark_item_served(QUEUE_ID, "b")

    assert rig.fail("c") is True


async def test_an_item_that_fails_in_two_paths_counts_once() -> None:
    """The next item path and the stream of the same item count it once."""
    rig = _Rig()

    async def _load_item(queue_item: QueueItem, **_kwargs: Any) -> None:
        if queue_item.queue_item_id == "b":
            raise AudioError("Timeout waiting for audio data")

    rig.ctrl._load_item = AsyncMock(side_effect=_load_item)  # type: ignore[method-assign]

    next_item = await rig.ctrl.load_next_queue_item(QUEUE_ID, "a")

    assert next_item is rig.item("c")
    assert rig.fail("b") is False
    assert rig.fail("d") is False
    assert list(rig.queue_data.failed_source_items) == ["b", "d"]
    assert rig.tasks == {}


async def test_an_item_that_is_not_found_does_not_count() -> None:
    """A playlist with removed tracks skips them as before, without stopping the queue."""
    rig = _Rig()

    async def _load_item(queue_item: QueueItem, **_kwargs: Any) -> None:
        if queue_item.queue_item_id != "e":
            raise MediaNotFoundError(f"{queue_item.name} is gone")

    rig.ctrl._load_item = AsyncMock(side_effect=_load_item)  # type: ignore[method-assign]

    next_item = await rig.ctrl.load_next_queue_item(QUEUE_ID, "a")

    assert next_item is rig.item("e")
    assert rig.queue_data.failed_source_items == {}
    assert rig.tasks == {}


async def test_the_next_item_path_stops_walking_when_the_queue_stops() -> None:
    """Once the queue stops for its items, the items after them are left as they are."""
    rig = _Rig()
    load_item = AsyncMock(side_effect=AudioError("Timeout waiting for audio data"))
    rig.ctrl._load_item = load_item  # type: ignore[method-assign]

    with pytest.raises(QueueEmpty):
        await rig.ctrl.load_next_queue_item(QUEUE_ID, "a")

    assert load_item.await_count == SOURCE_FAILURE_LIMIT
    assert rig.item("e").available is True
    await rig.run_tasks()
    rig.handle_stop.assert_awaited_once_with(QUEUE_ID)
    assert rig.queue.current_item is rig.item("b")
    assert all(rig.item(item_id).available for item_id in ("b", "c", "d"))


async def test_a_next_item_that_fails_after_another_playback_took_over_is_not_counted() -> None:
    """A next item of the playback that was replaced while it loaded does not count."""
    rig = _Rig()

    async def _load_item(*_args: Any, **_kwargs: Any) -> None:
        rig.queue_data.session_id = "session-2"
        raise AudioError("Timeout waiting for audio data")

    rig.ctrl._load_item = _load_item  # type: ignore[method-assign]

    with pytest.raises(QueueEmpty):
        await rig.ctrl.load_next_queue_item(QUEUE_ID, "a")

    assert rig.queue_data.failed_source_items == {}
    rig.handle_stop.assert_not_awaited()


async def test_a_session_that_no_longer_plays_is_not_counted() -> None:
    """A stream of an earlier session, or of a queue that stopped, ended by our own hand."""
    rig = _Rig()

    assert rig.fail("a", "an-earlier-session") is False
    rig.queue_data.session_id = None
    assert rig.fail("b") is False

    assert rig.queue_data.failed_source_items == {}


async def test_a_command_that_went_first_keeps_the_queue() -> None:
    """A play or a stop that came in before the queue stopped is not undone."""
    rig = _Rig()
    for item_id in ("a", "b", "c"):
        rig.fail(item_id)

    rig.queue_data.session_id = "a-new-session"
    await rig.run_tasks()

    rig.handle_stop.assert_not_awaited()
    assert rig.queue.current_item is rig.item("d")


async def test_audio_after_the_stop_starts_the_count_over() -> None:
    """The first item that plays again with audio, one of the failed items, ends the row."""
    rig = _Rig()
    await _stop_for_a_row(rig)
    assert list(rig.queue_data.failed_source_items) == ["a", "b", "c"]
    rig.queue_data.session_id = "session-2"

    rig.ctrl.mark_item_served(QUEUE_ID, "a")

    assert rig.queue_data.failed_source_items == {}
    assert rig.fail("b", "session-2") is False
    assert rig.tasks == {}


@pytest.mark.parametrize("change", ["replace", "clear"])
async def test_new_media_after_the_stop_starts_the_count_over(change: str) -> None:
    """New media that replaces the queue, or a cleared queue, leaves the row behind."""
    rig = _Rig()
    await _stop_for_a_row(rig)
    assert list(rig.queue_data.failed_source_items) == ["a", "b", "c"]
    ctrl = rig.ctrl

    if change == "replace":
        await ctrl.load(QUEUE_ID, [_item("x"), _item("y")], keep_remaining=False, keep_played=False)
    else:
        ctrl.store_sources = Mock()  # type: ignore[method-assign]
        ctrl.is_smart_shuffle_active = Mock(return_value=False)  # type: ignore[method-assign]
        ctrl._clear(QUEUE_ID, skip_stop=True)
        # the audio cleanup the clear starts is not part of this test
        for coro in rig.tasks.values():
            coro.close()
        rig.tasks.clear()

    assert rig.queue_data.failed_source_items == {}


@pytest.mark.parametrize(
    ("providers", "translation_key", "translation_args"),
    [
        ({}, "no_audio_from_provider", ["Svc"]),
        ({"c": OTHER_INSTANCE}, "no_audio_from_sources", []),
    ],
)
async def test_a_start_without_audio_fails_with_a_clear_error(
    providers: dict[str, str], translation_key: str, translation_args: list[str]
) -> None:
    """A play that gets no audio stops at the limit and says why, naming a shared provider."""
    rig = _Rig(providers)
    ctrl = rig.ctrl
    load_item = AsyncMock(side_effect=AudioError("Timeout waiting for audio data"))
    ctrl._load_item = load_item  # type: ignore[method-assign]
    end_failed_start = AsyncMock()
    ctrl._end_failed_start = end_failed_start  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]

    with pytest.raises(AudioError) as err:
        await ctrl._play_index(QUEUE_ID, 1, None, fade_in=False)

    assert err.value.translation_key == translation_key
    assert err.value.translation_owner == "core.player_queues"
    assert err.value.translation_args == translation_args
    assert load_item.await_count == SOURCE_FAILURE_LIMIT
    end_failed_start.assert_awaited_once_with(rig.queue_data)
    assert rig.queue.current_item is rig.item("b")
    assert rig.queue.elapsed_time == 0
    assert list(rig.queue_data.failed_source_items) == ["b", "c", "d"]
    # the start stops the queue itself
    assert rig.tasks == {}


@pytest.mark.parametrize("start_item_id", ["a", "e"])
async def test_a_play_after_the_stop_fails_at_its_first_item_without_audio(
    start_item_id: str,
) -> None:
    """During the outage a play fails fast, and the queue stays on the item it started from."""
    rig = _Rig()
    await _stop_for_a_row(rig)
    ctrl = rig.ctrl
    load_item = AsyncMock(side_effect=AudioError("Timeout waiting for audio data"))
    ctrl._load_item = load_item  # type: ignore[method-assign]
    end_failed_start = AsyncMock()
    ctrl._end_failed_start = end_failed_start  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]

    with pytest.raises(AudioError) as err:
        await ctrl._play_index(QUEUE_ID, ITEM_IDS.index(start_item_id), None, fade_in=False)

    assert err.value.translation_key == "no_audio_from_provider"
    assert err.value.translation_args == ["Svc"]
    load_item.assert_awaited_once()
    end_failed_start.assert_awaited_once_with(rig.queue_data)
    assert rig.queue.current_item is rig.item(start_item_id)
    assert rig.queue.elapsed_time == 0
    assert all(rig.item(item_id).available for item_id in ("a", "b", "c", start_item_id))
