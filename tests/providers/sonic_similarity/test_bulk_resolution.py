"""
Benchmark/regression harness for the candidate-resolution rewrite.

Scoring reranking used to resolve every candidate with a full
``tracks.get()`` inside an unbounded ``asyncio.gather`` — up to ~200 DB
round-trips + full model builds per /similar call, fired onto the event
loop. These tests lock in the bulk-resolution shape that replaces it:

* one ``get_library_items_by_prov_id`` query per distinct provider (not
  one ``tracks.get`` per candidate),
* unique ``(item_id, provider)`` keys deduped before the query,
* item-ids routed only to their own provider's query,
* library misses handled by a *bounded* provider fallback,
* filter + rerank sharing a single resolution,
* and — most importantly — byte-identical rerank output (parity), proven
  on a mixed two-provider-plus-miss fixture that exercises exactly the
  branches the rewrite introduces.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.errors import MusicAssistantError

from music_assistant.providers.sonic_similarity.constants import (
    METADATA_BONUS_SCALE,
    PROVIDER_FALLBACK_CONCURRENCY,
)
from music_assistant.providers.sonic_similarity.similarity import Candidate
from tests.providers.sonic_similarity.conftest import make_track

if TYPE_CHECKING:
    from collections.abc import Callable
    from unittest.mock import MagicMock

FS = "filesystem--x"
SP = "spotify"

# Rerank weights with both knobs live, so parity covers genre AND year bonuses.
WEIGHTS = {"genre": 0.6, "era": 0.2}


def _cand(item_id: str, provider: str, distance: float) -> Candidate:
    """Build a Candidate namedtuple with a throwaway feature vector."""
    return Candidate(
        item_id=item_id,
        provider=provider,
        features=[0.0] * 18,
        distance=distance,
        generation=0,
    )


def _wire_library(mock_mass: MagicMock, library: dict[tuple[str, str], MagicMock]) -> AsyncMock:
    """
    Wire ``get_library_items_by_prov_id`` to serve ``library`` in bulk.

    ``library`` is keyed by (provider, item_id). The mock returns exactly the
    requested ids that are present, mimicking a single ``WHERE ... IN`` query.
    """

    async def _bulk(
        *,
        provider_instance_id_or_domain: str,
        provider_item_ids: list[str],
        **_kw: Any,
    ) -> list[MagicMock]:
        return [
            library[(provider_instance_id_or_domain, iid)]
            for iid in provider_item_ids
            if (provider_instance_id_or_domain, iid) in library
        ]

    bulk = AsyncMock(side_effect=_bulk)
    mock_mass.music.tracks.get_library_items_by_prov_id = bulk
    return bulk


@pytest.mark.asyncio
async def test_resolves_via_bulk_query_not_per_item(
    make_plugin: Callable[..., Any], mock_mass: MagicMock
) -> None:
    """Candidate resolution issues one bulk query per provider, zero per-item tracks.get."""
    plugin = make_plugin()
    library = {
        (FS, "t1"): make_track("t1", provider=FS),
        (FS, "t2"): make_track("t2", provider=FS),
        (SP, "t3"): make_track("t3", provider=SP),
    }
    bulk = _wire_library(mock_mass, library)

    cands = [_cand("t1", FS, 0.5), _cand("t2", FS, 0.4), _cand("t3", SP, 0.45)]
    resolved = await plugin._resolve_candidate_track_map(cands)

    # No N+1: the per-item get() path is never taken for scoring.
    assert mock_mass.music.tracks.get.call_count == 0
    # One bulk query per distinct provider.
    assert bulk.call_count == 2
    assert resolved[("t1", FS)] is library[(FS, "t1")]
    assert resolved[("t3", SP)] is library[(SP, "t3")]


@pytest.mark.asyncio
async def test_item_ids_routed_only_to_their_provider(
    make_plugin: Callable[..., Any], mock_mass: MagicMock
) -> None:
    """Each provider's bulk query receives only that provider's item-ids."""
    plugin = make_plugin()
    library = {
        (FS, "t1"): make_track("t1", provider=FS),
        (FS, "t2"): make_track("t2", provider=FS),
        (SP, "t3"): make_track("t3", provider=SP),
    }
    bulk = _wire_library(mock_mass, library)

    await plugin._resolve_candidate_track_map(
        [_cand("t1", FS, 0.5), _cand("t2", FS, 0.4), _cand("t3", SP, 0.45)]
    )

    by_provider = {
        call.kwargs["provider_instance_id_or_domain"]: set(call.kwargs["provider_item_ids"])
        for call in bulk.call_args_list
    }
    assert by_provider == {FS: {"t1", "t2"}, SP: {"t3"}}


@pytest.mark.asyncio
async def test_duplicate_keys_deduped_before_query(
    make_plugin: Callable[..., Any], mock_mass: MagicMock
) -> None:
    """A repeated (item_id, provider) is queried once, not once per occurrence."""
    plugin = make_plugin()
    library = {(FS, "t1"): make_track("t1", provider=FS)}
    bulk = _wire_library(mock_mass, library)

    await plugin._resolve_candidate_track_map(
        [_cand("t1", FS, 0.5), _cand("t1", FS, 0.6), _cand("t1", FS, 0.7)]
    )

    assert bulk.call_count == 1
    assert bulk.call_args.kwargs["provider_item_ids"] == ["t1"]


@pytest.mark.asyncio
async def test_library_miss_uses_bounded_provider_fallback(
    make_plugin: Callable[..., Any], mock_mass: MagicMock
) -> None:
    """Candidates absent from the library resolve via get_provider_item; failures map to None."""
    plugin = make_plugin()
    library = {(SP, "hit"): make_track("hit", provider=SP)}
    _wire_library(mock_mass, library)

    miss_track = make_track("miss", provider=SP)

    async def _provider_item(item_id: str, _provider: str) -> MagicMock:
        if item_id == "boom":
            raise MusicAssistantError("unreachable")
        return miss_track

    mock_mass.music.tracks.get_provider_item = AsyncMock(side_effect=_provider_item)

    resolved = await plugin._resolve_candidate_track_map(
        [_cand("hit", SP, 0.4), _cand("miss", SP, 0.5), _cand("boom", SP, 0.6)]
    )

    assert resolved[("hit", SP)] is library[(SP, "hit")]
    assert resolved[("miss", SP)] is miss_track  # fell back to provider
    assert resolved[("boom", SP)] is None  # failed fallback → miss, not an exception
    # The library hit must NOT touch the network fallback.
    assert {c.args[0] for c in mock_mass.music.tracks.get_provider_item.call_args_list} == {
        "miss",
        "boom",
    }


@pytest.mark.asyncio
async def test_provider_fallback_concurrency_is_bounded(
    make_plugin: Callable[..., Any], mock_mass: MagicMock
) -> None:
    """The provider fallback never runs more than PROVIDER_FALLBACK_CONCURRENCY at once."""
    plugin = make_plugin()
    _wire_library(mock_mass, {})  # everything misses → all hit the fallback

    in_flight = 0
    peak = 0

    async def _slow_provider_item(item_id: str, _provider: str) -> MagicMock:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)  # yield so overlap is observable
        in_flight -= 1
        return make_track(item_id, provider=SP)

    mock_mass.music.tracks.get_provider_item = AsyncMock(side_effect=_slow_provider_item)

    cands = [_cand(f"m{i}", SP, 0.1 * i) for i in range(PROVIDER_FALLBACK_CONCURRENCY * 3)]
    await plugin._resolve_candidate_track_map(cands)

    assert peak <= PROVIDER_FALLBACK_CONCURRENCY


@pytest.mark.asyncio
async def test_rerank_parity_on_mixed_providers_and_miss(
    make_plugin: Callable[..., Any], mock_mass: MagicMock
) -> None:
    """
    Rerank output is byte-identical to the hand-computed reference.

    The fixture spans two providers, one library miss resolved via the
    provider fallback, and one unresolvable miss — the exact branches the
    rewrite introduces — so parity here means parity everywhere.
    """
    plugin = make_plugin()
    seed = make_track("seed1", provider="library", genres=["rock", "pop"], album_year=2000)
    plugin._provider_by_item_id["seed1"] = "library"
    mock_mass.music.tracks.get = AsyncMock(return_value=seed)

    library = {
        (FS, "t1"): make_track("t1", provider=FS, genres=["rock"], album_year=2000),
        (FS, "t2"): make_track("t2", provider=FS, genres=["jazz"], album_year=2010),
        (SP, "t3"): make_track("t3", provider=SP, genres=["rock", "pop"], album_year=1990),
    }
    _wire_library(mock_mass, library)

    t4 = make_track("t4", provider=SP, genres=["pop"], album_year=2000)

    async def _provider_item(item_id: str, _provider: str) -> MagicMock:
        if item_id == "t4":
            return t4
        raise MusicAssistantError("gone")  # t5 → unresolvable

    mock_mass.music.tracks.get_provider_item = AsyncMock(side_effect=_provider_item)

    cands = [
        _cand("t1", FS, 0.50),
        _cand("t2", FS, 0.40),
        _cand("t3", SP, 0.45),
        _cand("t4", SP, 0.60),
        _cand("t5", SP, 0.55),
    ]

    resolved = await plugin._resolve_candidate_track_map(cands)
    scored = await plugin._apply_metadata_reranking(["seed1"], cands, WEIGHTS, resolved)

    # Hand-computed: bonus = -(0.1*0.6*jaccard) - (0.1*0.2*1/(1+0.1*year_diff))
    g, e = METADATA_BONUS_SCALE * 0.6, METADATA_BONUS_SCALE * 0.2
    expected = {
        "t1": 0.50 - (g * 0.5 + e * 1.0),  # rock∩{rock,pop}=1/2 ; year 2000==2000
        "t2": 0.40 - (g * 0.0 + e * 0.5),  # jazz∩=0 ; |2010-2000|=10 → 1/2
        "t3": 0.45 - (g * 1.0 + e * 0.5),  # {rock,pop}=2/2 ; |1990-2000|=10 → 1/2
        "t4": 0.60 - (g * 0.5 + e * 1.0),  # pop=1/2 ; year 2000==2000 (via fallback)
        "t5": 0.55,  # unresolvable → no bonus
    }
    order = sorted(expected, key=lambda k: expected[k])

    assert [c.item_id for c in scored] == order
    for c in scored:
        assert c.distance == pytest.approx(expected[c.item_id])
    # Candidates never used the per-item get() path; only the single seed did.
    assert mock_mass.music.tracks.get.call_count == 1


@pytest.mark.asyncio
async def test_filter_and_rerank_share_one_resolution(
    make_plugin: Callable[..., Any], mock_mass: MagicMock
) -> None:
    """A call that both filters and reranks resolves each provider once, not twice."""
    plugin = make_plugin()
    seed = make_track("seed1", provider="library", genres=["rock"], album_year=2000)
    plugin._provider_by_item_id["seed1"] = "library"
    mock_mass.music.tracks.get = AsyncMock(return_value=seed)

    library = {
        (FS, "t1"): make_track("t1", provider=FS, genres=["rock"], album_year=2000),
        (FS, "t2"): make_track("t2", provider=FS, genres=["jazz"], album_year=2000),
        (SP, "t3"): make_track("t3", provider=SP, genres=["rock"], album_year=2000),
    }
    bulk = _wire_library(mock_mass, library)

    ctx = SimpleNamespace(
        params=SimpleNamespace(filter_genres=["rock"], exclude_artists=None),
        weights=WEIGHTS,
        valid_seed_ids=["seed1"],
    )
    cands = [_cand("t1", FS, 0.5), _cand("t2", FS, 0.4), _cand("t3", SP, 0.45)]

    result = await plugin._post_process_candidates(ctx, cands)

    # Filter (rock) drops t2; rerank orders the survivors.
    assert {c.item_id for c in result} == {"t1", "t3"}
    # One bulk query per provider TOTAL — filter and rerank shared the map.
    assert bulk.call_count == 2
