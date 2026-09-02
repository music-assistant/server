"""Tests for the managed-pool refill allocator (player_queues/managed_pool.py)."""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Sequence
from itertools import groupby
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, ProviderMapping, SoundEffect, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import DynamicFeedItem
from music_assistant.controllers.music.recency import RecencySnapshot, RecencyWindows, song_keys
from music_assistant.controllers.player_queues.managed_pool import (
    DynamicFillMode,
    DynamicSource,
    ManagedPool,
    PoolWeightModel,
    allocate_refill,
    gate_tracks,
)

NOW = 1_000_000_000
HOUR = 3600
WEEK = 7 * 24 * HOUR
GAP = 3 * HOUR


def _track(item_id: str) -> Track:
    """Build a Track on the 'test' provider with a single artist and provider mapping."""
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


def _artist_track(item_id: str, artist: str) -> Track:
    """Build a Track on the 'test' provider with the given single named artist."""
    return Track(
        item_id=item_id,
        provider="test",
        name=f"Track {item_id}",
        duration=60,
        artists=UniqueList(
            [
                ItemMapping(
                    item_id=artist.lower(),
                    provider="test",
                    name=artist,
                    media_type=MediaType.ARTIST,
                )
            ]
        ),
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def _version_track(item_id: str, name: str, artist: str) -> Track:
    """Build a Track with an explicit title and single named artist."""
    return Track(
        item_id=item_id,
        provider="test",
        name=name,
        duration=60,
        artists=UniqueList(
            [
                ItemMapping(
                    item_id=artist.lower(),
                    provider="test",
                    name=artist,
                    media_type=MediaType.ARTIST,
                )
            ]
        ),
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def _source(
    candidate_ids: list[str],
    *,
    multiplicity: int = 1,
    fill_mode: DynamicFillMode = DynamicFillMode.TRACKS,
) -> DynamicSource:
    """Build a DynamicSource with the given candidate track ids."""
    return DynamicSource(
        media_item=_track("seed"),
        multiplicity=multiplicity,
        fill_mode=fill_mode,
        candidates=[_track(cid) for cid in candidate_ids],
    )


def _artist_source(
    pairs: list[tuple[str, str]],
    *,
    multiplicity: int = 1,
    fill_mode: DynamicFillMode = DynamicFillMode.TRACKS,
) -> DynamicSource:
    """Build a DynamicSource from (track_id, artist) pairs."""
    return DynamicSource(
        media_item=_track("seed"),
        multiplicity=multiplicity,
        fill_mode=fill_mode,
        candidates=[_artist_track(item_id, artist) for item_id, artist in pairs],
    )


def _snapshot(
    played: dict[str, int] | None = None, *, artists_played: dict[str, int] | None = None
) -> RecencySnapshot:
    """Build a snapshot marking the given track ids (and artist names) as played."""
    return RecencySnapshot(
        now=NOW,
        song_ts={("test", item_id): ts for item_id, ts in (played or {}).items()},
        artist_ts={name.lower(): ts for name, ts in (artists_played or {}).items()},
    )


def _artists(items: Sequence[DynamicFeedItem]) -> list[str]:
    return [item.artists[0].name for item in items if isinstance(item, Track)]


def _ids(items: Sequence[DynamicFeedItem]) -> list[str]:
    return [item.item_id for item in items]


def test_empty_slots_returns_empty() -> None:
    """Asking for zero (or fewer) slots returns nothing."""
    source = _source(["a", "b"])
    assert (
        allocate_refill(
            [source], slots=0, pool_keys=set(), snapshot=_snapshot(), windows=RecencyWindows()
        )
        == []
    )


def test_no_sources_returns_empty() -> None:
    """No sources returns nothing."""
    assert (
        allocate_refill(
            [], slots=5, pool_keys=set(), snapshot=_snapshot(), windows=RecencyWindows()
        )
        == []
    )


def test_per_base_quota_equal_share() -> None:
    """Two equally-weighted sources split the slots evenly, regardless of catalogue size."""
    sources = [
        _source([f"a{i}" for i in range(20)]),
        _source([f"b{i}" for i in range(20)]),
    ]
    result = allocate_refill(
        sources, slots=10, pool_keys=set(), snapshot=_snapshot(), windows=RecencyWindows()
    )
    counts = Counter("a" if tid.startswith("a") else "b" for tid in _ids(result))
    assert counts["a"] == 5
    assert counts["b"] == 5


def test_multiplicity_increases_share() -> None:
    """A source added 3x gets ~3x the slots of one added once (per-base quota)."""
    sources = [
        _source([f"a{i}" for i in range(20)], multiplicity=1),
        _source([f"b{i}" for i in range(20)], multiplicity=3),
    ]
    result = allocate_refill(
        sources, slots=8, pool_keys=set(), snapshot=_snapshot(), windows=RecencyWindows()
    )
    counts = Counter("a" if tid.startswith("a") else "b" for tid in _ids(result))
    assert counts["b"] == 6
    assert counts["a"] == 2


def test_weighted_sources_are_spread_across_batch() -> None:
    """A higher-weight source is mixed through the batch instead of emitted as one block."""
    sources = [
        _source([f"a{i}" for i in range(20)]),
        _source([f"b{i}" for i in range(20)], multiplicity=2),
        _source([f"c{i}" for i in range(20)]),
        _source([f"d{i}" for i in range(20)]),
    ]
    random.seed(0)
    result = allocate_refill(
        sources, slots=25, pool_keys=set(), snapshot=_snapshot(), windows=RecencyWindows()
    )
    source_ids = [track.item_id[0] for track in result]
    longest_run = max(sum(1 for _ in run) for _, run in groupby(source_ids))
    assert longest_run <= 2


def test_size_multiplicity_weights_by_catalogue_size() -> None:
    """Under SIZE_MULTIPLICITY, the larger-catalogue source dominates the pool."""
    sources = [
        _source(["a0", "a1"], multiplicity=1),
        _source([f"b{i}" for i in range(20)], multiplicity=1),
    ]
    result = allocate_refill(
        sources,
        slots=11,
        pool_keys=set(),
        snapshot=_snapshot(),
        windows=RecencyWindows(),
        weight_model=PoolWeightModel.SIZE_MULTIPLICITY,
    )
    counts = Counter("a" if tid.startswith("a") else "b" for tid in _ids(result))
    # weights 2 vs 20 -> b takes the lion's share; a is capped by its 2 candidates
    assert counts["b"] > counts["a"]
    assert counts["a"] <= 2


def test_repeat_gap_hard_exclusion() -> None:
    """A duplicated source's candidate played within the repeat-gap is excluded entirely."""
    windows = RecencyWindows(song_seconds=WEEK, duplicate_gap_seconds=GAP)
    sources = [_source(["hot", "cold"], multiplicity=2)]
    snapshot = _snapshot({"hot": NOW - HOUR, "cold": NOW - 10 * HOUR})
    result = allocate_refill(sources, slots=10, pool_keys=set(), snapshot=snapshot, windows=windows)
    assert "hot" not in _ids(result)
    assert "cold" in _ids(result)


def test_singleton_window_vs_duplicate_gap() -> None:
    """A track 5h old is buried as a singleton (week window) but fresh as a duplicate (3h gap)."""
    windows = RecencyWindows(song_seconds=WEEK, duplicate_gap_seconds=GAP)
    singleton = _source(["s"], multiplicity=1)
    duplicate = _source(["d"], multiplicity=2)
    snapshot = _snapshot({"s": NOW - 5 * HOUR, "d": NOW - 5 * HOUR})
    result = allocate_refill(
        [singleton, duplicate], slots=10, pool_keys=set(), snapshot=snapshot, windows=windows
    )
    assert "s" not in _ids(result)
    assert "d" in _ids(result)


def test_least_recently_played_first() -> None:
    """A dynamic batch is ordered never-played first, then oldest play before most recent."""
    windows = RecencyWindows(song_seconds=0)  # gate off so we only test ordering
    source = _source(["recent", "old", "never"], fill_mode=DynamicFillMode.DYNAMIC)
    snapshot = _snapshot({"recent": NOW - 10, "old": NOW - 100_000})
    result = allocate_refill([source], slots=3, pool_keys=set(), snapshot=snapshot, windows=windows)
    assert _ids(result) == ["never", "old", "recent"]


def test_tracks_mode_preserves_candidate_order() -> None:
    """A finite (TRACKS) source keeps its materialized order instead of re-sorting by recency."""
    windows = RecencyWindows(song_seconds=0)  # gate off so we only test ordering
    source = _source(["recent", "old", "never"], fill_mode=DynamicFillMode.TRACKS)
    snapshot = _snapshot({"recent": NOW - 10, "old": NOW - 100_000})
    result = allocate_refill([source], slots=3, pool_keys=set(), snapshot=snapshot, windows=windows)
    assert _ids(result) == ["recent", "old", "never"]


def test_pool_keys_excluded() -> None:
    """A candidate already in the pool is never re-added."""
    source = _source(["a", "b", "c"])
    in_pool = _track("b")
    result = allocate_refill(
        [source], slots=10, pool_keys={in_pool}, snapshot=_snapshot(), windows=RecencyWindows()
    )
    assert "b" not in _ids(result)
    assert set(_ids(result)) == {"a", "c"}


def test_pool_song_keys_exclude_other_version() -> None:
    """A different catalog version of an already-queued song is skipped too."""
    queued = _version_track("amber-1", "Amber", "The Thrillseekers")
    other_version = _version_track("amber-2", "Amber", "The Thrillseekers")
    fresh = _version_track("other", "Two Bodies", "Flight Facilities")
    source = DynamicSource(
        media_item=_track("seed"),
        multiplicity=1,
        fill_mode=DynamicFillMode.TRACKS,
        candidates=[other_version, fresh],
    )
    result = allocate_refill(
        [source],
        slots=10,
        pool_keys={queued},
        pool_song_keys=song_keys(queued),
        snapshot=_snapshot(),
        windows=RecencyWindows(),
    )
    assert _ids(result) == ["other"]


def test_batch_never_contains_two_versions_of_same_song() -> None:
    """Two catalog versions of the same song offered in one refill yield only one pick."""
    sources = [
        DynamicSource(
            media_item=_track("seed"),
            multiplicity=1,
            fill_mode=DynamicFillMode.DYNAMIC,
            candidates=[
                _version_track("amber-1", "Amber", "The Thrillseekers"),
                _version_track("amber-2", "Amber (Remastered 2019)", "The Thrillseekers"),
                _version_track("other", "Two Bodies", "Flight Facilities"),
            ],
        )
    ]
    result = allocate_refill(
        sources, slots=10, pool_keys=set(), snapshot=_snapshot(), windows=RecencyWindows()
    )
    assert len([tid for tid in _ids(result) if tid.startswith("amber")]) == 1
    assert "other" in _ids(result)


def test_never_exceeds_slots() -> None:
    """The result never contains more than the requested number of slots."""
    source = _source([f"a{i}" for i in range(50)])
    result = allocate_refill(
        [source], slots=7, pool_keys=set(), snapshot=_snapshot(), windows=RecencyWindows()
    )
    assert len(result) == 7


def test_no_duplicates_across_sources() -> None:
    """A track offered by two sources is added only once."""
    shared = [f"x{i}" for i in range(10)]
    sources = [_source(shared), _source(shared)]
    result = allocate_refill(
        sources, slots=10, pool_keys=set(), snapshot=_snapshot(), windows=RecencyWindows()
    )
    assert len(_ids(result)) == len(set(_ids(result)))


def test_all_gated_falls_back_ungated() -> None:
    """When every candidate is within the window, the ungated least-recently-played set is used."""
    windows = RecencyWindows(song_seconds=WEEK)
    source = _source(["a", "b", "c"])
    snapshot = _snapshot({"a": NOW - HOUR, "b": NOW - 2 * HOUR, "c": NOW - 3 * HOUR})
    result = allocate_refill([source], slots=2, pool_keys=set(), snapshot=snapshot, windows=windows)
    # all are recent, but playback must not stall: the two least-recently-played come back
    assert _ids(result) == ["c", "b"]


def test_randomized_order_is_reproducible_under_seed() -> None:
    """A fixed seed reproduces the interleave while a different seed varies it."""
    sources = [
        _source([f"a{i}" for i in range(10)], multiplicity=2),
        _source([f"b{i}" for i in range(10)]),
    ]
    snapshot = _snapshot({"a3": NOW - 10, "b1": NOW - 20})
    windows = RecencyWindows(song_seconds=WEEK, duplicate_gap_seconds=GAP)
    random.seed(123)
    first = _ids(
        allocate_refill(sources, slots=6, pool_keys=set(), snapshot=snapshot, windows=windows)
    )
    random.seed(123)
    second = _ids(
        allocate_refill(sources, slots=6, pool_keys=set(), snapshot=snapshot, windows=windows)
    )
    random.seed(124)
    third = _ids(
        allocate_refill(sources, slots=6, pool_keys=set(), snapshot=snapshot, windows=windows)
    )
    assert first == second
    assert first != third


def test_gate_tracks_drops_recent() -> None:
    """gate_tracks drops tracks played within the song window, keeping order."""
    windows = RecencyWindows(song_seconds=WEEK)
    tracks = [_track("a"), _track("b"), _track("c")]
    snapshot = _snapshot({"b": NOW - HOUR})
    assert _ids(gate_tracks(tracks, snapshot, windows)) == ["a", "c"]


def test_gate_tracks_fallback_when_all_recent() -> None:
    """gate_tracks returns the ungated list when every track is within the window."""
    windows = RecencyWindows(song_seconds=WEEK)
    tracks = [_track("a"), _track("b")]
    snapshot = _snapshot({"a": NOW - HOUR, "b": NOW - 2 * HOUR})
    assert _ids(gate_tracks(tracks, snapshot, windows)) == ["a", "b"]


def test_spaces_adjacent_same_artist() -> None:
    """The assembled batch never places two same-artist tracks directly adjacent."""
    windows = RecencyWindows(song_seconds=0)  # gate off; test ordering only
    source = _artist_source(
        [("a1", "A"), ("a2", "A"), ("a3", "A"), ("b1", "B"), ("c1", "C")],
    )
    result = allocate_refill(
        [source], slots=5, pool_keys=set(), snapshot=_snapshot(), windows=windows
    )
    artists = _artists(result)
    assert len(result) == 5  # spacing reorders, never drops
    assert all(artists[i] != artists[i + 1] for i in range(len(artists) - 1))


def test_seam_avoids_preceding_artist() -> None:
    """The first added track is kept clear of the artist that plays right before the batch."""
    windows = RecencyWindows(song_seconds=0)
    source = _artist_source([("a1", "A"), ("b1", "B"), ("c1", "C")])
    result = allocate_refill(
        [source],
        slots=3,
        pool_keys=set(),
        snapshot=_snapshot(),
        windows=windows,
        preceding_artists={"a"},
    )
    assert len(result) == 3
    assert _artists(result)[0] != "A"


def test_artist_recency_deprioritized() -> None:
    """A dynamic candidate whose artist is within the artist window sorts behind fresh ones."""
    windows = RecencyWindows(song_seconds=0, artist_seconds=1800)
    source = _artist_source([("r1", "Recent"), ("f1", "Fresh")], fill_mode=DynamicFillMode.DYNAMIC)
    snapshot = _snapshot(artists_played={"Recent": NOW - 600})
    result = allocate_refill([source], slots=2, pool_keys=set(), snapshot=snapshot, windows=windows)
    # the fresh-artist track leads even though it appears later in the candidate list
    assert _artists(result) == ["Fresh", "Recent"]


def test_artist_recency_not_hard_excluded() -> None:
    """A within-window artist is only nudged back, never dropped (single-artist stations still play)."""
    windows = RecencyWindows(song_seconds=0, artist_seconds=1800)
    source = _artist_source([("a1", "A"), ("a2", "A")], fill_mode=DynamicFillMode.DYNAMIC)
    snapshot = _snapshot(artists_played={"A": NOW - 600})
    result = allocate_refill([source], slots=5, pool_keys=set(), snapshot=snapshot, windows=windows)
    assert len(result) == 2  # both kept despite the artist being recently heard


@pytest.mark.asyncio
async def test_fill_reconciles_after_smart_fade_reordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reordering a refill must not skip finite-source bookkeeping."""
    queues = MagicMock()
    pool = ManagedPool(queues)
    queue = MagicMock(queue_id="q1", current_index=None)
    tail = _artist_track("tail", "Tail Artist")
    queue_data = SimpleNamespace(
        queue=queue,
        userid=None,
        items=[SimpleNamespace(media_item=tail)],
    )

    queues.queue_data_or_none.return_value = queue_data
    queues.recency_windows.return_value = RecencyWindows()
    queues.smart_fade_ordering_enabled.return_value = True
    queues.mass.music.recency.snapshot = AsyncMock(return_value=_snapshot())

    source = _source(["a", "b"], fill_mode=DynamicFillMode.TRACKS)
    pool._collect_sources = AsyncMock(return_value=[source])  # type: ignore[method-assign]
    reconcile = AsyncMock()
    pool._reconcile_tracks = reconcile  # type: ignore[method-assign]

    reordered = list(reversed(source.candidates))
    order_tracks = AsyncMock(return_value=reordered)
    monkeypatch.setattr(
        "music_assistant.controllers.player_queues.managed_pool.order_tracks",
        order_tracks,
    )

    result = await pool.fill("q1", is_initial=False)

    assert result == reordered

    order_call = order_tracks.await_args
    assert order_call is not None
    assert order_call.kwargs["preceding_track"] is tail

    reconcile.assert_awaited_once()
    call = reconcile.await_args
    assert call is not None
    assert call.args[2] == reordered


def _sound_effect(item_id: str) -> SoundEffect:
    """Build a SoundEffect on the 'test' provider, as a station weaves into its own feed."""
    return SoundEffect(
        item_id=item_id,
        provider="test",
        name=f"Clip {item_id}",
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


def test_dynamic_feed_sound_effect_leads_its_batch() -> None:
    """A sound effect heading a dynamic batch is chosen with the tracks and stays in front."""
    windows = RecencyWindows(song_seconds=WEEK, artist_seconds=1800)
    source = DynamicSource(
        media_item=_track("seed"),
        multiplicity=1,
        fill_mode=DynamicFillMode.DYNAMIC,
        candidates=[_sound_effect("intro"), _artist_track("t1", "A"), _artist_track("t2", "B")],
    )
    # the tail's last artist clashes with the first track, which must not pull it in front of
    # the artist-less clip
    result = allocate_refill(
        [source],
        slots=3,
        pool_keys=set(),
        snapshot=_snapshot(artists_played={"A": NOW - 600}),
        windows=windows,
        preceding_artists={"a"},
    )
    assert _ids(result) == ["intro", "t2", "t1"]
