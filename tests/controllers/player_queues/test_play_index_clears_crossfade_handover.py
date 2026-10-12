"""Tests that a new playback session drops the fade the previous one mixed ahead of playback."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.media_items import ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "sonos-1"
PROVIDER = "spotify--1"


def _track(item_id: str) -> QueueItem:
    return QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=item_id,
        name=item_id,
        duration=200,
        media_item=Track(
            item_id=item_id,
            provider=PROVIDER,
            name=item_id,
            duration=200,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id, provider_domain="spotify", provider_instance=PROVIDER
                )
            },
        ),
    )


def _controller(items: list[QueueItem]) -> PlayerQueuesController:
    """Build a bare controller holding the tracks, with everything beyond the start stubbed."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Sonos", available=True, items=len(items)
    )
    ctrl._queue_data = {QUEUE_ID: PlayerQueueData(queue=queue, items=items)}
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl._preload_next_item = Mock()  # type: ignore[method-assign]
    ctrl._cleanup_stale_queue_buffers = Mock()  # type: ignore[method-assign]
    ctrl._load_item = AsyncMock()  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl.player_media_from_queue_item = AsyncMock()  # type: ignore[method-assign]
    ctrl.mark_ended = Mock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.streams.is_smart_fades_active.return_value = False
    ctrl.mass.players.play_media = AsyncMock()
    return ctrl


async def test_a_new_start_drops_the_fade_the_previous_session_mixed() -> None:
    """A skip during a break leaves the fade it mixed into the next track with nothing to play it."""
    ctrl = _controller([_track("a"), _track("b"), _track("c")])

    await ctrl.play_index(QUEUE_ID, 2)

    clear_crossfade_handover = cast("MagicMock", ctrl.mass.streams.audio.clear_crossfade_handover)
    clear_crossfade_handover.assert_called_once_with(QUEUE_ID)
