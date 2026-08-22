"""
Tests for the notifications a plugin gets when a queue drops or hands over its AudioSource.

Clearing the queue is the one moment the owning plugin has no other signal to go on: the
stream serving the source may have been torn down long before (a paused source ends its
stream), so without this notification the plugin never learns that MA is done with it. A
transfer of a paused source is the same blind spot - it moves to another queue without a
stream request, so the plugin has to be told which queue holds it now.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import PlaybackState, QueueOption
from music_assistant_models.errors import MediaNotFoundError
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
    Build a bare controller holding a queue "q1" playing the given item, plus an empty "q2".

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
    target = PlayerQueue(queue_id="q2", active=True, display_name="Q2", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue), "q2": PlayerQueueData(queue=target)}
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


def test_replacing_or_transferring_the_queue_notifies_nothing() -> None:
    """
    Only the user clearing their queue releases the source, not the internal clear.

    Playing the same live source again and transferring the queue to another player both drop
    the queue's items on their way to re-selecting that very source. Releasing it there would
    end the upstream session and cost the user the position they were paused at. A replace
    that brings different media is a gap this leaves open, not a case it decides.
    """
    ctrl = _controller(QueueItem.from_media_item("q1", _audio_source()))
    provider = _plugin_provider(ctrl)

    ctrl._clear("q1", skip_stop=True)

    provider.on_source_removed.assert_not_called()


def test_clearing_a_queue_whose_plugin_is_gone_still_clears() -> None:
    """An unloaded plugin must not keep the user from clearing their queue."""
    ctrl = _controller(QueueItem.from_media_item("q1", _audio_source()))
    ctrl.mass.get_provider.return_value = None

    ctrl.clear("q1")

    assert ctrl._queue_data["q1"].items == []


def _ready_for_transfer(ctrl: Any, target_current_item: QueueItem | None = None) -> None:
    """
    Stub out the playback side of transfer_queue, leaving the handover itself real.

    :param ctrl: The controller to prepare.
    :param target_current_item: What the target queue "q2" is playing when the transfer lands.
    """
    ctrl.stop = AsyncMock()
    ctrl.load = AsyncMock()

    def _apply_items(queue_id: str, items: list[QueueItem]) -> None:
        ctrl._queue_data[queue_id].items = items

    # only the assignment the real one does: the queues have to end up holding what they were
    # handed, or a check on what survived the transfer reads the items it was supposed to replace
    ctrl.update_items = Mock(side_effect=_apply_items)
    ctrl._queue_data["q1"].queue.state = PlaybackState.PAUSED
    if target_current_item is not None:
        ctrl._queue_data["q2"].queue.current_item = target_current_item
        ctrl._queue_data["q2"].items = [target_current_item]
    target_player = MagicMock()
    target_player.state.active_group = None
    target_player.state.synced_to = None
    ctrl.mass.players.get_player.return_value = target_player


async def test_transferring_a_paused_source_notifies_the_plugin() -> None:
    """
    The plugin is told which queue holds its source now.

    A paused source is moved without a stream request, so nothing else would tell the plugin
    it changed hands - and every later callback would arrive for the queue it left behind.
    """
    ctrl = _controller(QueueItem.from_media_item("q1", _audio_source()))
    provider = _plugin_provider(ctrl)
    _ready_for_transfer(ctrl)

    await ctrl.transfer_queue("q1", "q2")

    provider.on_source_transferred.assert_awaited_once_with("main", "q1", "q2")


async def test_transferring_a_queue_of_tracks_notifies_nothing() -> None:
    """A queue without a live source has no plugin to hand anything over to."""
    ctrl = _controller(QueueItem.from_media_item("q1", _track()))
    provider = _plugin_provider(ctrl)
    _ready_for_transfer(ctrl)

    await ctrl.transfer_queue("q1", "q2")

    provider.on_source_transferred.assert_not_awaited()


async def test_a_plugin_raising_does_not_break_the_transfer() -> None:
    """A buggy plugin must not strand the queue halfway through a transfer."""
    ctrl = _controller(QueueItem.from_media_item("q1", _audio_source()))
    provider = _plugin_provider(ctrl)
    provider.on_source_transferred.side_effect = RuntimeError("plugin is broken")
    _ready_for_transfer(ctrl)

    await ctrl.transfer_queue("q1", "q2")

    # reaching this at all means the raise was contained; the queue still changed hands
    provider.on_source_transferred.assert_awaited_once()
    assert ctrl._queue_data["q2"].queue.current_item is not None


def _play_media_replacing_with(ctrl: Any, *items: QueueItem) -> None:
    """Stand in for the resolve/enqueue path, leaving the queue holding the given items."""
    ctrl._check_player_permission = Mock()

    async def _handle(*_args: Any, **_kwargs: Any) -> None:
        ctrl._queue_data["q1"].items = list(items)

    ctrl._handle_play_media = _handle


async def test_starting_other_media_releases_the_source_it_replaced() -> None:
    """
    Media that pushes a live source out of the queue releases it.

    Album, artist and playlist all default to "replace", so this - not the clear button - is how
    a paused live source usually leaves the queue.
    """
    ctrl = _controller(QueueItem.from_media_item("q1", _audio_source()))
    provider = _plugin_provider(ctrl)
    _play_media_replacing_with(ctrl, QueueItem.from_media_item("q1", _track()))

    await ctrl.play_media("q1", "test://album/al1", QueueOption.REPLACE)

    provider.on_source_removed.assert_called_once_with("main", "q1")


async def test_playing_a_source_selects_it_and_leaves_the_queue_alone() -> None:
    """
    A play request naming a live source selects it on the player instead of enqueueing it.

    That is the whole point of the source living on the player: the queue keeps the items
    the user had, so they are still there to resume once the source is done. Re-selecting
    the same source is what keeps its upstream session alive, and the session itself is
    what protects that now (see test_audio_source_session.py).
    """
    existing = QueueItem.from_media_item("q1", _track())
    ctrl = _controller(existing)
    provider = _plugin_provider(ctrl)
    ctrl.mass.players.select_source = AsyncMock()

    await ctrl.play_media("q1", "spotify_connect--test://audio_source/main", QueueOption.REPLACE)

    ctrl.mass.players.select_source.assert_awaited_once_with(
        "q1", "spotify_connect--test://audio_source/main"
    )
    # nothing was enqueued, released or otherwise done to the queue
    assert ctrl._queue_data["q1"].items == [existing]
    provider.on_source_removed.assert_not_called()


async def test_media_played_alongside_the_source_keeps_it() -> None:
    """An option that leaves the source in the queue is not the user done with it."""
    source_item = QueueItem.from_media_item("q1", _audio_source())
    ctrl = _controller(source_item)
    provider = _plugin_provider(ctrl)
    _play_media_replacing_with(ctrl, source_item, QueueItem.from_media_item("q1", _track()))

    await ctrl.play_media("q1", "test://album/al1", QueueOption.PLAY)

    provider.on_source_removed.assert_not_called()


async def test_media_that_fails_to_load_still_releases_the_emptied_queue() -> None:
    """
    A replace gives up the queue's items before it knows the new media is playable.

    Nothing plays afterwards and the source is just as gone, so it has to be released even
    though starting the media failed.
    """
    ctrl = _controller(QueueItem.from_media_item("q1", _audio_source()))
    provider = _plugin_provider(ctrl)
    _play_media_replacing_with(ctrl)

    async def _fail(*_args: Any, **_kwargs: Any) -> None:
        ctrl._queue_data["q1"].items = []
        raise MediaNotFoundError("No playable items found")

    ctrl._handle_play_media = _fail

    with pytest.raises(MediaNotFoundError):
        await ctrl.play_media("q1", "test://album/gone", QueueOption.REPLACE)

    provider.on_source_removed.assert_called_once_with("main", "q1")


async def test_transferring_onto_a_paused_source_releases_it() -> None:
    """
    A queue moved onto a player that was paused on a live source releases that source.

    The transfer overwrites what the target was playing, so its source leaves the queue just as
    surely as a clear would take it - and nothing else would tell its plugin.
    """
    ctrl = _controller(QueueItem.from_media_item("q1", _track()))
    provider = _plugin_provider(ctrl)
    _ready_for_transfer(ctrl, target_current_item=QueueItem.from_media_item("q2", _audio_source()))

    await ctrl.transfer_queue("q1", "q2")

    provider.on_source_removed.assert_called_once_with("main", "q2")


async def test_a_source_left_behind_by_a_play_is_still_released_later() -> None:
    """
    A source kept in the queue alongside other media is released when it does leave.

    Starting a track with "play" leaves the live source in the queue behind it, so nothing is
    released then. Clearing the queue afterwards is what finally removes it - and by then it is
    no longer the item the queue is playing.
    """
    source_item = QueueItem.from_media_item("q1", _audio_source())
    ctrl = _controller(source_item)
    provider = _plugin_provider(ctrl)
    # as a "play" leaves it: the source still queued, a track playing in front of it
    track_item = QueueItem.from_media_item("q1", _track())
    ctrl._queue_data["q1"].items = [source_item, track_item]
    ctrl._queue_data["q1"].queue.current_item = track_item

    ctrl.clear("q1")

    provider.on_source_removed.assert_called_once_with("main", "q1")


async def test_a_source_left_behind_by_a_play_is_released_when_replaced() -> None:
    """The same source, no longer playing, is released when new media takes the queue over."""
    source_item = QueueItem.from_media_item("q1", _audio_source())
    ctrl = _controller(source_item)
    provider = _plugin_provider(ctrl)
    track_item = QueueItem.from_media_item("q1", _track())
    ctrl._queue_data["q1"].items = [source_item, track_item]
    ctrl._queue_data["q1"].queue.current_item = track_item
    _play_media_replacing_with(ctrl, QueueItem.from_media_item("q1", _track()))

    await ctrl.play_media("q1", "test://album/al1", QueueOption.REPLACE)

    provider.on_source_removed.assert_called_once_with("main", "q1")


async def test_transferring_hands_over_a_source_that_was_not_playing() -> None:
    """
    Every live source the queue holds moves with it, not just the one that was playing.

    A source left in the queue by a "play" travels to the target like any other item, so its
    plugin has to follow it there - otherwise a later clear on the target releases nothing.
    """
    source_item = QueueItem.from_media_item("q1", _audio_source())
    track_item = QueueItem.from_media_item("q1", _track())
    ctrl = _controller(track_item)
    provider = _plugin_provider(ctrl)
    ctrl._queue_data["q1"].items = [source_item, track_item]
    _ready_for_transfer(ctrl)

    await ctrl.transfer_queue("q1", "q2")

    provider.on_source_transferred.assert_awaited_once_with("main", "q1", "q2")
