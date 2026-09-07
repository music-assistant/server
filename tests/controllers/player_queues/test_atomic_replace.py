"""
Tests that replacing a queue's contents swaps them in one step.

A replace used to empty the queue up front and tear its audio down, then resolve the new media
over the network before loading it - so clients saw an empty queue with nothing playing for as
long as the providers took to answer. The queue now keeps playing its current content until the
new items are ready, and hands the audio over in a single swap.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.enums import MediaType, PlaybackState, QueueOption
from music_assistant_models.media_items import (
    ItemMapping,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

NEW_TRACKS = ["n1", "n2", "n3"]
PLAYING_TRACKS = ["p1", "p2", "p3"]


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


def _playlist(item_id: str = "pl1", *, dynamic: bool = False) -> Playlist:
    """Build a playlist, optionally a dynamic one (an always-on smart mix)."""
    playlist = Playlist(
        item_id=item_id,
        provider="test",
        name=f"Playlist {item_id}",
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )
    playlist.is_dynamic = dynamic
    return playlist


def _controller(**queue_kwargs: Any) -> Any:
    """Build a bare controller whose queue "q1" is playing, with the resolver stubbed."""
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
        side_effect=lambda *_a, **_kw: [_track(item_id) for item_id in NEW_TRACKS]
    )
    ctrl._smart_shuffle = Mock()
    ctrl._smart_shuffle.is_enabled = Mock(return_value=False)
    ctrl._media_resolver = Mock()
    ctrl._media_resolver._resolve_media_items = AsyncMock(
        side_effect=lambda *_a, **_kw: [_track(item_id) for item_id in NEW_TRACKS]
    )
    queue = PlayerQueue(
        queue_id="q1", active=True, display_name="Q1", available=True, items=0, **queue_kwargs
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    return ctrl


def _queue(ctrl: Any) -> PlayerQueue:
    """Return the controller's queue."""
    return cast("PlayerQueue", ctrl._queue_data["q1"].queue)


def _item_ids(ctrl: Any) -> list[str]:
    """Return the item ids of the tracks currently loaded in the queue, in play order."""
    return [
        item.media_item.item_id
        for item in ctrl._queue_data["q1"].items
        if item.media_item is not None
    ]


def _load_playing_queue(ctrl: Any, *, with_buffers: bool = False) -> list[QueueItem]:
    """Fill the queue with items as if it were playing its first one."""
    items = [QueueItem.from_media_item("q1", _track(item_id)) for item_id in PLAYING_TRACKS]
    if with_buffers:
        for item in items:
            item.streamdetails = MagicMock()
            item.streamdetails.buffer = MagicMock()
            item.streamdetails.buffer.clear = AsyncMock()
    ctrl._queue_data["q1"].items = items
    queue = _queue(ctrl)
    queue.items = len(items)
    queue.state = PlaybackState.PLAYING
    queue.current_index = 0
    queue.current_item = items[0]
    queue.index_in_buffer = 0
    return items


def _observe_item_counts(ctrl: Any) -> list[int]:
    """Record the number of items every queue-items update publishes."""
    counts: list[int] = []
    original = PlayerQueuesController.update_items

    def _spy(queue_id: str, queue_items: list[QueueItem]) -> None:
        counts.append(len(queue_items))
        original(ctrl, queue_id, queue_items)

    ctrl.update_items = _spy
    return counts


async def test_replace_never_advertises_an_empty_queue() -> None:
    """
    The swap goes out as a single update, so clients never see the queue empty in between.

    The empty state used to be published before the new media was resolved, which meant it stayed
    on screen for as long as the providers took to answer.
    """
    ctrl = _controller()
    _load_playing_queue(ctrl)
    counts = _observe_item_counts(ctrl)

    await ctrl.play_media("q1", _playlist(), QueueOption.REPLACE)

    assert counts == [len(NEW_TRACKS)]
    assert _item_ids(ctrl) == NEW_TRACKS


async def test_replace_releases_the_audio_of_the_items_it_swapped_out() -> None:
    """
    The outgoing items hand their buffers back once the new item has taken over.

    Nothing reaches an item after the swap, so a buffer left attached would keep its producer -
    and the provider stream slot behind it - alive until its own inactivity timeout expires.
    """
    ctrl = _controller()
    replaced = _load_playing_queue(ctrl, with_buffers=True)
    details = [cast("Any", item.streamdetails) for item in replaced]
    order: list[str] = []
    for detail in details:
        detail.buffer.clear = AsyncMock(side_effect=lambda: order.append("release"))
    buffers = [detail.buffer for detail in details]
    ctrl.play_index = AsyncMock(side_effect=lambda *_a, **_kw: order.append("play"))

    await ctrl.play_media("q1", _playlist(), QueueOption.REPLACE)

    for buffer in buffers:
        buffer.clear.assert_awaited_once()
    assert all(detail.buffer is None for detail in details)
    # the source slot the outgoing item holds has to be free before the new one is started, or a
    # provider that allows only one stream refuses the track the user just picked
    assert order == ["release", "release", "release", "play"]


async def test_replace_does_not_hand_the_player_a_next_item_from_the_new_list() -> None:
    """
    The buffered index is dropped before the swap, so no stale position picks the next track.

    The player is still sitting on the old playing index while the items are exchanged; loading
    the new ones against that index would enqueue an arbitrary track as the one to play next.
    """
    ctrl = _controller()
    _load_playing_queue(ctrl)
    # a live successor, so the branch under test can actually fire
    ctrl.get_next_item = Mock(
        side_effect=lambda queue_id, _cur_index: ctrl._queue_data[queue_id].items[0]
    )
    enqueued = Mock()
    ctrl._enqueue_next_item = enqueued

    await ctrl.play_media("q1", _playlist(), QueueOption.REPLACE)

    enqueued.assert_not_called()
    assert _queue(ctrl).index_in_buffer is None


async def test_replace_starting_at_a_chosen_track_never_advertises_a_partial_queue() -> None:
    """
    Pinning the track the user picked still publishes the queue exactly once.

    The pinned item used to be loaded on its own before the rest of the batch was shuffled in
    behind it, which put a one-item queue on screen in between - and the shuffle in between can
    hit the database, so it is a real window rather than a theoretical one.
    """
    ctrl = _controller(shuffle_enabled=True)
    ctrl._smart_shuffle.is_enabled = Mock(return_value=False)
    _load_playing_queue(ctrl)
    counts = _observe_item_counts(ctrl)

    await ctrl.play_media(
        "q1", _playlist(), QueueOption.REPLACE, start_item="test://track/n1", shuffle=True
    )

    assert counts == [len(NEW_TRACKS)]
    # the chosen track is the one that starts, whatever the shuffle did with the rest
    assert _item_ids(ctrl)[0] == NEW_TRACKS[0]
    assert sorted(_item_ids(ctrl)) == sorted(NEW_TRACKS)


async def test_replacing_a_dynamic_queue_hides_the_rebuild_from_clients() -> None:
    """
    The pool is fetched over the network with the queue already emptied, so updates are held off.

    Player updates would otherwise reconcile against the half-built queue and publish it as empty
    with nothing playing - the very state this is meant to remove, and on the dynamic path the
    fetch is the slowest part of the whole operation.
    """
    ctrl = _controller(is_dynamic=True, shuffle_enabled=True)
    _load_playing_queue(ctrl)
    observed: list[bool] = []

    def _record_fill(*_args: Any, **_kwargs: Any) -> list[Track]:
        observed.append(ctrl._queue_data["q1"].transitioning)
        return [_track(item_id) for item_id in NEW_TRACKS]

    ctrl._managed_pool.fill = AsyncMock(side_effect=_record_fill)

    await ctrl.play_media("q1", _playlist("dyn1", dynamic=True), QueueOption.REPLACE)

    assert observed == [True]
    # and the flag is handed back afterwards, or the queue would stop reconciling for good
    assert ctrl._queue_data["q1"].transitioning is False


async def test_replacing_a_dynamic_queue_rebuilds_it_from_the_front() -> None:
    """
    A replace onto a smart mix takes the place of what was playing instead of stacking behind it.

    The rebuild normally keeps the current and already-buffered tracks so the crossfade is not
    disturbed, which is right for an add or a play but would leave a replace appending its pool
    behind the old queue.
    """
    ctrl = _controller(is_dynamic=True, shuffle_enabled=True)
    _load_playing_queue(ctrl)

    await ctrl.play_media("q1", _playlist("dyn1", dynamic=True), QueueOption.REPLACE)

    assert _queue(ctrl).is_dynamic is True
    assert _item_ids(ctrl) == NEW_TRACKS
    ctrl.play_index.assert_awaited_once_with("q1", 0)


async def test_replacing_an_ended_queue_keeps_the_sources_it_just_stored() -> None:
    """
    A queue picked up from its end is replaced like any other, sources included.

    The finished queue used to be emptied on its way through, which also dropped the sources
    recorded for the media being started - taking autoplay's seed with them.
    """
    ctrl = _controller(ended=True)
    _load_playing_queue(ctrl)
    _queue(ctrl).ended = True

    await ctrl.play_media("q1", _playlist(), QueueOption.REPLACE)

    assert _queue(ctrl).ended is False
    assert [source.item_id for source in ctrl._queue_data["q1"].source_items] == ["pl1"]
    assert _item_ids(ctrl) == NEW_TRACKS
