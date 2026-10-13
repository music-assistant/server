"""Tests that a trailing sound effect (show outro) lets the queue end."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ProviderMapping, Radio, SoundEffect, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "q1"

_MEDIA_ITEM_CLS = {
    MediaType.TRACK: Track,
    MediaType.SOUND_EFFECT: SoundEffect,
    MediaType.RADIO: Radio,
}


def _item(item_id: str, media_type: MediaType) -> QueueItem:
    """
    Build a minimal queue item of the given media type.

    ``QueueItem.media_type`` is a derived property read off ``media_item``, so a real media
    item is built here rather than stamped onto the queue item directly.
    """
    media_item = _MEDIA_ITEM_CLS[media_type](
        item_id=item_id,
        provider="test",
        name=item_id,
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )
    item = QueueItem.from_media_item(QUEUE_ID, media_item)
    item.queue_item_id = item_id
    item.name = item_id
    return item


def _controller(items: list[QueueItem]) -> tuple[PlayerQueuesController, PlayerQueue, Mock]:
    """Build a bare controller with one queue holding the given items."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Q1", available=True, items=len(items)
    )
    queue_data = PlayerQueueData(queue=queue)
    queue_data.items = items
    ctrl._queue_data = {QUEUE_ID: queue_data}
    ctrl.logger = MagicMock()
    ctrl.mass = MagicMock()
    marker = Mock()
    ctrl.mark_ended = marker  # type: ignore[method-assign]
    return ctrl, queue, marker


async def test_trailing_sound_effect_marks_queue_ended() -> None:
    """A sound effect that ends the queue is a real end, not a live-source stop."""
    outro = _item("outro", MediaType.SOUND_EFFECT)
    ctrl, queue, marker = _controller([_item("t1", MediaType.TRACK), outro])
    ctrl._finish_queue(queue, outro)
    marker.assert_called_once_with(QUEUE_ID)


async def test_radio_never_marks_queue_ended() -> None:
    """A live source going idle never marks the queue ended."""
    radio = _item("radio", MediaType.RADIO)
    ctrl, queue, marker = _controller([radio])
    ctrl._finish_queue(queue, radio)
    marker.assert_not_called()


async def test_autoplay_never_continues_after_a_sound_effect() -> None:
    """Regression: a one-off sound effect (doorbell) as last item must not seed autoplay."""
    ctrl, queue, _marker = _controller(
        [_item("t1", MediaType.TRACK), _item("doorbell", MediaType.SOUND_EFFECT)]
    )
    queue.autoplay_enabled = True
    ctrl._fill_autoplay_next_in_series = AsyncMock()  # type: ignore[method-assign]
    ctrl._fill_autoplay_music_tracks = AsyncMock()  # type: ignore[method-assign]
    await ctrl._fill_autoplay_tracks(QUEUE_ID)
    ctrl._fill_autoplay_next_in_series.assert_not_awaited()
    ctrl._fill_autoplay_music_tracks.assert_not_awaited()
