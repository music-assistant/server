"""
Tests for PlayerQueuesController flow-stream EOF tracking.

Player providers query ``flow_stream_finished`` to recognise an end-of-stream
condition that does not surface as a normal idle report (e.g. a Cast group that
underruns the LIVE flow stream at EOF and keeps reporting buffering/playing).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.player_queue import PlayLogEntry

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant.models.player import Player


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


def _streamed_session(state: PlaybackState = PlaybackState.PLAYING) -> MagicMock:
    """Return a fake controller whose queue streamed 150 seconds in session sess-1."""
    queue = MagicMock(queue_id="q", session_id="sess-1", state=state)
    fake = _fake_controller(queue)
    fake._queue_data["q"].flow_mode_stream_log = [
        PlayLogEntry("first", seconds_streamed=100),
        PlayLogEntry("last", seconds_streamed=50),
    ]
    return fake


def _played_out(
    fake: MagicMock, elapsed: float | None, queue_id: str = "q", session_id: str = "sess-1"
) -> bool:
    player = SimpleNamespace(state=SimpleNamespace(corrected_elapsed_time=elapsed))
    return PlayerQueuesController.flow_stream_played_out(
        cast("PlayerQueuesController", fake), queue_id, session_id, cast("Player", player)
    )


@pytest.mark.parametrize("elapsed", [140.0, None])
def test_flow_stream_not_played_out_while_the_player_holds_audio(elapsed: float | None) -> None:
    """A player that has not reached the end of what the session streamed still needs it."""
    assert _played_out(_streamed_session(), elapsed) is False


def test_flow_stream_played_out_once_the_player_reached_its_end() -> None:
    """A player that played everything the session streamed is done with it."""
    assert _played_out(_streamed_session(), 149.6) is True


@pytest.mark.parametrize("state", [PlaybackState.IDLE, PlaybackState.PAUSED])
def test_flow_stream_played_out_when_the_queue_stopped_playing(state: PlaybackState) -> None:
    """A queue that no longer plays needs nothing more of the session."""
    assert _played_out(_streamed_session(state), 10.0) is True


def test_flow_stream_played_out_when_a_newer_session_took_over() -> None:
    """The audio of a superseded session is no longer played."""
    fake = _streamed_session()
    fake._queue_data["q"].session_id = "sess-2"

    assert _played_out(fake, 10.0) is True


def test_flow_stream_played_out_for_unknown_queue() -> None:
    """There is nothing to hold for a queue that does not exist."""
    assert _played_out(_streamed_session(), 10.0, queue_id="other") is True


async def test_queue_resumes_items_added_while_a_long_buffer_plays_out() -> None:
    """
    Items added while the player plays out a long buffer still start once it goes idle.

    A held-open flow stream lets the player play its whole buffer, which takes minutes after a
    long session, before it reports idle.
    """
    queue = MagicMock(
        queue_id="q",
        session_id="sess-1",
        active=True,
        state=PlaybackState.PLAYING,
        current_index=0,
        display_name="Queue",
    )
    fake = _fake_controller(queue)
    scheduled: list[Any] = []
    fake.mass.create_task = MagicMock(side_effect=lambda coro, **_kw: scheduled.append(coro))
    fake.mass.players.get_player.return_value = MagicMock(offline_by_power_control=False)
    fake.get_next_item.return_value = MagicMock(queue_item_id="added")
    fake.index_by_id.return_value = 1
    fake.play_index = AsyncMock()
    polls = 0

    async def _sleep(_seconds: float) -> None:
        nonlocal polls
        polls += 1
        if polls == 120:
            queue.state = PlaybackState.IDLE

    PlayerQueuesController.queue_buffer_completed(
        cast("PlayerQueuesController", fake), "q", queue_exhausted=True
    )
    with patch("music_assistant.controllers.player_queues.controller.asyncio.sleep", _sleep):
        await scheduled[0]

    fake.play_index.assert_awaited_once_with("q", 1)
