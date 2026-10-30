"""Tests that the wall-clock time an item plays is recorded as listening sessions."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, Mock, patch

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.media_items import Audiobook, ProviderMapping
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "queue_1"
PROVIDER = "audiobookshelf--1"


def _controller(userid: str | None, served: bool) -> PlayerQueuesController:
    book = QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id="book_1",
        name="Book",
        duration=30000,
        media_item=Audiobook(
            item_id="book_1",
            provider=PROVIDER,
            name="Book",
            duration=30000,
            provider_mappings={
                ProviderMapping(
                    item_id="book_1", provider_domain="audiobookshelf", provider_instance=PROVIDER
                )
            },
        ),
    )
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Queue", available=True, items=1
    )
    ctrl._queue_data = {
        QUEUE_ID: PlayerQueueData(
            queue=queue,
            items=[book],
            userid=userid,
            served_item_ids={"book_1"} if served else set(),
        )
    }
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.streams.is_smart_fades_active.return_value = False
    return ctrl


def _player_reports(ctrl: PlayerQueuesController, state: PlaybackState, elapsed: float) -> None:
    player = SimpleNamespace(
        player_id=QUEUE_ID,
        active_output_protocol=None,
        current_media=SimpleNamespace(source_id=QUEUE_ID, queue_item_id="book_1", uri=None),
        state=SimpleNamespace(
            name="Queue",
            available=True,
            playback_state=state,
            corrected_elapsed_time=elapsed,
            group_members=[],
        ),
    )
    ctrl._update_queue_from_player(player)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("userid", "served", "expected"),
    [
        ("user_1", True, [(0, 31), (0, 45), (100, 30), (100, 65)]),
        (None, True, []),
        ("user_1", False, []),
    ],
)
def test_playing_time_is_written_per_session(
    userid: str | None, served: bool, expected: list[tuple[int, int]]
) -> None:
    """Play, pause and resume write each session's seconds; a reporting gap counts capped."""
    ctrl = _controller(userid, served)
    start = time.time()
    clock = Mock(return_value=start)
    with patch("music_assistant.controllers.player_queues.playback_tracker.time.time", clock):
        for offset, state, elapsed in (
            (0, PlaybackState.PLAYING, 0),
            (10, PlaybackState.PLAYING, 10),
            (31, PlaybackState.PLAYING, 31),
            (45, PlaybackState.PAUSED, 45),
            (100, PlaybackState.PLAYING, 45),
            (130, PlaybackState.PLAYING, 75),
            # the player went offline while playing and only reports again hours later
            (5000, PlaybackState.PAUSED, 75),
        ):
            clock.return_value = start + offset
            _player_reports(ctrl, state, elapsed)

    upsert = cast("Mock", ctrl.mass.music.database.upsert)
    assert [
        (round(call.args[1]["timestamp"] - start), call.args[1]["seconds"])
        for call in upsert.call_args_list
    ] == expected
