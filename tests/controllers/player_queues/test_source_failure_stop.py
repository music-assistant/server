"""
Tests for the stop of a queue whose items get no audio from their source.

When the source of SOURCE_FAILURE_LIMIT items in a row fails to deliver audio (a music service or
the network is down), the queue stops instead of running on through its items. Audio of an item
that reaches the player starts the count over.
"""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "queue-1"
SESSION = "session-1"
ITEM_IDS = ["a", "b", "c", "d", "e"]
STOP_TASK_ID = f"stop_for_failed_sources_{QUEUE_ID}"


def _item(item_id: str) -> QueueItem:
    """Build a track queue item with stream details."""
    item = QueueItem(
        queue_id=QUEUE_ID, queue_item_id=item_id, name=f"Track {item_id}", duration=180
    )
    item.streamdetails = StreamDetails(
        provider="svc--1", item_id=item_id, audio_format=AudioFormat(), media_type=MediaType.TRACK
    )
    return item


class _Rig:
    """A bare queue controller with one queue that plays session-1."""

    def __init__(self) -> None:
        """Build the controller."""
        self.items = [_item(item_id) for item_id in ITEM_IDS]
        queue = PlayerQueue(
            queue_id=QUEUE_ID, active=True, display_name="Queue", available=True, items=5
        )
        queue.state = PlaybackState.PLAYING
        queue.current_index = 0
        queue.current_item = self.items[0]
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
        # a stop ends the playback session of the queue
        self.handle_stop = AsyncMock(
            side_effect=lambda _queue_id: setattr(self.queue_data, "session_id", None)
        )
        ctrl._handle_stop = self.handle_stop  # type: ignore[method-assign]
        ctrl.signal_update = Mock()  # type: ignore[method-assign]
        ctrl.update_items = Mock()  # type: ignore[method-assign]
        ctrl._get_resume_position = AsyncMock(return_value=0)  # type: ignore[method-assign]
        self.ctrl = ctrl

    def fail(self, item_id: str, session_id: str | None = SESSION) -> None:
        """Report that the source of the given item failed, as a stream does."""
        self.ctrl.report_source_failure(self.item(item_id), session_id)

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


async def test_items_without_audio_in_a_row_stop_the_queue() -> None:
    """The third item in a row without audio stops the queue."""
    rig = _Rig()
    rig.fail("a")
    rig.fail("b")
    assert rig.tasks == {}

    rig.fail("c")
    await rig.run_tasks()

    rig.handle_stop.assert_awaited_once_with(QUEUE_ID)
    warning = rig.logger.warning.call_args.args
    assert "Queue" in warning
    assert 3 in warning


async def test_the_playback_that_stops_is_stopped_once() -> None:
    """Items the stopping playback still reports stop nothing more, before or after the stop."""
    rig = _Rig()
    for item_id in ("a", "b", "c"):
        rig.fail(item_id)

    # reported before the stop ran: the stop that is on its way is the one for it
    rig.fail("d")
    await rig.run_tasks()
    # reported after the stop: its playback ended
    rig.fail("e")

    task_ids = {call.kwargs["task_id"] for call in rig.mass.create_task.call_args_list}
    assert task_ids == {STOP_TASK_ID}
    rig.handle_stop.assert_awaited_once_with(QUEUE_ID)
    assert rig.tasks == {}


async def test_audio_that_reaches_the_player_starts_the_count_over() -> None:
    """An item whose audio went out between failures breaks the row."""
    rig = _Rig()
    rig.fail("a")
    rig.fail("b")

    rig.ctrl.mark_item_served(QUEUE_ID, "c")
    rig.fail("d")
    rig.fail("e")

    assert rig.tasks == {}
    assert rig.queue_data.failed_source_items == {"d", "e"}


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
    rig.fail("c")

    assert STOP_TASK_ID in rig.tasks


async def test_an_item_without_stream_details_keeps_the_count() -> None:
    """A player that moves past an item it got no stream details for sent out no audio."""
    rig = _Rig()
    rig.fail("a")
    rig.item("b").streamdetails = None
    rig.fail("b")

    rig.ctrl.mark_item_served(QUEUE_ID, "b")
    rig.fail("c")

    assert STOP_TASK_ID in rig.tasks


async def test_an_item_that_fails_twice_counts_once() -> None:
    """An item whose stream reports its failure more than once counts once."""
    rig = _Rig()
    rig.fail("b")
    rig.fail("b")
    rig.fail("d")

    assert rig.tasks == {}
    assert rig.queue_data.failed_source_items == {"b", "d"}


async def test_a_session_that_no_longer_plays_is_not_counted() -> None:
    """A stream of an earlier session, or of a queue that stopped, ended by our own hand."""
    rig = _Rig()
    rig.fail("a", "an-earlier-session")
    rig.queue_data.session_id = None
    rig.fail("b")

    assert rig.queue_data.failed_source_items == set()


async def test_a_command_that_went_first_keeps_the_queue() -> None:
    """A play or a stop that came in before the queue stopped is not undone."""
    rig = _Rig()
    for item_id in ("a", "b", "c"):
        rig.fail(item_id)

    rig.queue_data.session_id = "a-new-session"
    await rig.run_tasks()

    rig.handle_stop.assert_not_awaited()


async def test_a_new_play_keeps_the_count() -> None:
    """A player that moves on to its next item with a new play does not start the row over."""
    rig = _Rig()
    rig.fail("a")
    rig.fail("b")
    ctrl = rig.ctrl
    ctrl._load_item = AsyncMock(side_effect=MediaNotFoundError("gone"))  # type: ignore[method-assign]
    ctrl._end_failed_start = AsyncMock()  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]

    with pytest.raises(MediaNotFoundError):
        await ctrl._play_index(QUEUE_ID, 2, None, fade_in=False)
    new_session = rig.queue_data.session_id
    assert new_session not in (None, SESSION)
    rig.fail("c", new_session)

    assert STOP_TASK_ID in rig.tasks
