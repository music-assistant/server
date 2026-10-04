"""Tests that pausing reports the position the player last played, not a reset one."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.media_items import Audiobook, AudioFormat, ProviderMapping
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "sonos-1"
PROVIDER = "audiobookshelf--1"
# where playback of the audiobook was resumed, so the stream starts here
RESUMED_AT = 19439


def _book() -> QueueItem:
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id="book",
        name="Book",
        duration=30000,
        media_item=Audiobook(
            item_id="book",
            provider=PROVIDER,
            name="Book",
            duration=30000,
            provider_mappings={
                ProviderMapping(
                    item_id="book", provider_domain="audiobookshelf", provider_instance=PROVIDER
                )
            },
        ),
    )


def _controller(item: QueueItem) -> PlayerQueuesController:
    """Build a bare controller holding the audiobook, with stream loading stubbed."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Sonos", available=True, items=1
    )
    ctrl._queue_data = {QUEUE_ID: PlayerQueueData(queue=queue, items=[item])}

    async def load_item(queue_item: QueueItem, seek_position: int = 0, **_kwargs: Any) -> None:
        queue_item.streamdetails = StreamDetails(
            provider=PROVIDER,
            item_id="book",
            audio_format=AudioFormat(),
            media_type=MediaType.AUDIOBOOK,
            seek_position=seek_position,
        )

    ctrl._load_item = AsyncMock(side_effect=load_item)  # type: ignore[method-assign]
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl._get_next_index = Mock(return_value=None)  # type: ignore[method-assign]
    ctrl.player_media_from_queue_item = AsyncMock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.streams.is_smart_fades_active.return_value = False
    ctrl.mass.music.get_playback_speed = AsyncMock(return_value=1.0)
    ctrl.mass.players.play_media = AsyncMock()
    return ctrl


def _player_reports(
    ctrl: PlayerQueuesController, state: PlaybackState, stream_elapsed: float
) -> None:
    """Feed a player update reporting its position within the stream it plays."""
    player = SimpleNamespace(
        player_id=QUEUE_ID,
        active_output_protocol=None,
        current_media=SimpleNamespace(source_id=QUEUE_ID, queue_item_id="book", uri=None),
        state=SimpleNamespace(
            name="Sonos",
            available=True,
            playback_state=state,
            corrected_elapsed_time=stream_elapsed,
            group_members=[],
        ),
    )
    ctrl._update_queue_from_player(player)  # type: ignore[arg-type]


def _reported_positions(ctrl: PlayerQueuesController) -> list[int]:
    mark_item_played = cast("Mock", ctrl.mass.music.mark_item_played)
    return [call.kwargs["seconds_played"] for call in mark_item_played.call_args_list]


@pytest.mark.parametrize("rewind_to", [None, 19000])
async def test_pause_reports_the_last_played_position(rewind_to: int | None) -> None:
    """A player resetting its position on pause must not move the progress back."""
    ctrl = _controller(_book())
    await ctrl.play_index(QUEUE_ID, 0, seek_position=RESUMED_AT)
    for stream_elapsed in (0, 91, 121):
        _player_reports(ctrl, PlaybackState.PLAYING, stream_elapsed)
    played_until = RESUMED_AT + 121
    if rewind_to is not None:
        await ctrl.seek(QUEUE_ID, rewind_to)
        for stream_elapsed in (0, 30):
            _player_reports(ctrl, PlaybackState.PLAYING, stream_elapsed)
        played_until = rewind_to + 30

    # like Sonos, which stops on pause and reports the start of the stream afterwards
    _player_reports(ctrl, PlaybackState.PAUSED, 0)

    assert _reported_positions(ctrl)[-1] == played_until
