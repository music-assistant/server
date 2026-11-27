"""A player that starts a fully streamed flow stream over at the end of the queue ends the queue."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import PlaybackState, PlayerType
from music_assistant_models.player_queue import PlayerQueue, PlayLogEntry
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "player-1"
SESSION_ID = "sess-1"
TRACK_S = 10


def _controller(
    item_ids: tuple[str, ...] = ("a", "b", "c"),
    streamed_ids: tuple[str, ...] = ("a", "b", "c"),
    position: float = 2 * TRACK_S + 9,
) -> tuple[PlayerQueuesController, list[Coroutine[Any, Any, None]]]:
    """Return a flow-mode queue at `position` s into one session that streamed `streamed_ids`."""
    items = [
        QueueItem(queue_id=QUEUE_ID, queue_item_id=item_id, name=item_id, duration=TRACK_S)
        for item_id in item_ids
    ]
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    ctrl.mass = MagicMock()
    ctrl.mass.streams.is_smart_fades_active.return_value = False
    ctrl.mass.players.get_player.return_value = MagicMock(offline_by_power_control=False)
    ctrl.signal_update = MagicMock()  # type: ignore[method-assign]
    ctrl._handle_playback_progress_report = MagicMock()  # type: ignore[method-assign]
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.mark_ended = MagicMock()  # type: ignore[method-assign]
    tasks: list[Coroutine[Any, Any, None]] = []
    ctrl.mass.create_task.side_effect = lambda coro, **_kwargs: tasks.append(coro)
    queue = PlayerQueue(
        queue_id=QUEUE_ID,
        active=True,
        display_name="P",
        available=True,
        items=len(items),
        flow_mode=True,
        current_index=0,
        current_item=items[0],
        next_item=items[1],
    )
    queue_data = PlayerQueueData(queue=queue, items=items)
    queue_data.session_id = queue_data.started_session_id = SESSION_ID
    queue_data.served_item_ids = dict.fromkeys(streamed_ids, SESSION_ID)
    queue_data.flow_mode_stream_log = [
        PlayLogEntry(item_id, seconds_streamed=TRACK_S) for item_id in streamed_ids
    ]
    ctrl._queue_data = {QUEUE_ID: queue_data}
    ctrl._update_queue_from_player(_player(PlaybackState.PLAYING, position))
    return ctrl, tasks


def _player(state: PlaybackState, stream_elapsed: float) -> Any:
    return SimpleNamespace(
        player_id=QUEUE_ID,
        type=PlayerType.PLAYER,
        extra_data={},
        resolve_output_protocol=lambda: SimpleNamespace(
            paused_position_reliable=False, current_media=None
        ),
        name="P",
        available=True,
        playback_state=state,
        active_source=QUEUE_ID,
        corrected_elapsed_time=stream_elapsed,
        group_members=[],
        fake_paused=False,
    )


async def _run(tasks: list[Coroutine[Any, Any, None]]) -> None:
    with patch("asyncio.sleep", AsyncMock()):
        while tasks:
            await tasks.pop(0)


async def test_restart_at_zero_after_the_end_ends_the_queue() -> None:
    """
    Sonos in flow mode reconnects to the flow stream once it played it out.

    It reports the stream at 0 s again before the stream server refuses it, then fails and goes
    idle: the queue must end on its last track instead of resuming after the first one.
    """
    ctrl, tasks = _controller()
    queue = ctrl._queue_data[QUEUE_ID].queue
    assert queue.current_index == 2
    ctrl.queue_buffer_completed(QUEUE_ID, queue_exhausted=True)

    ctrl._update_queue_from_player(_player(PlaybackState.PLAYING, 0.9))
    ctrl._update_queue_from_player(_player(PlaybackState.IDLE, 0))
    await asyncio.wait_for(_run(tasks), 5)

    play_index = ctrl.play_index
    assert isinstance(play_index, AsyncMock)
    play_index.assert_not_awaited()
    mark_ended = ctrl.mark_ended
    assert isinstance(mark_ended, MagicMock)
    mark_ended.assert_called_once_with(QUEUE_ID)
    assert queue.current_index == 2


async def test_restart_at_zero_reported_as_a_position_jump_keeps_the_last_track() -> None:
    """The jump back to 0 s can reach the queue as an elapsed time correction first."""
    ctrl, _tasks = _controller()
    queue = ctrl._queue_data[QUEUE_ID].queue
    ctrl.queue_buffer_completed(QUEUE_ID, queue_exhausted=True)

    ctrl.on_player_elapsed_time_corrected(_player(PlaybackState.PLAYING, 0.0))
    ctrl._update_queue_from_player(_player(PlaybackState.PLAYING, 0.7))

    assert queue.current_index == 2


async def test_restart_at_zero_after_a_format_restart_plays_the_next_track_once() -> None:
    """
    A flow stream that ends early for a sample rate change is played again from 0 s as well.

    The queue must go on with the first track of the next stream, not replay the last one.
    """
    ctrl, tasks = _controller(("a", "b", "c", "d"), ("a", "b"), TRACK_S + 9)
    queue = ctrl._queue_data[QUEUE_ID].queue
    assert queue.current_index == 1
    ctrl.queue_buffer_completed(QUEUE_ID, queue_exhausted=False)

    ctrl._update_queue_from_player(_player(PlaybackState.PLAYING, 0.9))
    assert queue.current_index == 1
    ctrl._update_queue_from_player(_player(PlaybackState.PLAYING, 2.9))
    assert queue.current_index == 1
    ctrl._update_queue_from_player(_player(PlaybackState.IDLE, 0))
    await asyncio.wait_for(_run(tasks), 5)

    play_index = ctrl.play_index
    assert isinstance(play_index, AsyncMock)
    play_index.assert_awaited_once_with(QUEUE_ID, 2)


async def test_fully_streamed_session_still_follows_the_player_from_its_start() -> None:
    """A short queue streamed in full before it plays (or a seek to its start) moves on as before."""
    ctrl, _tasks = _controller(position=0.5)
    queue = ctrl._queue_data[QUEUE_ID].queue
    ctrl.queue_buffer_completed(QUEUE_ID, queue_exhausted=True)

    for position, index in ((0.5, 0), (TRACK_S + 1, 1), (2 * TRACK_S + 1, 2)):
        ctrl._update_queue_from_player(_player(PlaybackState.PLAYING, position))
        assert queue.current_index == index
