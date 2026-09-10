"""
Tests for PlayerQueuesController flow-stream EOF tracking.

Player providers query ``flow_stream_finished`` to recognise an end-of-stream
condition that does not surface as a normal idle report (e.g. a Cast group that
underruns the LIVE flow stream at EOF and keeps reporting buffering/playing).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue


def _fake_controller(queue: MagicMock) -> MagicMock:
    """Build a MagicMock standing in for the controller, owning a single queue."""

    def _close_coro(coro: Any, **_kwargs: Any) -> None:
        # close the scheduled resume coroutine so it doesn't raise 'never awaited'
        coro.close()

    fake = MagicMock()
    data = PlayerQueueData(queue=cast("PlayerQueue", queue))
    # session_id is server-side state on the record now, not on the wire queue
    data.session_id = queue.session_id
    fake._queue_data = {queue.queue_id: data}
    fake.get = MagicMock(
        side_effect=lambda qid: d.queue if (d := fake._queue_data.get(qid)) else None
    )
    fake.queue_data_or_none = MagicMock(side_effect=lambda qid: fake._queue_data.get(qid))
    fake.mass.create_task = MagicMock(side_effect=_close_coro)
    return fake


def _finished(fake: MagicMock, queue_id: str) -> bool:
    return PlayerQueuesController.flow_stream_finished(
        cast("PlayerQueuesController", fake), queue_id
    )


def test_flow_stream_finished_false_without_completion() -> None:
    """An untouched queue reports its flow stream as not finished."""
    queue = MagicMock(queue_id="q", session_id="sess-1")
    fake = _fake_controller(queue)

    assert _finished(fake, "q") is False


def test_queue_buffer_completed_marks_session_finished() -> None:
    """Buffer completion records the session so flow_stream_finished returns True."""
    queue = MagicMock(queue_id="q", session_id="sess-1")
    fake = _fake_controller(queue)

    PlayerQueuesController.queue_buffer_completed(
        cast("PlayerQueuesController", fake), "q", queue_exhausted=True
    )

    assert fake._queue_data["q"].flow_buffer_completed == "sess-1"
    assert _finished(fake, "q") is True


def test_flow_stream_finished_invalidated_by_new_session() -> None:
    """A completion from a previous session must not count for a fresh session."""
    queue = MagicMock(queue_id="q", session_id="sess-1")
    fake = _fake_controller(queue)
    PlayerQueuesController.queue_buffer_completed(
        cast("PlayerQueuesController", fake), "q", queue_exhausted=True
    )

    # a new playback session starts (play_index assigns a fresh session_id)
    fake._queue_data["q"].session_id = "sess-2"

    assert _finished(fake, "q") is False


def test_flow_stream_finished_false_for_unknown_queue() -> None:
    """An unknown queue id never reports a finished flow stream."""
    queue = MagicMock(queue_id="q", session_id="sess-1")
    fake = _fake_controller(queue)

    assert _finished(fake, "other") is False


def _exhausted(fake: MagicMock, queue_id: str, session_id: str) -> bool:
    return PlayerQueuesController.flow_queue_exhausted(
        cast("PlayerQueuesController", fake), queue_id, session_id
    )


def test_flow_queue_exhausted_set_when_queue_ran_out() -> None:
    """Playing the queue to its end marks the session as exhausted."""
    queue = MagicMock(queue_id="q", session_id="sess-1")
    fake = _fake_controller(queue)

    PlayerQueuesController.queue_buffer_completed(
        cast("PlayerQueuesController", fake), "q", queue_exhausted=True
    )

    assert _exhausted(fake, "q", "sess-1") is True


def test_flow_queue_not_exhausted_on_early_restart() -> None:
    """A flow that ended early to be restarted has no tail left to play out."""
    queue = MagicMock(queue_id="q", session_id="sess-1")
    fake = _fake_controller(queue)

    PlayerQueuesController.queue_buffer_completed(
        cast("PlayerQueuesController", fake), "q", queue_exhausted=False
    )

    assert fake._queue_data["q"].flow_buffer_completed == "sess-1"
    assert _exhausted(fake, "q", "sess-1") is False


def test_flow_queue_exhausted_invalidated_by_new_session() -> None:
    """A previous session's end of queue must not count for a fresh session."""
    queue = MagicMock(queue_id="q", session_id="sess-1")
    fake = _fake_controller(queue)
    PlayerQueuesController.queue_buffer_completed(
        cast("PlayerQueuesController", fake), "q", queue_exhausted=True
    )

    fake._queue_data["q"].session_id = "sess-2"

    assert _exhausted(fake, "q", "sess-1") is False
    assert _exhausted(fake, "q", "sess-2") is False


def test_flow_queue_exhausted_false_for_unknown_queue() -> None:
    """An unknown queue id never reports an exhausted flow stream."""
    queue = MagicMock(queue_id="q", session_id="sess-1")
    fake = _fake_controller(queue)

    assert _exhausted(fake, "other", "sess-1") is False
