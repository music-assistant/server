"""Tests for the smart shuffle algorithm (player_queues/smart_shuffle.py)."""

from __future__ import annotations

import random

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.music.recency import RecencySnapshot, RecencyWindows
from music_assistant.controllers.player_queues.smart_shuffle import _arrange

NOW = 1_000_000_000
WEEK = 7 * 24 * 3600
GAP = 3 * 3600


def _track(song_id: str, artist: str) -> Track:
    """Build a library Track with a single artist and provider mapping."""
    return Track(
        item_id=song_id,
        provider="library",
        name=f"Song {song_id}",
        duration=180,
        artists=UniqueList(
            [
                ItemMapping(
                    item_id=artist.lower(),
                    provider="library",
                    name=artist,
                    media_type=MediaType.ARTIST,
                )
            ]
        ),
        provider_mappings={
            ProviderMapping(item_id=song_id, provider_domain="library", provider_instance="library")
        },
    )


def _item(song_id: str, *, artist: str | None = None, qiid: str | None = None) -> QueueItem:
    """Build a QueueItem wrapping a library Track (copies share song_id, differ by qiid)."""
    track = _track(song_id, artist or f"Artist {song_id}")
    return QueueItem(
        queue_id="q1",
        queue_item_id=qiid or f"qi-{song_id}",
        name=track.name,
        duration=180,
        media_item=track,
    )


def _snapshot(*, song_recent: tuple[str, ...] = (), played_at: int = NOW) -> RecencySnapshot:
    """Build a snapshot marking the given song ids as played at played_at."""
    return RecencySnapshot(now=NOW, song_ts={("library", sid): played_at for sid in song_recent})


def _ids(items: list[QueueItem]) -> list[str]:
    return [item.queue_item_id for item in items]


def _song_id(item: QueueItem) -> str:
    assert item.media_item is not None
    return item.media_item.item_id


def test_empty_returns_empty() -> None:
    """An empty queue arranges to empty."""
    assert _arrange([], _snapshot(), RecencyWindows()) == []


def test_single_item_preserved() -> None:
    """A single item is returned unchanged."""
    items = [_item("a")]
    assert _arrange(items, _snapshot(), RecencyWindows()) == items


def test_preserves_all_items_across_seeds() -> None:
    """Arranging never drops or duplicates items, regardless of the random seed."""
    items = [_item(f"s{i}", artist=f"A{i}") for i in range(12)]
    for seed in range(8):
        random.seed(seed)
        result = _arrange(list(items), _snapshot(), RecencyWindows())
        assert sorted(_ids(result)) == sorted(_ids(items))


def test_duplicates_spread_not_bursted() -> None:
    """Copies of an intentionally-duplicated song spread across the queue, never adjacent."""
    dup = [_item("x", artist="X", qiid=f"x{k}") for k in range(8)]
    fillers = [_item(f"f{i}", artist=f"A{i}") for i in range(24)]
    items = dup + fillers
    for seed in range(8):
        random.seed(seed)
        result = _arrange(list(items), _snapshot(), RecencyWindows())
        positions = [i for i, item in enumerate(result) if _song_id(item) == "x"]
        # spread across most of the queue rather than clustered in a burst
        assert positions[-1] - positions[0] >= len(items) // 2
        # no two copies of the same song are adjacent
        assert all(positions[i + 1] - positions[i] >= 2 for i in range(len(positions) - 1))


def test_recent_singletons_pushed_to_back() -> None:
    """Recently-played singletons sort behind fresh ones."""
    recent = [_item(f"r{i}", artist=f"R{i}") for i in range(6)]
    fresh = [_item(f"f{i}", artist=f"F{i}") for i in range(6)]
    snapshot = _snapshot(song_recent=tuple(f"r{i}" for i in range(6)))
    windows = RecencyWindows(song_seconds=WEEK, artist_seconds=0, duplicate_gap_seconds=GAP)
    for seed in range(8):
        random.seed(seed)
        result = _arrange(recent + fresh, snapshot, windows)
        index = {_song_id(item): i for i, item in enumerate(result)}
        assert max(index[f"f{i}"] for i in range(6)) < min(index[f"r{i}"] for i in range(6))


def test_duplicated_song_not_buried_like_singleton() -> None:
    """A song heard 5h ago is buried as a singleton (week window) but fresh as a duplicate (3h gap)."""
    snapshot = _snapshot(song_recent=("dup", "single"), played_at=NOW - 5 * 3600)
    windows = RecencyWindows(song_seconds=WEEK, artist_seconds=0, duplicate_gap_seconds=GAP)
    dup = [_item("dup", artist="D", qiid=f"d{k}") for k in range(4)]
    single = _item("single", artist="S", qiid="single")
    fresh = [_item(f"f{i}", artist=f"F{i}") for i in range(8)]
    random.seed(1)
    result = _arrange([*dup, single, *fresh], snapshot, windows)
    index = {item.queue_item_id: i for i, item in enumerate(result)}
    # the duplicate (fresh, tier 0) lands ahead of the buried singleton (tier 2)
    assert max(index[f"d{k}"] for k in range(4)) < index["single"]


def test_all_recent_never_drops() -> None:
    """When every track is recently played, all items are still present (reordered, not dropped)."""
    items = [_item(f"s{i}", artist=f"A{i}") for i in range(10)]
    snapshot = _snapshot(song_recent=tuple(f"s{i}" for i in range(10)))
    windows = RecencyWindows(song_seconds=WEEK)
    random.seed(0)
    result = _arrange(list(items), snapshot, windows)
    assert sorted(_ids(result)) == sorted(_ids(items))


def test_deterministic_under_seed() -> None:
    """Same seed + same inputs yield an identical arrangement."""
    items = [_item(f"s{i}", artist=f"A{i % 3}") for i in range(15)]
    random.seed(123)
    first = _ids(_arrange(list(items), _snapshot(), RecencyWindows()))
    random.seed(123)
    second = _ids(_arrange(list(items), _snapshot(), RecencyWindows()))
    assert first == second
