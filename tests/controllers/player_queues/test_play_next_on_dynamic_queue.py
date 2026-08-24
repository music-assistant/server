"""
Tests for ``QueueOption.NEXT`` ("Play next") of a track on a dynamic (managed-pool) queue.

These exercise ``QueueLoaderMixin._handle_play_media`` end-to-end against a bare controller
instance wired to a real ``ManagedPool``, mirroring ``test_user_initiated_plays`` and
``test_enqueue_options``. On a dynamic queue, a NEXT track must be carved out of the pool: it
is inserted literally after the buffered index instead of being folded into the pool as a
source (which would place it at a random position and subject it to the pool's recency gate).
A control test proves the linear (non-dynamic) path already does this correctly, scoping the
carve-out to the dynamic path. Further tests prove ADD and NEXT-of-a-container keep feeding
the pool, and that the enqueue transitioning a queue to dynamic plays the track exactly once.
"""

from __future__ import annotations

import random
from unittest.mock import AsyncMock, MagicMock, Mock

from music_assistant_models.enums import MediaType, PlaybackState, QueueOption
from music_assistant_models.media_items import Album, ItemMapping, ProviderMapping, Radio, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.music.recency import RecencySnapshot, RecencyWindows
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.managed_pool import ManagedPool
from music_assistant.controllers.player_queues.state import PlayerQueueData

NOW = 1_000_000
DAY = 86_400
WINDOWS = RecencyWindows(song_seconds=DAY, artist_seconds=None, duplicate_gap_seconds=3600)


def _track(item_id: str, artist: str = "A") -> Track:
    """Build a playable Track on the 'test' provider."""
    return Track(
        item_id=item_id,
        provider="test",
        name=f"Track {item_id}",
        duration=60,
        artists=UniqueList(
            [ItemMapping(item_id=artist, provider="test", name=artist, media_type=MediaType.ARTIST)]
        ),
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def _radio(item_id: str) -> Radio:
    """Build a dynamic Radio source on the 'test' provider."""
    return Radio(
        item_id=item_id,
        provider="test",
        name=f"Radio {item_id}",
        is_dynamic=True,
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def _album(item_id: str) -> Album:
    """Build an Album on the 'test' provider (a container that feeds the pool as a source)."""
    return Album(
        item_id=item_id,
        provider="test",
        name=f"Album {item_id}",
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def _queue_item(queue_id: str, track: Track) -> QueueItem:
    """Build a queue item wrapping the given track."""
    return QueueItem(
        queue_id=queue_id,
        queue_item_id=track.item_id,
        name=track.name,
        duration=60,
        media_item=track,
    )


def _controller(snapshot: RecencySnapshot) -> PlayerQueuesController:
    """Build a bare controller wired to drive ``_handle_play_media`` with a real ``ManagedPool``."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    ctrl.mass = MagicMock()
    ctrl.mass.music.recency.snapshot = AsyncMock(return_value=snapshot)
    ctrl.mass.players.get_player = Mock(return_value=Mock(extra_data={}))
    lock_cm = MagicMock()
    lock_cm.__aenter__ = AsyncMock(return_value=None)
    lock_cm.__aexit__ = AsyncMock(return_value=None)
    ctrl.mass.players.get_player_lock = Mock(return_value=lock_cm)
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl.get_next_item = Mock(return_value=None)  # type: ignore[method-assign]
    ctrl.recency_windows = Mock(return_value=WINDOWS)  # type: ignore[method-assign]
    ctrl._smart_shuffle = Mock()
    ctrl._smart_shuffle.is_enabled = Mock(return_value=True)
    ctrl._smart_shuffle.windows = Mock(return_value=WINDOWS)
    ctrl._managed_pool = ManagedPool(ctrl)
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    # a carved-out NEXT track is expanded through the media resolver like on a linear queue;
    # a bare track resolves to just itself
    ctrl._media_resolver = Mock()
    ctrl._media_resolver._resolve_media_items = AsyncMock(
        side_effect=lambda item, *_args, **_kwargs: [item]
    )
    return ctrl


def _dynamic_playing_queue(
    ctrl: PlayerQueuesController, dynamic_candidates: list[Track]
) -> PlayerQueue:
    """Set up a playing dynamic queue: current item at index 0, fed by one dynamic radio source."""
    current = _track("current", artist="Cur")
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=1,
        state=PlaybackState.PLAYING,
        current_index=0,
        index_in_buffer=0,
        shuffle_enabled=True,
        is_dynamic=True,
    )
    ctrl._queue_data = {
        "q1": PlayerQueueData(
            queue=queue,
            items=[_queue_item("q1", current)],
            source_items=[_radio("dyn")],
        )
    }
    ctrl.get = Mock(return_value=queue)  # type: ignore[method-assign]
    ctrl.get_dynamic_source_tracks = AsyncMock(return_value=dynamic_candidates)  # type: ignore[method-assign]
    # a bare track source materializes to just itself
    ctrl.get_tracks_for_playback = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda item: [item] if isinstance(item, Track) else []
    )
    return queue


async def test_play_next_on_dynamic_queue_places_track_next() -> None:
    """NEXT of a track on a dynamic queue inserts it directly after the buffered index."""
    random.seed(4)
    snapshot = RecencySnapshot(now=NOW)  # nothing played recently
    ctrl = _controller(snapshot)
    dynamic_candidates = [_track(f"d{i}", artist=f"Artist{i}") for i in range(40)]
    _dynamic_playing_queue(ctrl, dynamic_candidates)
    wish = _track("wish", artist="Wish")

    await ctrl._handle_play_media("q1", wish, QueueOption.NEXT)

    items = ctrl._queue_data["q1"].items
    ids = [item.media_item.item_id for item in items if item.media_item is not None]
    # inserted right after the buffered index; no source changed so the tail stays untouched
    assert ids == ["current", "wish"], f"expected only ['current', 'wish'], got: {ids}"
    assert wish not in ctrl._queue_data["q1"].source_items


async def test_play_next_on_dynamic_queue_allows_recently_played_track() -> None:
    """NEXT of a track heard within the recency window still gets enqueued on a dynamic queue."""
    random.seed(4)
    replay = _track("replay", artist="Replay")
    # played 2 hours ago, well within the 1-day song window
    snapshot = RecencySnapshot(now=NOW, song_ts={("test", "replay"): NOW - 2 * 3600})
    ctrl = _controller(snapshot)
    dynamic_candidates = [_track(f"d{i}", artist=f"Artist{i}") for i in range(40)]
    _dynamic_playing_queue(ctrl, dynamic_candidates)

    await ctrl._handle_play_media("q1", replay, QueueOption.NEXT)

    items = ctrl._queue_data["q1"].items
    ids = [item.media_item.item_id for item in items if item.media_item is not None]
    assert ids[1] == "replay", f"expected 'replay' at index 1, got: {ids}"


async def test_play_next_on_linear_queue_inserts_after_current() -> None:
    """Control: NEXT of a track on a non-dynamic queue already inserts it right after current."""
    random.seed(4)
    snapshot = RecencySnapshot(now=NOW, song_ts={("test", "wish"): NOW - 2 * 3600})
    ctrl = _controller(snapshot)
    tail = [_track(f"t{i}", artist=f"Artist{i}") for i in range(5)]
    current = _track("current", artist="Cur")
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=6,
        state=PlaybackState.PLAYING,
        current_index=0,
        index_in_buffer=0,
        shuffle_enabled=True,
        is_dynamic=False,
    )
    ctrl._queue_data = {
        "q1": PlayerQueueData(
            queue=queue,
            items=[_queue_item("q1", current)] + [_queue_item("q1", t) for t in tail],
            source_items=[],
        )
    }
    ctrl.get = Mock(return_value=queue)  # type: ignore[method-assign]
    wish = _track("wish", artist="Wish")
    ctrl._media_resolver = Mock()
    ctrl._media_resolver._resolve_media_items = AsyncMock(return_value=[wish])

    await ctrl._handle_play_media("q1", wish, QueueOption.NEXT)

    items = ctrl._queue_data["q1"].items
    ids = [item.media_item.item_id for item in items if item.media_item is not None]
    assert ids[0] == "current"
    assert ids[1] == "wish", f"expected 'wish' at index 1: {ids}"


async def test_add_track_on_dynamic_queue_still_feeds_pool() -> None:
    """ADD of a track on a dynamic queue still feeds the pool as a source (unlike NEXT)."""
    random.seed(4)
    snapshot = RecencySnapshot(now=NOW)
    ctrl = _controller(snapshot)
    dynamic_candidates = [_track(f"d{i}", artist=f"Artist{i}") for i in range(40)]
    _dynamic_playing_queue(ctrl, dynamic_candidates)
    seed = _track("seed", artist="Seed")

    await ctrl._handle_play_media("q1", seed, QueueOption.ADD)

    items = ctrl._queue_data["q1"].items
    ids = [item.media_item.item_id for item in items if item.media_item is not None]
    # fed to the pool and mixed into the rebuilt tail, not inserted at index 1 like NEXT
    # (a one-shot track source is retired from source_items right after dispatch)
    assert "seed" in ids, f"expected 'seed' mixed into the pool tail: {ids}"
    assert ids[1] != "seed", f"expected 'seed' mixed into the pool tail, not at index 1: {ids}"
    # the pool actually ran (proving _enter_dynamic_mode fired), not left untouched
    assert any(item_id.startswith("d") for item_id in ids), f"pool tail was not rebuilt: {ids}"


async def test_play_next_container_on_dynamic_queue_feeds_pool() -> None:
    """NEXT of a container on a dynamic queue still feeds the pool as a source (unlike a track)."""
    random.seed(4)
    snapshot = RecencySnapshot(now=NOW)
    ctrl = _controller(snapshot)
    dynamic_candidates = [_track(f"d{i}", artist=f"Artist{i}") for i in range(40)]
    _dynamic_playing_queue(ctrl, dynamic_candidates)
    album = _album("alb")
    album_tracks = [_track(f"a{i}", artist=f"AlbArtist{i}") for i in range(30)]
    ctrl.get_tracks_for_playback = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda item: album_tracks if item is album else []
    )

    await ctrl._handle_play_media("q1", album, QueueOption.NEXT)

    ids = [
        item.media_item.item_id
        for item in ctrl._queue_data["q1"].items
        if item.media_item is not None
    ]
    # the album's tracks are mixed into the rebuilt tail and the album stays a pool source
    assert any(item_id.startswith("a") for item_id in ids), f"album did not feed the pool: {ids}"
    source_ids = {item.item_id for item in ctrl._queue_data["q1"].source_items}
    assert "alb" in source_ids, f"album missing from the pool sources: {source_ids}"
    assert ctrl._queue_data["q1"].queue.is_dynamic


async def test_play_next_mixed_batch_transition_plays_track_once() -> None:
    """NEXT of [track, dynamic radio] on a linear queue inserts the track next, exactly once."""
    random.seed(4)
    snapshot = RecencySnapshot(now=NOW)
    ctrl = _controller(snapshot)
    current = _track("current", artist="Cur")
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=1,
        state=PlaybackState.PLAYING,
        current_index=0,
        index_in_buffer=0,
        shuffle_enabled=False,
        is_dynamic=False,
    )
    ctrl._queue_data = {
        "q1": PlayerQueueData(queue=queue, items=[_queue_item("q1", current)], source_items=[])
    }
    ctrl.get = Mock(return_value=queue)  # type: ignore[method-assign]
    dynamic_candidates = [_track(f"d{i}", artist=f"Artist{i}") for i in range(10)]
    ctrl.get_dynamic_source_tracks = AsyncMock(return_value=dynamic_candidates)  # type: ignore[method-assign]
    ctrl.get_tracks_for_playback = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda item: [item] if isinstance(item, Track) else []
    )
    wish = _track("wish", artist="Wish")

    await ctrl._handle_play_media("q1", [wish, _radio("dyn")], QueueOption.NEXT)

    ids = [
        item.media_item.item_id
        for item in ctrl._queue_data["q1"].items
        if item.media_item is not None
    ]
    # the radio feeds the new pool; the play-next track is inserted next, exactly once
    assert ids.count("wish") == 1, f"'wish' must appear exactly once: {ids}"
    assert ids[1] == "wish", f"expected 'wish' at index 1 (play next), got: {ids}"
    assert not any(item.item_id == "wish" for item in ctrl._queue_data["q1"].source_items), (
        "play-next track must not be recorded as a pool source"
    )
    assert ctrl._queue_data["q1"].queue.is_dynamic
