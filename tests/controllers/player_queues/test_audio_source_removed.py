"""
Tests for the notification a plugin gets when a queue drops its live AudioSource.

Clearing the queue is the one moment the owning plugin has no other signal to go on: the
stream serving the source may have been torn down long before (a paused source ends its
stream), so without this notification the plugin never learns that MA is done with it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, Mock

from music_assistant_models.media_items import AudioSource, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.models.plugin import PluginProvider


def _audio_source() -> AudioSource:
    """Build the live AudioSource a plugin exposes."""
    return AudioSource(
        item_id="main",
        provider="spotify_connect--test",
        name="Spotify Connect",
        provider_mappings={
            ProviderMapping(
                item_id="main",
                provider_domain="spotify_connect",
                provider_instance="spotify_connect--test",
            )
        },
    )


def _track() -> Track:
    """Build a plain track, i.e. a current item with no plugin behind it."""
    return Track(
        item_id="t1",
        provider="test",
        name="Track t1",
        provider_mappings={
            ProviderMapping(item_id="t1", provider_domain="test", provider_instance="test")
        },
    )


def _controller(current_item: QueueItem | None) -> Any:
    """
    Build a bare controller holding a single queue "q1" playing the given item.

    Tasks created along the way are closed instead of scheduled, so the notification can be
    asserted on the plugin mock without a running loop.

    :param current_item: The item the queue has as its current one, None for an empty queue.
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = MagicMock()
    ctrl.mass.create_task.side_effect = lambda coroutine: coroutine.close()
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl._managed_pool = Mock()
    ctrl._smart_shuffle = Mock()
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    queue.current_item = current_item
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    if current_item is not None:
        ctrl._queue_data["q1"].items = [current_item]
        queue.items = 1
    return ctrl


def _plugin_provider(ctrl: Any) -> Any:
    """Make the controller resolve the AudioSource's provider to a plugin provider."""
    provider = MagicMock(spec=PluginProvider)
    ctrl.mass.get_provider.return_value = provider
    return provider


def test_clearing_the_queue_notifies_the_plugin() -> None:
    """The plugin owning the current AudioSource is told the queue dropped it."""
    ctrl = _controller(QueueItem.from_media_item("q1", _audio_source()))
    provider = _plugin_provider(ctrl)

    ctrl.clear("q1")

    provider.on_source_removed.assert_called_once_with("main", "q1")


def test_clearing_a_queue_of_tracks_notifies_nothing() -> None:
    """A regular track has no plugin behind it to notify."""
    ctrl = _controller(QueueItem.from_media_item("q1", _track()))
    provider = _plugin_provider(ctrl)

    ctrl.clear("q1")

    provider.on_source_removed.assert_not_called()


def test_clearing_an_empty_queue_notifies_nothing() -> None:
    """A queue with nothing playing has no source to release."""
    ctrl = _controller(None)
    provider = _plugin_provider(ctrl)

    ctrl.clear("q1")

    provider.on_source_removed.assert_not_called()


def test_clearing_a_queue_whose_plugin_is_gone_still_clears() -> None:
    """An unloaded plugin must not keep the user from clearing their queue."""
    ctrl = _controller(QueueItem.from_media_item("q1", _audio_source()))
    ctrl.mass.get_provider.return_value = None

    ctrl.clear("q1")

    assert ctrl._queue_data["q1"].items == []
