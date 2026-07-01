"""Tests for the server-side PlayerQueueData container and its cache round-trip."""

from __future__ import annotations

from music_assistant_models.media_items import ItemMapping, Playlist, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues.state import PlayerQueueData


def _track(item_id: str) -> Track:
    """Build a minimal library Track."""
    return Track(
        item_id=item_id,
        provider="library",
        name=f"Track {item_id}",
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def _dynamic_playlist(item_id: str = "radio1") -> Playlist:
    """Build a dynamic (radio) Playlist."""
    playlist = Playlist(
        item_id=item_id,
        provider="radio_playlist",
        name="Radio",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="radio_playlist",
                provider_instance="radio_playlist",
            )
        },
    )
    playlist.is_dynamic = True
    return playlist


def _queue(**kwargs: object) -> PlayerQueue:
    """Build a PlayerQueue with the given overrides."""
    return PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Queue",
        available=True,
        items=0,
        **kwargs,  # type: ignore[arg-type]
    )


def _data_with_dynamic_source() -> PlayerQueueData:
    """Build a PlayerQueueData for a queue playing a dynamic radio playlist plus one queued track."""
    playlist = _dynamic_playlist()
    queue = _queue(is_dynamic=True, sources=[ItemMapping.from_item(playlist)])
    queue.enqueued_media_items = [playlist]
    return PlayerQueueData(
        queue=queue,
        items=[QueueItem.from_media_item("q1", _track("t1"))],
        source_items=[playlist],
        # runtime-only fields set to non-defaults to prove they do not survive the round-trip
        transitioning=True,
        play_action_refcount=3,
        last_counted_play="t1",
        flow_buffer_completed="sess-1",
    )


def test_cache_round_trip_restores_queue_items_and_sources() -> None:
    """to_cache/from_cache restores the queue, items and source items; runtime fields reset."""
    data = _data_with_dynamic_source()

    restored = PlayerQueueData.from_cache(data.to_cache(), data.items_to_cache())

    # the persisted bits survive
    assert restored.queue.queue_id == "q1"
    assert [item.queue_item_id for item in restored.items] == [
        item.queue_item_id for item in data.items
    ]
    assert [item.uri for item in restored.source_items] == [item.uri for item in data.source_items]
    # is_dynamic is recomputed from the restored source items (a dynamic playlist is present)
    assert restored.queue.is_dynamic is True
    # runtime-only fields are reset to their defaults, never persisted
    assert restored.transitioning is False
    assert restored.play_action_refcount == 0
    assert restored.last_counted_play is None
    assert restored.flow_buffer_completed is None


def test_cache_round_trip_non_dynamic_has_no_sources() -> None:
    """A queue with no dynamic source restores with is_dynamic False and empty sources."""
    queue = _queue()
    queue.enqueued_media_items = [_track("seed")]
    data = PlayerQueueData(
        queue=queue, items=[QueueItem.from_media_item("q1", _track("t1"))], source_items=[]
    )

    restored = PlayerQueueData.from_cache(data.to_cache(), data.items_to_cache())

    assert restored.source_items == []
    assert restored.queue.is_dynamic is False


def test_from_cache_legacy_rebuilds_source_items_from_sources() -> None:
    """A legacy cache without persisted source_items rebuilds them from sources + enqueued items."""
    data = _data_with_dynamic_source()
    state = data.to_cache()
    # simulate a pre-source_items cache entry: only the wire `sources` ItemMappings persisted
    state.pop("source_items")

    restored = PlayerQueueData.from_cache(state, data.items_to_cache())

    # the full source item is recovered by matching `sources` against the enqueued media items
    assert [item.uri for item in restored.source_items] == [item.uri for item in data.source_items]
    assert restored.queue.is_dynamic is True
