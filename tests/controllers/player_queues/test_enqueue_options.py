"""
Tests for the enqueue-option handling in the player-queues controller.

These exercise ``QueueLoaderMixin._enqueue_with_option`` against a bare controller
instance, mirroring ``test_user_initiated_plays``. The real ``load`` and
``update_items`` run so the insert position is verified end-to-end; only the
side-effecting ``play_index`` and ``signal_update`` are stubbed out.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.enums import MediaType, PlaybackState, QueueOption
from music_assistant_models.media_items import (
    Album,
    ItemMapping,
    MediaItemType,
    Podcast,
    ProviderMapping,
    Radio,
    Track,
)
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


def _controller() -> PlayerQueuesController:
    """Create a bare controller instance with the noisy ``signal_update`` stubbed out."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    return ctrl


def _items(queue_id: str, names: list[str]) -> list[QueueItem]:
    """Build a list of simple queue items with the given names."""
    return [
        QueueItem(queue_id=queue_id, queue_item_id=name, name=name, duration=60) for name in names
    ]


def _track(item_id: str) -> Track:
    """Build a playable Track on the 'test' provider (mirrors the managed-pool test helper)."""
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


def _album(item_id: str) -> Album:
    """Build an Album on the 'test' provider (a container source, kept in the wire `sources`)."""
    return Album(
        item_id=item_id,
        provider="test",
        name=f"Album {item_id}",
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def _podcast(item_id: str) -> Podcast:
    """Build a Podcast on the 'test' provider (a container source, kept in the wire `sources`)."""
    return Podcast(
        item_id=item_id,
        provider="test",
        name=f"Podcast {item_id}",
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def _radio(item_id: str, *, is_dynamic: bool = False) -> Radio:
    """Build a Radio on the 'test' provider (an individual item, omitted from the wire `sources`)."""
    return Radio(
        item_id=item_id,
        provider="test",
        name=f"Radio {item_id}",
        is_dynamic=is_dynamic,
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


async def test_play_on_idle_queue_starts_at_first_item() -> None:
    """PLAY with multiple items onto an idle/empty queue plays the first item, not the second."""
    ctrl = _controller()
    play_index = AsyncMock()
    ctrl.play_index = play_index  # type: ignore[method-assign]
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    items = _items("q1", ["a", "b", "c"])

    await ctrl._enqueue_with_option("q1", items, QueueOption.PLAY)

    # the items are loaded from the very start and playback begins at the first one
    assert ctrl._queue_data["q1"].items[0] is items[0]
    assert len(ctrl._queue_data["q1"].items) == 3
    play_index.assert_awaited_once_with("q1", 0)


async def test_play_on_active_queue_inserts_after_current() -> None:
    """PLAY on an active queue keeps inserting right after the current index and jumps to it."""
    ctrl = _controller()
    play_index = AsyncMock()
    ctrl.play_index = play_index  # type: ignore[method-assign]
    ctrl.get_next_item = Mock(return_value=None)  # type: ignore[method-assign]
    existing = _items("q1", ["e0", "e1", "e2"])
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=len(existing),
        state=PlaybackState.PLAYING,
        current_index=2,
        index_in_buffer=2,
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue, items=list(existing))}
    new_items = _items("q1", ["n0", "n1"])

    await ctrl._enqueue_with_option("q1", new_items, QueueOption.PLAY)

    # inserted right after the current/buffered index (2) and playback jumps there
    assert ctrl._queue_data["q1"].items[3] is new_items[0]
    play_index.assert_awaited_once_with("q1", 3)


async def test_next_shuffle_pins_first_item_and_shuffles_rest() -> None:
    """NEXT with shuffle on pins the first new item after the buffered index, shuffles the rest."""
    ctrl = _controller()
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.get_next_item = Mock(return_value=None)  # type: ignore[method-assign]
    # keep the shuffle deterministic-enough: pure random of the "rest", first item stays pinned
    ctrl._smart_shuffle = Mock()
    ctrl._smart_shuffle.is_enabled = Mock(return_value=False)
    existing = _items("q1", ["e0", "e1", "e2"])
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=len(existing),
        state=PlaybackState.PLAYING,
        current_index=0,
        index_in_buffer=0,
        shuffle_enabled=True,
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue, items=list(existing))}
    new_items = _items("q1", ["n0", "n1"])

    await ctrl._enqueue_with_option("q1", new_items, QueueOption.NEXT)

    items = ctrl._queue_data["q1"].items
    # the first new item is pinned right after the buffered index (0) so it plays next
    assert items[0] is existing[0]
    assert items[1] is new_items[0]
    # the rest of the batch and the existing unplayed tail are shuffled together behind it
    assert {item.queue_item_id for item in items[2:]} == {"n1", "e1", "e2"}
    ctrl.play_index.assert_not_awaited()


async def test_next_shuffle_single_item_keeps_tail_order() -> None:
    """A single-item NEXT inserts at the buffered index+1 without re-arranging the tail."""
    ctrl = _controller()
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.get_next_item = Mock(return_value=None)  # type: ignore[method-assign]
    existing = _items("q1", ["e0", "e1", "e2"])
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=len(existing),
        state=PlaybackState.PLAYING,
        current_index=0,
        index_in_buffer=0,
        shuffle_enabled=True,
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue, items=list(existing))}
    new_items = _items("q1", ["n0"])

    await ctrl._enqueue_with_option("q1", new_items, QueueOption.NEXT)

    # inserted right after the buffered index; the existing tail keeps its order (no reshuffle)
    assert [item.queue_item_id for item in ctrl._queue_data["q1"].items] == ["e0", "n0", "e1", "e2"]
    ctrl.play_index.assert_not_awaited()


async def test_next_on_empty_queue_sets_current_index_without_playing() -> None:
    """NEXT onto an empty queue stages the items and points the current index at the first one."""
    ctrl = _controller()
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    items = _items("q1", ["a", "b", "c"])

    await ctrl._enqueue_with_option("q1", items, QueueOption.NEXT)

    assert [item.queue_item_id for item in ctrl._queue_data["q1"].items] == ["a", "b", "c"]
    assert queue.current_index == 0
    assert queue.current_item is items[0]
    ctrl.play_index.assert_not_awaited()


async def test_replace_next_on_empty_queue_sets_current_index_without_playing() -> None:
    """REPLACE_NEXT onto an empty queue stages the items and sets the current index."""
    ctrl = _controller()
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    items = _items("q1", ["a", "b"])

    await ctrl._enqueue_with_option("q1", items, QueueOption.REPLACE_NEXT)

    assert [item.queue_item_id for item in ctrl._queue_data["q1"].items] == ["a", "b"]
    assert queue.current_index == 0
    assert queue.current_item is items[0]
    ctrl.play_index.assert_not_awaited()


async def test_next_on_idle_queue_with_content_keeps_current_index() -> None:
    """NEXT on an idle queue that has content leaves the current index and inserts after it."""
    ctrl = _controller()
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    existing = _items("q1", ["e0", "e1", "e2"])
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=len(existing),
        state=PlaybackState.IDLE,
        current_index=1,
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue, items=list(existing))}
    new_items = _items("q1", ["n0"])

    await ctrl._enqueue_with_option("q1", new_items, QueueOption.NEXT)

    items = ctrl._queue_data["q1"].items
    # current index is untouched; the new item is inserted right after it
    assert queue.current_index == 1
    assert [item.queue_item_id for item in items] == ["e0", "e1", "n0", "e2"]
    ctrl.play_index.assert_not_awaited()


def _dynamic_controller() -> PlayerQueuesController:
    """Build a bare controller wired to drive ``_enter_dynamic_mode`` with a stubbed managed pool."""
    ctrl = _controller()
    ctrl.get_next_item = Mock(return_value=None)  # type: ignore[method-assign]
    ctrl.is_smart_shuffle_active = Mock(return_value=True)  # type: ignore[method-assign]
    ctrl._managed_pool = Mock()
    ctrl._managed_pool.fill = AsyncMock(return_value=[_track("p0"), _track("p1")])
    return ctrl


async def test_enter_dynamic_mode_add_on_idle_does_not_start_playback() -> None:
    """ADD of a dynamic source onto an idle/empty queue stages the pool but does not start playing."""
    ctrl = _dynamic_controller()
    play_index = AsyncMock()
    ctrl.play_index = play_index  # type: ignore[method-assign]
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}

    await ctrl._enter_dynamic_mode("q1", QueueOption.ADD)

    # the pool is loaded and the current item is set, but playback is not started
    items = ctrl._queue_data["q1"].items
    assert {item.media_item.item_id for item in items if item.media_item is not None} == {
        "p0",
        "p1",
    }
    assert queue.current_index == 0
    assert queue.current_item is not None
    play_index.assert_not_awaited()


async def test_enter_dynamic_mode_play_on_idle_starts_playback() -> None:
    """PLAY of a dynamic source onto an idle/empty queue starts playback on the rebuilt pool."""
    ctrl = _dynamic_controller()
    play_index = AsyncMock()
    ctrl.play_index = play_index  # type: ignore[method-assign]
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}

    await ctrl._enter_dynamic_mode("q1", QueueOption.PLAY)

    assert len(ctrl._queue_data["q1"].items) == 2
    play_index.assert_awaited_once_with("q1", 0)


async def test_enter_dynamic_mode_add_on_active_keeps_current_and_rebuilds_tail() -> None:
    """ADD of a dynamic source onto a playing queue rebuilds the tail without interrupting it."""
    ctrl = _dynamic_controller()
    play_index = AsyncMock()
    ctrl.play_index = play_index  # type: ignore[method-assign]
    existing = _items("q1", ["e0", "e1", "e2"])
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=len(existing),
        state=PlaybackState.PLAYING,
        current_index=1,
        index_in_buffer=1,
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue, items=list(existing))}

    await ctrl._enter_dynamic_mode("q1", QueueOption.ADD)

    items = ctrl._queue_data["q1"].items
    # the current track and history are kept; the finite tail behind it is replaced by the pool
    assert [item.queue_item_id for item in items[:2]] == ["e0", "e1"]
    assert {item.media_item.item_id for item in items[2:] if item.media_item is not None} == {
        "p0",
        "p1",
    }
    assert queue.current_index == 1
    play_index.assert_not_awaited()


async def test_enter_dynamic_mode_replaces_old_pool_tail_stays_bounded() -> None:
    """Re-building on an already-dynamic queue drops the whole old pool tail, keeping it bounded."""
    ctrl = _dynamic_controller()
    play_index = AsyncMock()
    ctrl.play_index = play_index  # type: ignore[method-assign]
    # current + buffered (e0, e1) followed by a large existing pool tail (o0..o9)
    existing = _items("q1", ["e0", "e1", *[f"o{i}" for i in range(10)]])
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=len(existing),
        state=PlaybackState.PLAYING,
        current_index=1,
        index_in_buffer=1,
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue, items=list(existing))}

    await ctrl._enter_dynamic_mode("q1", QueueOption.ADD)

    items = ctrl._queue_data["q1"].items
    # old 10-track tail is dropped and replaced by the fresh bounded pool (2 + 2, not 2 + 10 + 2)
    assert [item.queue_item_id for item in items[:2]] == ["e0", "e1"]
    assert {item.media_item.item_id for item in items[2:] if item.media_item is not None} == {
        "p0",
        "p1",
    }
    assert len(items) == 4
    play_index.assert_not_awaited()


async def test_enter_dynamic_mode_rebuilds_from_buffer_index() -> None:
    """The rebuild keeps the already-buffered next track and only replaces what is behind it."""
    ctrl = _dynamic_controller()
    play_index = AsyncMock()
    ctrl.play_index = play_index  # type: ignore[method-assign]
    # current at 1, but the player has already buffered index 2, so it must be kept
    existing = _items("q1", ["e0", "e1", "e2", "o0", "o1"])
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=len(existing),
        state=PlaybackState.PLAYING,
        current_index=1,
        index_in_buffer=2,
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue, items=list(existing))}

    await ctrl._enter_dynamic_mode("q1", QueueOption.ADD)

    items = ctrl._queue_data["q1"].items
    # kept through the buffered index (e0, e1, e2); everything after it rebuilt from the pool
    assert [item.queue_item_id for item in items[:3]] == ["e0", "e1", "e2"]
    assert {item.media_item.item_id for item in items[3:] if item.media_item is not None} == {
        "p0",
        "p1",
    }
    play_index.assert_not_awaited()


def test_store_sources_dedupes_wire_sources_keeps_internal_multiplicity() -> None:
    """The wire `sources` list is deduped per source; the server keeps every occurrence."""
    ctrl = _controller()
    ctrl._managed_pool = Mock()
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    a, b = _album("a"), _album("b")

    # "a" added twice (multiplicity 2), "b" once
    ctrl.store_sources(queue, [a, b, a])

    # server-side list keeps every occurrence (drives the managed-pool weighting)
    assert ctrl._queue_data["q1"].source_items == [a, b, a]
    # wire list clients see is deduped to the distinct sources, order preserved
    assert [mapping.uri for mapping in queue.sources] == [a.uri, b.uri]


def test_store_sources_keeps_only_container_types_on_wire() -> None:
    """Container sources reach the wire `sources`; individual items are omitted (kept server-side)."""
    ctrl = _controller()
    ctrl._managed_pool = Mock()
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    album, podcast = _album("al"), _podcast("po")
    track, radio = _track("t"), _radio("r")
    items: list[MediaItemType] = [album, track, podcast, radio]

    ctrl.store_sources(queue, items)

    # the full set (incl. the individual items) is retained server-side for pool weighting / seeds
    assert ctrl._queue_data["q1"].source_items == items
    # but only the container sources are shown to clients, in original order
    assert [mapping.uri for mapping in queue.sources] == [album.uri, podcast.uri]


def test_store_sources_keeps_a_dynamic_station_on_wire() -> None:
    """A dynamic station is a source the queue plays from, so clients get to show it."""
    ctrl = _controller()
    ctrl._managed_pool = Mock()
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    station = _radio("dyn", is_dynamic=True)
    live_stream = _radio("live")

    ctrl.store_sources(queue, [station, live_stream])

    assert [mapping.uri for mapping in queue.sources] == [station.uri]


def _shuffled_queue(ctrl: PlayerQueuesController) -> PlayerQueue:
    """Set up an idle queue with shuffle on and a deterministic (reversing) shuffle."""
    ctrl._smart_shuffle = Mock()
    ctrl._smart_shuffle.is_enabled = Mock(return_value=True)
    ctrl._smart_shuffle.arrange = AsyncMock(
        side_effect=lambda _queue, items: list(items)[::-1],
    )
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=0,
        shuffle_enabled=True,
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    return queue


async def test_play_with_start_item_pins_chosen_track_under_shuffle() -> None:
    """PLAY from a chosen track keeps that track first when shuffle is on."""
    ctrl = _controller()
    play_index = AsyncMock()
    ctrl.play_index = play_index  # type: ignore[method-assign]
    _shuffled_queue(ctrl)
    items = _items("q1", ["chosen", "b", "c", "d"])

    await ctrl._enqueue_with_option("q1", items, QueueOption.PLAY, pin_first=True)

    queue_items = ctrl._queue_data["q1"].items
    # the track the user picked starts playing; the rest is shuffled behind it
    assert queue_items[0] is items[0]
    assert {item.queue_item_id for item in queue_items[1:]} == {"b", "c", "d"}
    play_index.assert_awaited_once_with("q1", 0)


async def test_replace_with_start_item_pins_chosen_track_under_shuffle() -> None:
    """REPLACE from a chosen track keeps that track first when shuffle is on."""
    ctrl = _controller()
    play_index = AsyncMock()
    ctrl.play_index = play_index  # type: ignore[method-assign]
    _shuffled_queue(ctrl)
    items = _items("q1", ["chosen", "b", "c", "d"])

    await ctrl._enqueue_with_option("q1", items, QueueOption.REPLACE, pin_first=True)

    queue_items = ctrl._queue_data["q1"].items
    assert queue_items[0] is items[0]
    assert {item.queue_item_id for item in queue_items[1:]} == {"b", "c", "d"}
    play_index.assert_awaited_once_with("q1", 0)


async def test_play_without_start_item_shuffles_the_whole_batch() -> None:
    """PLAY of a whole playlist under shuffle still randomises which track starts."""
    ctrl = _controller()
    play_index = AsyncMock()
    ctrl.play_index = play_index  # type: ignore[method-assign]
    _shuffled_queue(ctrl)
    items = _items("q1", ["a", "b", "c", "d"])

    await ctrl._enqueue_with_option("q1", items, QueueOption.PLAY)

    # nothing is pinned: the deterministic reversal moves the last item to the front
    assert ctrl._queue_data["q1"].items[0] is items[-1]
    play_index.assert_awaited_once_with("q1", 0)


async def test_play_with_start_item_keeps_order_when_shuffle_is_off() -> None:
    """With shuffle off, PLAY from a chosen track plays it and keeps the rest in order."""
    ctrl = _controller()
    play_index = AsyncMock()
    ctrl.play_index = play_index  # type: ignore[method-assign]
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    items = _items("q1", ["chosen", "b", "c"])

    await ctrl._enqueue_with_option("q1", items, QueueOption.PLAY, pin_first=True)

    assert [item.queue_item_id for item in ctrl._queue_data["q1"].items] == ["chosen", "b", "c"]
    play_index.assert_awaited_once_with("q1", 0)
