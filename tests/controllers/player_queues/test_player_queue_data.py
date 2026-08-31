"""Tests for the server-side PlayerQueueData container and its cache round-trip."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import AlbumType, RepeatMode
from music_assistant_models.media_items import (
    Album,
    ItemMapping,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues.constants import CACHE_FORMAT_VERSION
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


def _album(item_id: str) -> Album:
    """Build a minimal library Album."""
    return Album(
        item_id=item_id,
        provider="library",
        name=f"Album {item_id}",
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
        album_type=AlbumType.ALBUM,
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
    album = _album("a1")
    queue = _queue(is_dynamic=True, sources=[ItemMapping.from_item(playlist)])
    return PlayerQueueData(
        queue=queue,
        items=[QueueItem.from_media_item("q1", _track("t1"))],
        source_items=[playlist],
        enqueued_media_items=[playlist, album],
        credited_albums={album},
        userid="user-1",
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
    # the server-only persisted fields survive too
    assert [item.uri for item in restored.enqueued_media_items] == [
        item.uri for item in data.enqueued_media_items
    ]
    assert restored.userid == "user-1"
    # a credited album survives, so a restart does not re-credit the same enqueue
    assert {item.uri for item in restored.credited_albums} == {
        item.uri for item in data.credited_albums
    }
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
    data = PlayerQueueData(
        queue=queue,
        items=[QueueItem.from_media_item("q1", _track("t1"))],
        source_items=[],
        enqueued_media_items=[_track("seed")],
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


def test_cache_round_trip_restores_settings() -> None:
    """The shuffle/repeat settings and the crossfade/autoplay overrides survive a round-trip."""
    queue = _queue(shuffle_enabled=True, repeat_mode=RepeatMode.ALL)
    data = PlayerQueueData(queue=queue, crossfade_override=True, autoplay_override=True)

    restored = PlayerQueueData.from_cache(data.to_cache(), data.items_to_cache())

    assert restored.queue.shuffle_enabled is True
    assert restored.queue.repeat_mode == RepeatMode.ALL
    assert restored.crossfade_override is True
    assert restored.autoplay_override is True


def test_from_cache_keeps_settings_when_item_unreadable() -> None:
    """One unreadable queue item is skipped without losing the other items or the settings."""
    queue = _queue(shuffle_enabled=True)
    data = PlayerQueueData(
        queue=queue,
        items=[QueueItem.from_media_item("q1", _track("t1"))],
        crossfade_override=True,
    )
    items_data = [*data.items_to_cache(), {"sort_index": 0}]  # 2nd entry misses required fields

    restored = PlayerQueueData.from_cache(data.to_cache(), items_data)

    # the good item is kept, the unreadable one is dropped
    assert [item.queue_item_id for item in restored.items] == [
        item.queue_item_id for item in data.items
    ]
    # the wire-facing count matches the items actually restored (not the cached count)
    assert restored.queue.items == len(restored.items) == 1
    # and the settings are untouched by the item failure
    assert restored.queue.shuffle_enabled is True
    assert restored.crossfade_override is True


def test_from_cache_keeps_settings_when_source_unreadable() -> None:
    """An unreadable source mapping is skipped instead of aborting the whole restore."""
    queue = _queue(shuffle_enabled=True, sources=[ItemMapping.from_item(_dynamic_playlist())])
    data = PlayerQueueData(queue=queue)
    state = data.to_cache()
    # corrupt the persisted source mappings so their deserialization raises
    state["queue"]["sources"] = [{"not": "a mapping"}]

    restored = PlayerQueueData.from_cache(state, data.items_to_cache())

    assert restored.queue.sources == []
    assert restored.queue.shuffle_enabled is True


def test_from_cache_keeps_settings_when_enqueued_media_unreadable() -> None:
    """Unreadable enqueued media clears only the media payload, never the queue settings."""
    queue = _queue(shuffle_enabled=True)
    data = PlayerQueueData(
        queue=queue, enqueued_media_items=[_track("seed")], crossfade_override=True
    )
    state = data.to_cache()
    # corrupt the persisted enqueued media so its deserialization raises
    state["enqueued_media_items"] = [{"not": "a media item"}]

    restored = PlayerQueueData.from_cache(state, data.items_to_cache())

    assert restored.enqueued_media_items == []
    assert restored.queue.shuffle_enabled is True
    assert restored.crossfade_override is True


def test_from_cache_incompatible_version_raises() -> None:
    """A cache written with an incompatible format version is rejected rather than misread."""
    data = _data_with_dynamic_source()
    state = data.to_cache()
    state["cache_format_version"] = CACHE_FORMAT_VERSION + 1

    with pytest.raises(ValueError, match="incompatible queue cache format"):
        PlayerQueueData.from_cache(state, data.items_to_cache())


def test_from_cache_reads_legacy_flat_layout() -> None:
    """A pre-refactor cache (wire fields at the top level, no envelope) is still restored."""
    data = _data_with_dynamic_source()
    nested = data.to_cache()
    # rebuild the old flat layout: wire fields at the top level, server-state keys alongside, and
    # no cache_format_version (caches written before the refactor had no version stamp)
    flat = {
        **nested["queue"],
        "enqueued_media_items": nested["enqueued_media_items"],
        "source_items": nested["source_items"],
        "userid": nested["userid"],
    }

    restored = PlayerQueueData.from_cache(flat, data.items_to_cache())

    assert restored.queue.is_dynamic is True
    assert [item.uri for item in restored.source_items] == [item.uri for item in data.source_items]
    assert [item.uri for item in restored.enqueued_media_items] == [
        item.uri for item in data.enqueued_media_items
    ]
    assert restored.userid == "user-1"


def test_cache_significant_ignores_playback_progress() -> None:
    """Change detection ignores volatile playback-progress fields but catches real changes."""
    data = _data_with_dynamic_source()
    state = data.to_cache()
    # only elapsed time / playback speed advanced -> not a persist-worthy change
    progressed = {
        **state,
        "queue": {**state["queue"], "elapsed_time": 999.0, "playback_speed": 2.0},
    }
    assert PlayerQueueData.cache_significant(progressed) == PlayerQueueData.cache_significant(state)
    # a real settings change -> detected
    shuffled = {
        **state,
        "queue": {**state["queue"], "shuffle_enabled": not state["queue"]["shuffle_enabled"]},
    }
    assert PlayerQueueData.cache_significant(shuffled) != PlayerQueueData.cache_significant(state)


def test_legacy_toggle_state_empty_once_overrides_present() -> None:
    """A snapshot that already carries the override fields has nothing left to import."""
    data = PlayerQueueData(queue=_queue(), crossfade_override=True, autoplay_override=False)
    assert PlayerQueueData.legacy_toggle_state(data.to_cache()) == {}


def test_legacy_toggle_state_reads_the_pre_override_wire_fields() -> None:
    """A pre-override snapshot's wire crossfade/autoplay values are read out by name."""
    state = {"queue": {"crossfade_enabled": True, "autoplay_enabled": False}}
    assert PlayerQueueData.legacy_toggle_state(state) == {"crossfade": True, "autoplay": False}


def test_legacy_toggle_state_falls_back_to_dont_stop_the_music() -> None:
    """A pre-rename snapshot's `dont_stop_the_music_enabled` is read as the autoplay toggle."""
    state = {"queue": {"dont_stop_the_music_enabled": True}}
    assert PlayerQueueData.legacy_toggle_state(state) == {"autoplay": True}


def test_legacy_toggle_state_only_returns_keys_actually_present() -> None:
    """A snapshot storing only one toggle does not invent a value for the other."""
    state = {"queue": {"crossfade_enabled": True}}
    assert PlayerQueueData.legacy_toggle_state(state) == {"crossfade": True}
