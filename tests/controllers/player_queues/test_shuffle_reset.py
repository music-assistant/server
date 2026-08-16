"""
Tests for the shuffle state a newly started media item ends up with.

A shuffle left switched on by an earlier listening session must not silently reorder the album the
user just picked, while a shuffle the user switched on moments before pressing play is a deliberate
"shuffle this" gesture and has to be honoured. Only replacing the queue starts such a new listening
session; the options that enqueue onto the running queue keep its shuffle state. These drive the
real ``play_media`` path against a bare controller instance, mirroring ``test_user_initiated_plays``
and ``test_enqueue_options``: resolution and playback are stubbed, but the enqueue/load path runs
for real so the resulting item order is verified end-to-end.
"""

from __future__ import annotations

import time
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, QueueOption
from music_assistant_models.media_items import (
    Album,
    ItemMapping,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.constants import SHUFFLE_INTENT_WINDOW
from music_assistant.controllers.player_queues.state import PlayerQueueData

# the album the user starts, in its own track order
ALBUM_TRACKS = ["t1", "t2", "t3", "t4"]


def _track(item_id: str) -> Track:
    """Build a playable Track on the 'test' provider."""
    return Track(
        item_id=item_id,
        provider="test",
        name=f"Track {item_id}",
        duration=60,
        artists=UniqueList(
            [ItemMapping(item_id="a", provider="test", name="A", media_type=MediaType.ARTIST)]
        ),
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def _album() -> Album:
    """Build the Album the user presses play on (its configured enqueue default is 'replace')."""
    return Album(
        item_id="al1",
        provider="test",
        name="Album al1",
        provider_mappings={
            ProviderMapping(item_id="al1", provider_domain="test", provider_instance="test")
        },
    )


def _dynamic_playlist() -> Playlist:
    """Build a dynamic playlist: a source that supplies its own tracks and is always a smart mix."""
    playlist = Playlist(
        item_id="dyn1",
        provider="test",
        name="Dynamic",
        provider_mappings={
            ProviderMapping(item_id="dyn1", provider_domain="test", provider_instance="test")
        },
    )
    playlist.is_dynamic = True
    return playlist


def _controller(**queue_kwargs: Any) -> Any:
    """
    Build a bare controller driving ``play_media`` on a single queue "q1".

    The album's tracks come from a stubbed media resolver and the shuffle is made deterministic
    (it reverses the batch), so the resulting queue order tells shuffle-on from shuffle-off.

    :param queue_kwargs: Overrides for the queue this controller is set up with.
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = MagicMock()
    ctrl.mass.players.get_player = Mock(return_value=Mock(extra_data={}))
    lock_cm = MagicMock()
    lock_cm.__aenter__ = AsyncMock(return_value=None)
    lock_cm.__aexit__ = AsyncMock(return_value=None)
    ctrl.mass.players.get_player_lock = Mock(return_value=lock_cm)
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.get_next_item = Mock(return_value=None)  # type: ignore[method-assign]
    ctrl.get_config_value = Mock(return_value=QueueOption.REPLACE.value)  # type: ignore[method-assign]
    ctrl._managed_pool = Mock()
    ctrl._managed_pool.fill = AsyncMock(
        side_effect=lambda *_args, **_kwargs: [_track(item_id) for item_id in ALBUM_TRACKS]
    )
    ctrl._smart_shuffle = Mock()
    ctrl._smart_shuffle.is_enabled = Mock(return_value=True)
    ctrl._smart_shuffle.arrange = AsyncMock(side_effect=lambda _queue, items: list(items)[::-1])
    ctrl._media_resolver = Mock()
    ctrl._media_resolver._resolve_media_items = AsyncMock(
        side_effect=lambda *_args, **_kwargs: [_track(item_id) for item_id in ALBUM_TRACKS]
    )
    queue = PlayerQueue(
        queue_id="q1", active=True, display_name="Q1", available=True, items=0, **queue_kwargs
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    return ctrl


def _queue(ctrl: Any) -> PlayerQueue:
    """Return the controller's queue."""
    return cast("PlayerQueue", ctrl._queue_data["q1"].queue)


def _played_order(ctrl: Any) -> list[str]:
    """Return the item ids of the tracks currently loaded in the queue, in play order."""
    return [
        item.media_item.item_id
        for item in ctrl._queue_data["q1"].items
        if item.media_item is not None
    ]


async def test_shuffle_left_on_by_previous_session_is_reset() -> None:
    """An album started on a queue that still had shuffle on plays in its own track order."""
    ctrl = _controller(shuffle_enabled=True, smart_shuffle_active=True)

    await ctrl.play_media("q1", _album(), QueueOption.REPLACE)

    assert _queue(ctrl).shuffle_enabled is False
    assert _queue(ctrl).smart_shuffle_active is False
    # the album is played front to back, not in the (reversed) shuffle order
    assert _played_order(ctrl) == ALBUM_TRACKS
    # shuffle was settled before the items were resolved: a shuffled queue asks the resolver to
    # keep the items preceding a chosen track, an in-order one does not
    resolve_call = ctrl._media_resolver._resolve_media_items.call_args
    assert resolve_call.kwargs["keep_preceding_items"] is False


async def test_shuffle_left_on_is_reset_for_a_derived_enqueue_option() -> None:
    """The reset also applies when the enqueue option comes from the media type's config default."""
    ctrl = _controller(shuffle_enabled=True)

    await ctrl.play_media("q1", _album())

    # the option was derived from the album's configured default (which is 'replace')
    assert ctrl.get_config_value.call_args.args[0] == "default_enqueue_option_album"
    assert _queue(ctrl).shuffle_enabled is False
    assert _played_order(ctrl) == ALBUM_TRACKS


async def test_shuffle_just_switched_on_by_the_user_is_honoured() -> None:
    """Switching shuffle on and then pressing play shuffles the album the user picked."""
    ctrl = _controller()
    await ctrl.set_shuffle("q1", True)

    await ctrl.play_media("q1", _album(), QueueOption.REPLACE)

    assert _queue(ctrl).shuffle_enabled is True
    assert _played_order(ctrl) == ALBUM_TRACKS[::-1]


async def test_shuffle_intent_is_only_good_for_one_play() -> None:
    """The toggle carries into the album the user starts next, but not into the one after it."""
    ctrl = _controller()
    await ctrl.set_shuffle("q1", True)
    await ctrl.play_media("q1", _album(), QueueOption.REPLACE)

    await ctrl.play_media("q1", _album(), QueueOption.REPLACE)

    assert ctrl._queue_data["q1"].shuffle_set_at is None
    assert _queue(ctrl).shuffle_enabled is False
    assert _played_order(ctrl) == ALBUM_TRACKS


async def test_switching_shuffle_off_drops_the_intent() -> None:
    """Turning shuffle back off leaves no intent behind for the next play to pick up."""
    ctrl = _controller()
    await ctrl.set_shuffle("q1", True)
    await ctrl.set_shuffle("q1", False)

    await ctrl.play_media("q1", _album(), QueueOption.REPLACE)

    assert ctrl._queue_data["q1"].shuffle_set_at is None
    assert _queue(ctrl).shuffle_enabled is False
    assert _played_order(ctrl) == ALBUM_TRACKS


async def test_stale_shuffle_intent_is_not_honoured() -> None:
    """A toggle from well before the play command is a leftover, not intent for this album."""
    ctrl = _controller(shuffle_enabled=True)
    ctrl._queue_data["q1"].shuffle_set_at = time.monotonic() - SHUFFLE_INTENT_WINDOW - 100

    await ctrl.play_media("q1", _album(), QueueOption.REPLACE)

    assert _queue(ctrl).shuffle_enabled is False
    assert _played_order(ctrl) == ALBUM_TRACKS


async def test_explicit_shuffle_request_shuffles_an_unshuffled_queue() -> None:
    """A caller asking for a shuffled play gets one, without having to toggle shuffle first."""
    ctrl = _controller()

    await ctrl.play_media("q1", _album(), QueueOption.REPLACE, shuffle=True)

    assert _queue(ctrl).shuffle_enabled is True
    assert _played_order(ctrl) == ALBUM_TRACKS[::-1]


async def test_explicit_no_shuffle_wins_over_a_fresh_intent() -> None:
    """An explicit "play in order" beats the shuffle the user switched on moments earlier."""
    ctrl = _controller()
    await ctrl.set_shuffle("q1", True)

    await ctrl.play_media("q1", _album(), QueueOption.REPLACE, shuffle=False)

    assert _queue(ctrl).shuffle_enabled is False
    assert _played_order(ctrl) == ALBUM_TRACKS


async def test_dynamic_source_overrides_an_explicit_play_in_order() -> None:
    """A dynamic source is an always-on smart mix, so it outranks an explicit "play in order"."""
    ctrl = _controller()

    await ctrl.play_media("q1", _dynamic_playlist(), QueueOption.REPLACE, shuffle=False)

    assert _queue(ctrl).is_dynamic is True
    assert _queue(ctrl).shuffle_enabled is True
    assert _queue(ctrl).smart_shuffle_active is True


async def test_replacing_a_dynamic_queue_drops_the_smart_mix_indicator() -> None:
    """
    An album started over a dynamic queue is a plain queue, so it must not report a smart mix.

    The dynamic source is what made smart shuffle active here (the per-queue setting is off), so
    dropping it has to take the indicator with it.
    """
    ctrl = _controller(shuffle_enabled=True, smart_shuffle_active=True, is_dynamic=True)
    ctrl._smart_shuffle.is_enabled = Mock(return_value=False)

    await ctrl.play_media("q1", _album(), QueueOption.REPLACE, shuffle=True)

    assert _queue(ctrl).is_dynamic is False
    # the caller asked for a shuffled play, but a plain random shuffle is not a smart mix
    assert _queue(ctrl).shuffle_enabled is True
    assert _queue(ctrl).smart_shuffle_active is False


@pytest.mark.parametrize("option", [QueueOption.PLAY, QueueOption.NEXT, QueueOption.REPLACE_NEXT])
async def test_starting_media_on_an_ended_queue_resets_shuffle(option: QueueOption) -> None:
    """
    A queue that played to its end is discarded by every option but add, so the shuffle goes too.

    The items of a finished queue are not kept, so there is nothing left in shuffled order for the
    flag to contradict: this is a new listening session just as much as a replace is.
    """
    ctrl = _controller(shuffle_enabled=True, ended=True)

    await ctrl.play_media("q1", _album(), option)

    assert _queue(ctrl).shuffle_enabled is False
    assert _played_order(ctrl) == ALBUM_TRACKS


async def test_adding_onto_an_ended_queue_keeps_shuffle() -> None:
    """Adding continues a finished queue rather than starting over, so its shuffle stays on."""
    ctrl = _controller(shuffle_enabled=True, ended=True)
    # a marker rather than a realistic timestamp: it only has to show the intent was left alone
    ctrl._queue_data["q1"].shuffle_set_at = 12345.0

    await ctrl.play_media("q1", _album(), QueueOption.ADD)

    assert _queue(ctrl).shuffle_enabled is True
    assert _played_order(ctrl) == ALBUM_TRACKS[::-1]
    assert ctrl._queue_data["q1"].shuffle_set_at == 12345.0


@pytest.mark.parametrize("shuffle_enabled", [True, False])
@pytest.mark.parametrize(
    "option", [QueueOption.PLAY, QueueOption.ADD, QueueOption.NEXT, QueueOption.REPLACE_NEXT]
)
async def test_enqueueing_leaves_shuffle_untouched(
    option: QueueOption, shuffle_enabled: bool
) -> None:
    """
    Only replacing the queue starts a new listening session; every other option keeps its shuffle.

    These all keep (part of) the existing queue, whose items are already in shuffled order, so
    switching shuffle off here would leave those items contradicting the queue's own flag.
    """
    # a running queue, as opposed to the finished one every option but add starts over from
    ctrl = _controller(shuffle_enabled=shuffle_enabled, ended=False)
    # a marker rather than a realistic timestamp: it only has to show the intent was left alone
    ctrl._queue_data["q1"].shuffle_set_at = 12345.0

    await ctrl.play_media("q1", _album(), option)

    assert _queue(ctrl).shuffle_enabled is shuffle_enabled
    # the intent is not consumed either: it still belongs to the next media the user starts
    assert ctrl._queue_data["q1"].shuffle_set_at == 12345.0


def test_clear_command_resets_shuffle() -> None:
    """Clearing the queue is an explicit "start over", so the shuffle goes with the content."""
    ctrl = _controller(shuffle_enabled=True, smart_shuffle_active=True)
    ctrl._queue_data["q1"].items = [
        QueueItem.from_media_item("q1", _track(item_id)) for item_id in ALBUM_TRACKS
    ]

    ctrl.clear("q1")

    assert _queue(ctrl).shuffle_enabled is False
    assert _queue(ctrl).smart_shuffle_active is False
    assert ctrl._queue_data["q1"].shuffle_set_at is None


def test_empty_queue_reaching_its_end_keeps_shuffle() -> None:
    """An end reached with nothing to replay is still not the user clearing the queue."""
    ctrl = _controller(shuffle_enabled=True)

    ctrl.mark_ended("q1")

    assert ctrl._queue_data["q1"].items == []
    assert _queue(ctrl).shuffle_enabled is True
