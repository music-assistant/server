"""Tests for the player queues controller helpers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Self, cast

import pytest
from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    Radio,
    Track,
)
from music_assistant_models.media_items.provider_mapping import ProviderMapping
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import ATTR_PLAY_ACTION_IN_PROGRESS
from music_assistant.controllers.player_queues.helpers import (
    build_queue_item,
    find_dynamic_source,
    get_current_playback_speed,
    handle_play_action,
    has_dynamic_source,
    is_dynamic_source,
    space_by_artist,
)
from music_assistant.controllers.player_queues.state import PlayerQueueData

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType

    from music_assistant.controllers.player_queues.controller import PlayerQueuesController

_PROVIDER_MAPPINGS = {
    ProviderMapping(item_id="x", provider_domain="test", provider_instance="test")
}


def _playlist(*, is_dynamic: bool, name: str = "PL") -> Playlist:
    return Playlist(
        item_id=name.lower(),
        provider="test",
        name=name,
        provider_mappings=_PROVIDER_MAPPINGS,
        is_dynamic=is_dynamic,
    )


def _radio(*, is_dynamic: bool, name: str = "R") -> Radio:
    return Radio(
        item_id=name.lower(),
        provider="test",
        name=name,
        provider_mappings=_PROVIDER_MAPPINGS,
        is_dynamic=is_dynamic,
    )


def _track(name: str) -> Track:
    return Track(
        item_id=name.lower(),
        provider="test",
        name=name,
        duration=100,
        artists=UniqueList(),
        provider_mappings=_PROVIDER_MAPPINGS,
    )


def _queue_item(name: str, *, item_id: str | None = None, **extra_attributes: Any) -> QueueItem:
    return QueueItem(
        queue_id="q1",
        queue_item_id=item_id or name.lower(),
        name=name,
        duration=100,
        extra_attributes=extra_attributes,
    )


def _queue() -> PlayerQueue:
    return PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)


def _queue_data(
    *,
    source_items: list[MediaItemType] | None = None,
    enqueued_media_items: list[MediaItemType] | None = None,
) -> PlayerQueueData:
    queue_data = PlayerQueueData(queue=_queue())
    queue_data.source_items = source_items or []
    queue_data.enqueued_media_items = enqueued_media_items or []
    return queue_data


class TestHasDynamicSource:
    """Tests for has_dynamic_source."""

    def test_single_dynamic_playlist(self) -> None:
        """A single dynamic playlist puts the queue in dynamic mode."""
        assert has_dynamic_source([_playlist(is_dynamic=True)]) is True

    def test_single_non_dynamic_playlist(self) -> None:
        """A single non-dynamic playlist is not a dynamic source."""
        assert has_dynamic_source([_playlist(is_dynamic=False)]) is False

    def test_empty_source(self) -> None:
        """An empty source list is not dynamic."""
        assert has_dynamic_source([]) is False

    def test_multiple_dynamic_playlists(self) -> None:
        """Any dynamic playlist among the sources puts the queue in dynamic mode."""
        source: list[MediaItemType] = [
            _playlist(is_dynamic=True, name="A"),
            _playlist(is_dynamic=True, name="B"),
        ]
        assert has_dynamic_source(source) is True

    def test_dynamic_playlist_mixed_with_finite_source(self) -> None:
        """A dynamic playlist mixed with a finite source still counts as dynamic."""
        source: list[MediaItemType] = [_track("Song"), _playlist(is_dynamic=True)]
        assert has_dynamic_source(source) is True

    def test_non_playlist_item(self) -> None:
        """A non-playlist media item is not a dynamic source."""
        assert has_dynamic_source([_track("Song")]) is False


class TestIsDynamicSource:
    """Tests for is_dynamic_source."""

    def test_dynamic_playlist(self) -> None:
        """A dynamic playlist supplies its own on-demand feed."""
        assert is_dynamic_source(_playlist(is_dynamic=True)) is True

    def test_non_dynamic_playlist(self) -> None:
        """A non-dynamic playlist is not a dynamic source."""
        assert is_dynamic_source(_playlist(is_dynamic=False)) is False

    def test_dynamic_radio(self) -> None:
        """A dynamic radio station supplies its own on-demand feed."""
        assert is_dynamic_source(_radio(is_dynamic=True)) is True

    def test_non_dynamic_radio(self) -> None:
        """A non-dynamic (live-stream) radio is not a dynamic source."""
        assert is_dynamic_source(_radio(is_dynamic=False)) is False

    def test_track(self) -> None:
        """A track is never a dynamic source."""
        assert is_dynamic_source(_track("Song")) is False


class TestFindDynamicSource:
    """Tests for find_dynamic_source."""

    def test_dynamic_radio_source(self) -> None:
        """A dynamic radio station is found, so an idle queue can refill from it."""
        station = _radio(is_dynamic=True)
        queue_data = _queue_data(source_items=[station])
        assert find_dynamic_source(queue_data) is station

    def test_dynamic_playlist_source(self) -> None:
        """A dynamic playlist is found the same way."""
        playlist = _playlist(is_dynamic=True)
        queue_data = _queue_data(source_items=[playlist])
        assert find_dynamic_source(queue_data) is playlist

    def test_prefers_the_last_added_source(self) -> None:
        """The most recently added dynamic source wins."""
        first = _playlist(is_dynamic=True, name="A")
        last = _radio(is_dynamic=True, name="B")
        queue_data = _queue_data(source_items=[first, _track("Song"), last])
        assert find_dynamic_source(queue_data) is last

    def test_falls_back_to_enqueued_items(self) -> None:
        """A queue without dynamic sources falls back to what was enqueued on it."""
        station = _radio(is_dynamic=True)
        queue_data = _queue_data(source_items=[_track("Song")], enqueued_media_items=[station])
        assert find_dynamic_source(queue_data) is station

    def test_no_dynamic_source(self) -> None:
        """A queue of finite items has nothing to refill from."""
        queue_data = _queue_data(
            source_items=[_playlist(is_dynamic=False)], enqueued_media_items=[_track("Song")]
        )
        assert find_dynamic_source(queue_data) is None


class TestGetCurrentPlaybackSpeed:
    """Tests for get_current_playback_speed."""

    def test_no_current_item(self) -> None:
        """Defaults to 1.0 when the queue has no current item."""
        assert get_current_playback_speed(_queue()) == 1.0

    def test_speed_from_extra_attributes(self) -> None:
        """Reads the playback_speed from the current item's extra attributes."""
        queue = _queue()
        queue.current_item = _queue_item("Song", playback_speed=1.5)
        assert get_current_playback_speed(queue) == 1.5

    def test_unset_speed_defaults_to_one(self) -> None:
        """Defaults to 1.0 when the current item has no playback_speed set."""
        queue = _queue()
        queue.current_item = _queue_item("Song")
        assert get_current_playback_speed(queue) == 1.0

    def test_zero_speed_falls_back_to_one(self) -> None:
        """A falsy playback_speed falls back to 1.0."""
        queue = _queue()
        queue.current_item = _queue_item("Song", playback_speed=0)
        assert get_current_playback_speed(queue) == 1.0


class _FakeLock:
    """No-op re-entrant async context manager standing in for the player lock."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakePlayer:
    def __init__(self, player_id: str) -> None:
        self.player_id = player_id


class _FakePlayers:
    def __init__(self, player_ids: set[str]) -> None:
        self._players = {player_id: _FakePlayer(player_id) for player_id in player_ids}

    def get_player_lock(self, queue_id: str, purpose: object) -> _FakeLock:
        return _FakeLock()

    def get_player(self, player_id: str) -> _FakePlayer | None:
        return self._players.get(player_id)


class _FakeMass:
    def __init__(self, player_ids: set[str]) -> None:
        self.players = _FakePlayers(player_ids)


class _FakeQueue:
    def __init__(self) -> None:
        self.extra_attributes: dict[str, Any] = {}


class _FakeController:
    """Minimal stand-in exposing only what handle_play_action touches."""

    def __init__(self, queues: dict[str, _FakeQueue], with_players: bool = True) -> None:
        self.mass = _FakeMass(set(queues) if with_players else set())
        self._queue_data = {
            queue_id: PlayerQueueData(queue=cast("PlayerQueue", queue))
            for queue_id, queue in queues.items()
        }
        self.calls: list[str] = []

    def signal_update(self, queue_id: str, items_changed: bool = False) -> None:
        self.calls.append(f"signal:{queue_id}")

    def on_player_update(
        self, player: _FakePlayer, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        self.calls.append(f"refresh:{player.player_id}")


def _flag_value(ctrl: PlayerQueuesController, queue_id: str) -> bool:
    """Return the current in-progress flag for a queue (False if unknown)."""
    data = ctrl._queue_data.get(queue_id)
    if data is None:
        return False
    return bool(data.queue.extra_attributes.get(ATTR_PLAY_ACTION_IN_PROGRESS, False))


@handle_play_action
async def _flag_during(self: PlayerQueuesController, queue_id: str, sink: list[bool]) -> str:
    """Record the in-progress flag while running."""
    sink.append(_flag_value(self, queue_id))
    return "done"


@handle_play_action
async def _nested(self: PlayerQueuesController, queue_id: str, sink: list[bool]) -> str:
    """Invoke a nested decorated action on the same queue."""
    await _flag_during(self, queue_id, sink)
    sink.append(_flag_value(self, queue_id))
    return "outer"


@handle_play_action
async def _boom(self: PlayerQueuesController, queue_id: str) -> str:  # noqa: ARG001
    """Raise to exercise the cleanup path."""
    raise RuntimeError("boom")


class TestHandlePlayAction:
    """Tests for the handle_play_action decorator."""

    async def test_runs_without_known_queue(self) -> None:
        """When the queue is unknown the action still runs and nothing is signalled."""
        ctrl = _FakeController({})
        sink: list[bool] = []
        assert await _flag_during(cast("PlayerQueuesController", ctrl), "missing", sink) == "done"
        assert sink == [False]
        assert ctrl.calls == []

    async def test_sets_and_clears_flag(self) -> None:
        """The in-progress flag is set during the action and cleared afterwards."""
        queue = _FakeQueue()
        ctrl = _FakeController({"q1": queue})
        sink: list[bool] = []
        assert await _flag_during(cast("PlayerQueuesController", ctrl), "q1", sink) == "done"
        assert sink == [True]
        assert queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] is False
        # signalled once on entry and once on exit, with the queue recalculated
        # from the player in between so the exit signal carries the result
        assert ctrl.calls == ["signal:q1", "refresh:q1", "signal:q1"]
        assert ctrl._queue_data["q1"].play_action_refcount == 0

    async def test_clears_flag_without_player(self) -> None:
        """A queue without a registered player still clears and signals."""
        queue = _FakeQueue()
        ctrl = _FakeController({"q1": queue}, with_players=False)
        sink: list[bool] = []
        assert await _flag_during(cast("PlayerQueuesController", ctrl), "q1", sink) == "done"
        assert queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] is False
        assert ctrl.calls == ["signal:q1", "signal:q1"]

    async def test_nested_actions_refcount(self) -> None:
        """Nested actions keep the flag set until the outermost one finishes."""
        queue = _FakeQueue()
        ctrl = _FakeController({"q1": queue})
        sink: list[bool] = []
        assert await _nested(cast("PlayerQueuesController", ctrl), "q1", sink) == "outer"
        # flag stayed True through the inner action and after it returned
        assert sink == [True, True]
        assert queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] is False
        # only the outermost entry/exit signal an update (inner sees it already in progress)
        assert ctrl.calls == ["signal:q1", "refresh:q1", "signal:q1"]
        assert ctrl._queue_data["q1"].play_action_refcount == 0

    async def test_flag_cleared_on_exception(self) -> None:
        """The flag is cleared even when the action raises."""
        queue = _FakeQueue()
        ctrl = _FakeController({"q1": queue})
        with pytest.raises(RuntimeError, match="boom"):
            await _boom(cast("PlayerQueuesController", ctrl), "q1")
        assert queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] is False
        assert ctrl._queue_data["q1"].play_action_refcount == 0


class TestSpaceByArtist:
    """Tests for space_by_artist."""

    def test_separates_adjacent_shared_artist(self) -> None:
        """Adjacent entries sharing an artist are pulled apart (by intersection), dropping nothing."""
        sets = [{"a"}, {"a", "b"}, {"b"}, {"c"}]
        spaced = [sets[index] for index in space_by_artist(sets)]
        assert sorted(spaced, key=sorted) == sorted(sets, key=sorted)
        assert all(not (spaced[i] & spaced[i + 1]) for i in range(len(spaced) - 1))

    def test_honours_preceding_seam(self) -> None:
        """The first entry shares no artist with the preceding (seam) set."""
        sets = [{"a", "b"}, {"c"}, {"d"}]
        order = space_by_artist(sets, preceding={"a"})
        assert not (sets[order[0]] & {"a"})

    def test_identity_when_already_clear(self) -> None:
        """With no clashes and no seam, the order is left untouched."""
        assert space_by_artist([{"a"}, {"b"}, {"c"}]) == [0, 1, 2]

    def test_empty_input(self) -> None:
        """An empty input yields an empty order."""
        assert space_by_artist([]) == []


def _thumb(path: str = "http://img/t.jpg") -> MediaItemImage:
    return MediaItemImage(
        type=ImageType.THUMB, path=path, provider="test", remotely_accessible=True
    )


def _heavy_metadata() -> MediaItemMetadata:
    """Build metadata resembling a fully enriched item (the bulk of a queue item's size)."""
    return MediaItemMetadata(
        description="A long track description. " * 20,
        review="An even longer critical review. " * 20,
        lyrics="\n".join(f"Lyrics line {index}" for index in range(60)),
        lrc_lyrics="\n".join(f"[00:{index:02d}.00] line {index}" for index in range(60)),
        images=UniqueList([_thumb()]),
        genres={"rock", "indie"},
    )


def _heavy_track() -> Track:
    return Track(
        item_id="t1",
        provider="test",
        name="Song",
        duration=210,
        artists=UniqueList(
            [
                Artist(
                    item_id="a1",
                    provider="test",
                    name="Artist",
                    metadata=_heavy_metadata(),
                    provider_mappings=_PROVIDER_MAPPINGS,
                )
            ]
        ),
        album=Album(
            item_id="al1",
            provider="test",
            name="Album",
            metadata=_heavy_metadata(),
            provider_mappings=_PROVIDER_MAPPINGS,
        ),
        metadata=_heavy_metadata(),
        provider_mappings=_PROVIDER_MAPPINGS,
    )


def _heavy_radio() -> Radio:
    return Radio(
        item_id="r1",
        provider="test",
        name="Radio One",
        metadata=_heavy_metadata(),
        provider_mappings=_PROVIDER_MAPPINGS,
    )


class TestBuildQueueItem:
    """Tests for build_queue_item."""

    def test_slims_track_metadata(self) -> None:
        """A track's heavy metadata is dropped while playback-relevant fields are kept."""
        item = build_queue_item("q1", _heavy_track())
        assert isinstance(item.media_item, Track)
        # heavy metadata is dropped
        assert item.media_item.metadata == MediaItemMetadata()
        # provider mappings are kept (needed for stream resolution / failover)
        assert item.media_item.provider_mappings == _PROVIDER_MAPPINGS
        # the identifiers used to re-hydrate on promotion are kept
        assert item.media_item.item_id == "t1"
        assert item.media_item.provider == "test"
        assert item.media_item.media_type is MediaType.TRACK
        # artwork survives on the top-level image and artists/album stay slimmed to mappings
        assert item.image is not None
        assert item.image.type is ImageType.THUMB
        assert all(isinstance(artist, ItemMapping) for artist in item.media_item.artists)
        assert isinstance(item.media_item.album, ItemMapping)
        # name/duration for compact list rows are kept
        assert item.name == "Artist - Song"
        assert item.duration == 210

    def test_reduces_serialized_size(self) -> None:
        """Slimming a track queue item substantially reduces its serialized size."""
        fat = len(json.dumps(QueueItem.from_media_item("q1", _heavy_track()).to_dict()))
        slim = len(json.dumps(build_queue_item("q1", _heavy_track()).to_dict()))
        assert slim < fat * 0.6

    def test_leaves_non_track_untouched(self) -> None:
        """Non-track items (e.g. radio) keep their metadata as they are not re-hydrated."""
        item = build_queue_item("q1", _heavy_radio())
        assert isinstance(item.media_item, Radio)
        assert item.media_item.metadata.description
        assert item.media_item.metadata.images

    def test_cache_roundtrip_preserves_slim_shape(self) -> None:
        """A slim track queue item round-trips through the persisted cache unchanged."""
        restored = QueueItem.from_cache(build_queue_item("q1", _heavy_track()).to_cache())
        assert isinstance(restored.media_item, Track)
        assert restored.media_item.metadata == MediaItemMetadata()
        assert restored.media_item.provider_mappings == _PROVIDER_MAPPINGS
        assert restored.media_item.item_id == "t1"
        assert restored.image is not None
        assert restored.image.type is ImageType.THUMB
