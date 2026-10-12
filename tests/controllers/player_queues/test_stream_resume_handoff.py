"""Tests for the queue's timeline while a player switches to a re-requested stream."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, Mock

from music_assistant_models.enums import MediaType, PlaybackState, PlayerType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "wiim-1"
ITEM_ID = "track"
START_SEEK = 8
# the player's counter in the stream it plays when the re-request arrives
STREAM_ELAPSED = 37
RESUME_AT = START_SEEK + STREAM_ELAPSED


def _player(stream_elapsed: float) -> SimpleNamespace:
    """Build the queue's player reporting its position in the stream it plays."""
    return SimpleNamespace(
        player_id=QUEUE_ID,
        type=PlayerType.PLAYER,
        protocol_parent_id=None,
        active_output_protocol=None,
        current_media=SimpleNamespace(source_id=QUEUE_ID, queue_item_id=ITEM_ID, uri=None),
        state=SimpleNamespace(
            name="WiiM",
            available=True,
            playback_state=PlaybackState.PLAYING,
            corrected_elapsed_time=stream_elapsed,
            group_members=[],
        ),
    )


def _controller() -> tuple[PlayerQueuesController, PlayerQueue, QueueItem]:
    """Build a bare controller playing a track that started at START_SEEK, at RESUME_AT now."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    item = QueueItem(queue_id=QUEUE_ID, queue_item_id=ITEM_ID, name="Track", duration=300)
    item.streamdetails = StreamDetails(
        provider="test",
        item_id=ITEM_ID,
        audio_format=AudioFormat(),
        media_type=MediaType.TRACK,
        seek_position=START_SEEK,
    )
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="WiiM", available=True, items=1
    )
    ctrl._queue_data = {QUEUE_ID: PlayerQueueData(queue=queue, items=[item])}
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.streams.is_smart_fades_active.return_value = False
    ctrl.mark_item_served(QUEUE_ID, ITEM_ID)
    ctrl._update_queue_from_player(_player(STREAM_ELAPSED))  # type: ignore[arg-type]
    return ctrl, queue, item


def _begin_resume(ctrl: PlayerQueuesController, item: QueueItem) -> None:
    """Re-request the playing track, as the streams controller does."""
    cast("MagicMock", ctrl.mass).players.get_player.return_value = _player(STREAM_ELAPSED)
    ctrl.begin_stream_resume(QUEUE_ID, ITEM_ID, RESUME_AT)
    assert item.streamdetails is not None
    item.streamdetails.seek_position = RESUME_AT


def _last_playing_elapsed(ctrl: PlayerQueuesController) -> int:
    prev_state = ctrl._queue_data[QUEUE_ID].prev_state
    assert prev_state is not None
    return prev_state["last_playing_elapsed_time"]


def test_stale_counter_of_the_old_stream_does_not_overshoot() -> None:
    """Until the player switches over, its old stream counter is not added to the new offset."""
    ctrl, queue, item = _controller()
    _begin_resume(ctrl, item)

    ctrl._update_queue_from_player(_player(STREAM_ELAPSED + 1))  # type: ignore[arg-type]
    assert RESUME_AT <= queue.elapsed_time < RESUME_AT + 1
    assert _last_playing_elapsed(ctrl) == RESUME_AT

    ctrl.on_player_elapsed_time_corrected(_player(STREAM_ELAPSED + 2))  # type: ignore[arg-type]
    assert RESUME_AT <= queue.elapsed_time < RESUME_AT + 1


def test_counter_restart_commits_the_resume_position() -> None:
    """Once the player's counter restarts on the new stream, elapsed follows it from RESUME_AT."""
    ctrl, queue, item = _controller()
    _begin_resume(ctrl, item)

    ctrl._update_queue_from_player(_player(0.5))  # type: ignore[arg-type]

    assert queue.elapsed_time == RESUME_AT + 0.5
    assert ctrl._queue_data[QUEUE_ID].pending_stream_resume is None


def test_counter_frozen_while_buffering_keeps_bridging() -> None:
    """A counter that stays frozen while the player buffers the new stream is not a switch."""
    ctrl, queue, item = _controller()
    _begin_resume(ctrl, item)
    queue_data = ctrl._queue_data[QUEUE_ID]
    assert queue_data.pending_stream_resume is not None
    queue_data.pending_stream_resume = dataclasses.replace(
        queue_data.pending_stream_resume,
        started_at=queue_data.pending_stream_resume.started_at - 12,
    )

    ctrl._update_queue_from_player(_player(STREAM_ELAPSED))  # type: ignore[arg-type]

    assert RESUME_AT + 12 <= queue.elapsed_time < RESUME_AT + 13
    assert queue_data.pending_stream_resume is not None
    assert item.streamdetails is not None
    assert item.streamdetails.seek_position == RESUME_AT
    assert _last_playing_elapsed(ctrl) <= queue.elapsed_time
