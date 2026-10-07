"""Tests for ``PlaybackTrackerMixin.is_playing_queue``."""

from __future__ import annotations

from unittest.mock import MagicMock, Mock

from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

BASE_URL = "http://ma.local:8097"


def _controller() -> PlayerQueuesController:
    """Create a bare controller holding queues q1 and q2 with one item each."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.streams.base_url = BASE_URL
    ctrl._queue_data = {}
    for queue_id in ("q1", "q2"):
        queue = PlayerQueue(
            queue_id=queue_id, active=True, display_name=queue_id, available=True, items=1
        )
        ctrl._queue_data[queue_id] = PlayerQueueData(
            queue=queue,
            items=[
                QueueItem(
                    queue_id=queue_id, queue_item_id=f"{queue_id}-item", name="x", duration=60
                )
            ],
        )
    return ctrl


def _player(media: PlayerMedia | None) -> MagicMock:
    """Build a native player reporting the given media."""
    player = MagicMock()
    player.active_output_protocol = "native"
    player.current_media = media
    return player


def test_item_of_the_queue_reported_by_id() -> None:
    """Media carrying the queue id and one of its item ids counts as playing the queue."""
    ctrl = _controller()
    player = _player(PlayerMedia(uri="x", source_id="q1", queue_item_id="q1-item"))

    assert ctrl.is_playing_queue("q1", player)
    assert not ctrl.is_playing_queue("q2", player)


def test_item_of_the_queue_reported_by_stream_url() -> None:
    """A stream URL of one of the queue's items counts, as a Sonos reports it."""
    ctrl = _controller()
    player = _player(PlayerMedia(uri=f"{BASE_URL}/single/sess/q1/q1-item/q1.flac"))

    assert ctrl.is_playing_queue("q1", player)


def test_flow_stream_of_the_queue_counts_even_when_its_item_is_gone() -> None:
    """A flow stream names only its first item; the queue in its path is what counts."""
    ctrl = _controller()
    player = _player(PlayerMedia(uri=f"{BASE_URL}/flow/sess/q1/gone/q1.flac"))

    assert ctrl.is_playing_queue("q1", player)
    assert not ctrl.is_playing_queue("q2", player)


def test_another_queues_stream_does_not_count() -> None:
    """A player rendering another queue's stream is not playing this queue."""
    ctrl = _controller()
    player = _player(PlayerMedia(uri=f"{BASE_URL}/single/sess/q1/q1-item/q1.flac"))

    assert not ctrl.is_playing_queue("q2", player)


def test_item_no_longer_in_the_queue_does_not_count() -> None:
    """An item id the queue no longer holds (after a replace) does not count."""
    ctrl = _controller()
    player = _player(PlayerMedia(uri="x", source_id="q1", queue_item_id="gone"))

    assert not ctrl.is_playing_queue("q1", player)


def test_no_media_does_not_count() -> None:
    """A player without reported media is not playing any queue."""
    ctrl = _controller()

    assert not ctrl.is_playing_queue("q1", _player(None))
