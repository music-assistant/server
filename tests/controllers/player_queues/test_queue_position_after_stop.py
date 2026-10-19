"""A stopped queue keeps the position its item was last played at."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState, PlayerType
from music_assistant_models.media_items import Audiobook, AudioFormat, ProviderMapping
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "player-1"
DURATION = 3000
PLAYED = 117


def _controller() -> PlayerQueuesController:
    item = QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id="book",
        name="Book",
        duration=DURATION,
        media_item=Audiobook(
            item_id="book",
            provider="prov",
            name="Book",
            duration=DURATION,
            provider_mappings={
                ProviderMapping(item_id="book", provider_domain="prov", provider_instance="prov")
            },
        ),
    )
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="P", available=True, items=1)
    ctrl._queue_data = {QUEUE_ID: PlayerQueueData(queue=queue, items=[item])}

    async def load_item(queue_item: QueueItem, seek_position: int = 0, **_kw: Any) -> None:
        queue_item.streamdetails = StreamDetails(
            provider="prov",
            item_id="book",
            audio_format=AudioFormat(),
            media_type=MediaType.AUDIOBOOK,
            seek_position=seek_position,
            duration=DURATION,
        )

    ctrl._load_item = AsyncMock(side_effect=load_item)  # type: ignore[method-assign]
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl.player_media_from_queue_item = AsyncMock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.streams.is_smart_fades_active.return_value = False
    ctrl.mass.music.get_playback_speed = AsyncMock(return_value=1.0)
    ctrl.mass.players.play_media = AsyncMock()
    return ctrl


def _player(state: PlaybackState, stream_elapsed: float) -> Any:
    """Return a player reporting its position within the stream it was sent."""
    return SimpleNamespace(
        player_id=QUEUE_ID,
        type=PlayerType.PLAYER,
        active_output_protocol=None,
        current_media=SimpleNamespace(source_id=QUEUE_ID, queue_item_id="book", uri=None),
        state=SimpleNamespace(
            name="P",
            available=True,
            playback_state=state,
            corrected_elapsed_time=stream_elapsed,
            group_members=[],
        ),
    )


@pytest.mark.parametrize(
    ("started_at", "reported_after_stop"),
    [
        (1000, PLAYED),
        (1000, DURATION - 1000),  # stopped player reports the length of the stream it was sent
        (1000, 0),  # stopped player resets its position
        (0, DURATION),
    ],
)
async def test_stopped_queue_keeps_last_played_position(
    started_at: int, reported_after_stop: float
) -> None:
    """A position jump reported by a stopped player does not move the queue position."""
    ctrl = _controller()
    await ctrl.play_index(QUEUE_ID, 0, seek_position=started_at)
    for state, elapsed in (
        (PlaybackState.PLAYING, 0),
        (PlaybackState.PLAYING, PLAYED),
        (PlaybackState.PAUSED, PLAYED),
        (PlaybackState.IDLE, PLAYED),
    ):
        ctrl._update_queue_from_player(_player(state, elapsed))

    ctrl.on_player_elapsed_time_corrected(_player(PlaybackState.IDLE, reported_after_stop))

    assert ctrl._queue_data[QUEUE_ID].queue.elapsed_time == started_at + PLAYED


async def test_playing_queue_rebases_on_position_jump() -> None:
    """A position jump reported while playing re-bases the queue position."""
    ctrl = _controller()
    await ctrl.play_index(QUEUE_ID, 0, seek_position=1000)
    ctrl._update_queue_from_player(_player(PlaybackState.PLAYING, PLAYED))

    ctrl.on_player_elapsed_time_corrected(_player(PlaybackState.PLAYING, 500))

    assert ctrl._queue_data[QUEUE_ID].queue.elapsed_time == 1500
