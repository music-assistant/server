"""Tests for ``PlaybackTrackerMixin.is_playing_queue``."""

from __future__ import annotations

from unittest.mock import MagicMock, Mock

from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

BASE_URL = "http://ma.local:8097"


def _controller() -> PlayerQueuesController:
    """Create a bare controller holding streaming queues q1 and q2 with one item each."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.streams.base_url = BASE_URL
    ctrl._queue_data = {}
    for queue_id in ("q1", "q2"):
        queue = PlayerQueue(
            queue_id=queue_id,
            active=True,
            display_name=queue_id,
            available=True,
            items=1,
            state=PlaybackState.PLAYING,
        )
        ctrl._queue_data[queue_id] = PlayerQueueData(
            queue=queue,
            items=[
                QueueItem(
                    queue_id=queue_id, queue_item_id=f"{queue_id}-item", name="x", duration=60
                )
            ],
            session_id=f"{queue_id}-session",
        )
    return ctrl


def _player(media: PlayerMedia | None) -> MagicMock:
    """Build a native player reporting the given media."""
    player = MagicMock()
    player.active_output_protocol = "native"
    player.current_media = media
    return player


def test_item_of_the_queue_reported_by_id() -> None:
    """Media carrying the queue id counts for that queue and against another."""
    ctrl = _controller()
    player = _player(PlayerMedia(uri="x", source_id="q1", queue_item_id="q1-item"))

    assert ctrl.is_playing_queue("q1", player)
    assert not ctrl.is_playing_queue("q2", player)


def test_stream_url_names_the_queue() -> None:
    """A stream URL of the queue counts, as a Sonos reports it; another queue's does not."""
    ctrl = _controller()
    player = _player(PlayerMedia(uri=f"{BASE_URL}/single/sess/q1/q1-item/q1.flac"))

    assert ctrl.is_playing_queue("q1", player)
    assert not ctrl.is_playing_queue("q2", player)


def test_stream_of_the_queue_with_a_removed_item_still_counts() -> None:
    """A removed item (after a replace, or a flow stream's first item) is not another queue."""
    ctrl = _controller()
    single = _player(PlayerMedia(uri=f"{BASE_URL}/single/sess/q1/gone/q1.flac"))
    flow = _player(PlayerMedia(uri=f"{BASE_URL}/flow/sess/q1/gone/q1.flac"))

    assert ctrl.is_playing_queue("q1", single)
    assert ctrl.is_playing_queue("q1", flow)
    assert not ctrl.is_playing_queue("q2", flow)


def test_mass_uri_names_the_queue() -> None:
    """The Sonos container uri `mass:<queue>` names the queue."""
    ctrl = _controller()
    player = _player(PlayerMedia(uri="mass:q1:q1-item"))

    assert ctrl.is_playing_queue("q1", player)
    assert not ctrl.is_playing_queue("q2", player)


def test_no_reported_media_counts_as_playing_the_streamed_queue() -> None:
    """A provider that reports no media gives no reason to doubt the queue it is streamed."""
    ctrl = _controller()

    assert ctrl.is_playing_queue("q1", _player(None))
    assert ctrl.is_playing_queue("q1", _player(PlayerMedia(uri="http://elsewhere/radio")))


def test_media_naming_an_unknown_queue_is_not_evidence() -> None:
    """Only a queue this server has can veto: `mass:unknown` or a foreign id is unknown media."""
    ctrl = _controller()

    assert ctrl.is_playing_queue("q1", _player(PlayerMedia(uri="mass:unknown")))
    assert ctrl.is_playing_queue(
        "q1", _player(PlayerMedia(uri="x", source_id="other-server-queue", queue_item_id="i"))
    )
    assert ctrl.is_playing_queue(
        "q1", _player(PlayerMedia(uri=f"{BASE_URL}/single/sess/not-a-queue/item/p.flac"))
    )


def test_queue_that_is_not_playing_needs_the_media_to_name_it() -> None:
    """A queue not (yet) playing is rendered only when the player's own report names it."""
    ctrl = _controller()
    ctrl._queue_data["q1"].queue.state = PlaybackState.IDLE
    own = _player(PlayerMedia(uri=f"{BASE_URL}/single/sess/q1/q1-item/q1.flac"))
    other = _player(PlayerMedia(uri=f"{BASE_URL}/single/sess/q2/q2-item/q2.flac"))

    # the queue state lags the player by the reconciliation delay; the report is fresher
    assert ctrl.is_playing_queue("q1", own)
    assert not ctrl.is_playing_queue("q1", other)
    assert not ctrl.is_playing_queue("q1", _player(None))
