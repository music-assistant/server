"""
Tests for queue-command delegation to a queue-capable AudioSource.

When the queue's current item is an AudioSource declaring ``queue_capabilities``, the
external session owns the queue: shuffle/repeat/next/previous/seek are forwarded to the
owning plugin (the mirrored options event updates the queue state afterwards), the
read-only mirrored items refuse move/delete, clear releases the session first, and
``queue_owner`` tells clients who owns the ordering. A transport-only AudioSource
(no ``queue_capabilities``) keeps the exact pre-delegation behavior.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import (
    EventType,
    PlaybackState,
    ProviderFeature,
    RepeatMode,
    SourceControl,
)
from music_assistant_models.errors import InvalidCommand
from music_assistant_models.media_items import (
    AudioSource,
    ProviderMapping,
    SourceQueueCapabilities,
)
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.models.plugin import PluginProvider

QUEUE_ID = "q1"
SOURCE_ID = "main"
INSTANCE_ID = "spotify_connect--test"


def _capabilities(**overrides: Any) -> SourceQueueCapabilities:
    """Build a full queue-capability declaration, with optional field overrides."""
    caps = SourceQueueCapabilities(
        provider_domain="spotify",
        can_shuffle=True,
        can_repeat=True,
        provides_queue_view=True,
    )
    for key, value in overrides.items():
        setattr(caps, key, value)
    return caps


def _audio_source(caps: SourceQueueCapabilities | None) -> AudioSource:
    """Build the live AudioSource queue item payload with the given queue capabilities."""
    return AudioSource(
        item_id=SOURCE_ID,
        provider=INSTANCE_ID,
        name="Spotify Connect",
        provider_mappings={
            ProviderMapping(
                item_id=SOURCE_ID,
                provider_domain="spotify_connect",
                provider_instance=INSTANCE_ID,
            )
        },
        can_play_pause=True,
        can_seek=True,
        can_next_previous=True,
        queue_capabilities=caps,
    )


def _controller(
    current_item: AudioSource | None, **queue_kwargs: Any
) -> tuple[PlayerQueuesController, MagicMock]:
    """
    Build a bare controller with a single queue playing the given AudioSource.

    Returns the controller and the (spec'd) PluginProvider mock owning the source, so
    tests can assert what was (not) forwarded through ``on_source_control``.
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    ctrl.mass = MagicMock()
    ctrl.mass.create_task = Mock(
        side_effect=lambda coro, **_kw: coro.close() if asyncio.iscoroutine(coro) else MagicMock()
    )
    lock_cm = MagicMock()
    lock_cm.__aenter__ = AsyncMock(return_value=None)
    lock_cm.__aexit__ = AsyncMock(return_value=None)
    ctrl.mass.players.get_player_lock = Mock(return_value=lock_cm)
    ctrl.mass.players.get_player = Mock(return_value=MagicMock(extra_data={}))
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.load = AsyncMock()  # type: ignore[method-assign]
    ctrl._smart_shuffle = Mock()
    ctrl._smart_shuffle.is_enabled = Mock(return_value=False)
    ctrl._managed_pool = MagicMock()
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Q1", available=True, items=0, **queue_kwargs
    )
    queue_data = PlayerQueueData(queue=queue)
    ctrl._queue_data = {QUEUE_ID: queue_data}
    if current_item is not None:
        item = QueueItem.from_media_item(QUEUE_ID, current_item)
        queue_data.items = [item]
        queue.items = 1
        queue.current_index = 0
        queue.current_item = item
    provider = MagicMock(spec=PluginProvider)
    provider.supported_features = {ProviderFeature.AUDIO_SOURCE}
    ctrl.mass.get_provider = Mock(return_value=provider)
    return ctrl, provider


def _queue(ctrl: PlayerQueuesController) -> PlayerQueue:
    """Return the controller's queue."""
    return ctrl._queue_data[QUEUE_ID].queue


async def test_delegated_shuffle_forwards_without_touching_the_queue() -> None:
    """Shuffle on a delegated queue reaches the plugin; the MA queue state is left alone."""
    ctrl, provider = _controller(_audio_source(_capabilities()))

    await ctrl.set_shuffle(QUEUE_ID, True)

    provider.on_source_control.assert_awaited_once_with(SOURCE_ID, SourceControl.SHUFFLE, True)
    # the mirrored options event updates the state, not the command itself
    assert _queue(ctrl).shuffle_enabled is False
    ctrl.load.assert_not_awaited()  # type: ignore[attr-defined]


async def test_delegated_repeat_forwards_the_repeat_mode() -> None:
    """Repeat on a delegated queue reaches the plugin with the RepeatMode as payload."""
    ctrl, provider = _controller(_audio_source(_capabilities()))

    await ctrl.set_repeat(QUEUE_ID, RepeatMode.ALL)

    provider.on_source_control.assert_awaited_once_with(
        SOURCE_ID, SourceControl.REPEAT, RepeatMode.ALL
    )
    assert _queue(ctrl).repeat_mode == RepeatMode.OFF


async def test_shuffle_refused_when_the_session_cannot_shuffle() -> None:
    """A session without shuffle support refuses the toggle instead of mutating the queue."""
    ctrl, provider = _controller(_audio_source(_capabilities(can_shuffle=False)))

    with pytest.raises(InvalidCommand):
        await ctrl.set_shuffle(QUEUE_ID, True)

    provider.on_source_control.assert_not_awaited()
    assert _queue(ctrl).shuffle_enabled is False


async def test_repeat_refused_when_the_session_cannot_repeat() -> None:
    """A session without repeat support refuses the command instead of mutating the queue."""
    ctrl, provider = _controller(_audio_source(_capabilities(can_repeat=False)))

    with pytest.raises(InvalidCommand):
        await ctrl.set_repeat(QUEUE_ID, RepeatMode.ALL)

    provider.on_source_control.assert_not_awaited()
    assert _queue(ctrl).repeat_mode == RepeatMode.OFF


async def test_delegated_next_forwards_instead_of_walking_the_queue() -> None:
    """player_queues/next on a delegated queue skips within the session."""
    ctrl, provider = _controller(_audio_source(_capabilities()))

    await ctrl.next(QUEUE_ID)

    provider.on_source_control.assert_awaited_once_with(SOURCE_ID, SourceControl.NEXT)
    # no MA index walk: the session is the only item and stays current
    assert _queue(ctrl).current_index == 0
    ctrl.play_index.assert_not_awaited()  # type: ignore[attr-defined]


async def test_delegated_previous_forwards_instead_of_walking_the_queue() -> None:
    """player_queues/previous on a delegated queue skips within the session."""
    ctrl, provider = _controller(_audio_source(_capabilities()))

    await ctrl.previous(QUEUE_ID)

    provider.on_source_control.assert_awaited_once_with(SOURCE_ID, SourceControl.PREVIOUS)
    assert _queue(ctrl).current_index == 0
    ctrl.play_index.assert_not_awaited()  # type: ignore[attr-defined]


async def test_delegated_seek_forwards_without_requiring_a_duration() -> None:
    """Seek forwards the absolute position even though the live source has no duration."""
    ctrl, provider = _controller(_audio_source(_capabilities()))

    await ctrl.seek(QUEUE_ID, 42)

    provider.on_source_control.assert_awaited_once_with(SOURCE_ID, SourceControl.SEEK, 42)
    ctrl.play_index.assert_not_awaited()  # type: ignore[attr-defined]


async def test_delegated_skip_forwards_the_absolute_position() -> None:
    """Skip translates its relative offset to an absolute in-session seek."""
    ctrl, provider = _controller(_audio_source(_capabilities()))
    _queue(ctrl).elapsed_time = 5

    await ctrl.skip(QUEUE_ID, 10)

    provider.on_source_control.assert_awaited_once_with(SOURCE_ID, SourceControl.SEEK, 15)


async def test_move_and_delete_refused_on_a_delegated_queue() -> None:
    """The mirrored queue is read-only: Spotify has no remove/reorder API."""
    ctrl, _provider = _controller(_audio_source(_capabilities()))

    with pytest.raises(InvalidCommand):
        ctrl.move_item(QUEUE_ID, "some_item")
    with pytest.raises(InvalidCommand):
        ctrl.move_item_end(QUEUE_ID, "some_item")
    with pytest.raises(InvalidCommand):
        ctrl.delete_item(QUEUE_ID, "some_item")


async def test_clear_on_a_delegated_queue_stops_first() -> None:
    """Clear releases the live session through the stop path before wiping the queue."""
    ctrl, _provider = _controller(_audio_source(_capabilities()))
    _queue(ctrl).state = PlaybackState.PLAYING
    ctrl.stop = AsyncMock()  # type: ignore[method-assign]

    await ctrl.clear(QUEUE_ID)

    ctrl.stop.assert_awaited_once_with(QUEUE_ID)
    assert ctrl._queue_data[QUEUE_ID].items == []
    assert _queue(ctrl).current_item is None


async def test_clear_on_a_normal_queue_keeps_the_background_stop() -> None:
    """A non-delegated clear keeps stopping through _clear's background task, as before."""
    ctrl, _provider = _controller(_audio_source(None))
    _queue(ctrl).state = PlaybackState.PLAYING
    ctrl.stop = AsyncMock()  # type: ignore[method-assign]

    await ctrl.clear(QUEUE_ID)

    # dispatched as a task by _clear rather than awaited up front
    assert ctrl.stop.await_count == 0
    assert ctrl.stop.call_count == 1
    assert ctrl._queue_data[QUEUE_ID].items == []


async def test_queue_owner_set_while_delegated() -> None:
    """The emitted queue carries the owning AudioSource uri while delegated."""
    source = _audio_source(_capabilities())
    ctrl, _provider = _controller(source)

    ctrl.signal_update(QUEUE_ID)

    assert _queue(ctrl).queue_owner == str(source.uri)
    # the QUEUE_UPDATED event carries the owner for clients
    event_call = next(
        call
        for call in ctrl.mass.signal_event.call_args_list  # type: ignore[attr-defined]
        if call.args and call.args[0] == EventType.QUEUE_UPDATED
    )
    assert event_call.kwargs["data"].queue_owner == str(source.uri)


async def test_queue_owner_cleared_for_a_transport_only_source() -> None:
    """A transport-only AudioSource does not delegate, so the queue stays MA-owned."""
    ctrl, _provider = _controller(_audio_source(None))

    ctrl.signal_update(QUEUE_ID)

    assert _queue(ctrl).queue_owner is None


async def test_queue_owner_cleared_when_the_provider_is_gone() -> None:
    """A vanished plugin provider ends the delegation, whatever the item declares."""
    ctrl, _provider = _controller(_audio_source(_capabilities()))
    ctrl.mass.get_provider = Mock(return_value=None)  # type: ignore[method-assign]

    ctrl.signal_update(QUEUE_ID)

    assert _queue(ctrl).queue_owner is None


async def test_transport_only_source_keeps_normal_queue_behavior() -> None:
    """Regression: without queue_capabilities nothing is forwarded to the plugin."""
    ctrl, provider = _controller(_audio_source(None))

    await ctrl.set_shuffle(QUEUE_ID, True)
    await ctrl.set_repeat(QUEUE_ID, RepeatMode.ALL)
    await ctrl.next(QUEUE_ID)
    ctrl.delete_item(QUEUE_ID, 0)

    provider.on_source_control.assert_not_awaited()
    # the commands applied to the MA queue itself
    assert _queue(ctrl).shuffle_enabled is True
    assert _queue(ctrl).repeat_mode == RepeatMode.ALL
    assert ctrl._queue_data[QUEUE_ID].items == []
