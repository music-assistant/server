"""Unit tests for the ClapIndex wrapper.

Covers deterministic hashing, add/search round-trip, save + reload
persistence, and reset semantics. All tests use a pytest tmp_path so no
real storage state is touched.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import numpy as np
import pytest

from music_assistant.providers.sonic_analysis.clap_index import (
    CLAP_EMBEDDING_DIM,
    ClapIndex,
    derive_label,
)

if TYPE_CHECKING:
    from pathlib import Path

    from music_assistant.mass import MusicAssistant


@pytest.fixture
def logger() -> logging.Logger:
    """Return a silent logger for test isolation."""
    lg = logging.getLogger("test_clap_index")
    lg.addHandler(logging.NullHandler())
    return lg


def _fake_mass(storage_path: Path) -> MusicAssistant:
    """Minimal stand-in for MusicAssistant — ClapIndex only uses storage_path."""
    return cast("MusicAssistant", SimpleNamespace(storage_path=str(storage_path)))


def _random_embedding(seed: int = 0) -> np.ndarray:
    """Produce a deterministic pseudo-random 1024-dim embedding."""
    rng = np.random.default_rng(seed)
    return rng.standard_normal(CLAP_EMBEDDING_DIM).astype(np.float32)


def test_derive_label_deterministic() -> None:
    """Same inputs produce same label across calls."""
    assert derive_label("spotify", "track_123") == derive_label("spotify", "track_123")


def test_derive_label_differs_across_tracks() -> None:
    """Distinct track identifiers hash to distinct labels."""
    labels = {derive_label("spotify", f"t{i}") for i in range(50)}
    assert len(labels) == 50


def test_derive_label_separator_unambiguous() -> None:
    """Colons inside item_id don't collide with provider+separator concatenation.

    Without a NUL separator, ('a', 'b:c') and ('a:b', 'c') would both produce
    'a:b:c'. The NUL separator prevents this.
    """
    a = derive_label("a", "b:c")
    b = derive_label("a:b", "c")
    assert a != b


@pytest.mark.asyncio
async def test_empty_index_returns_no_results(tmp_path: Path, logger: logging.Logger) -> None:
    """Fresh index should report length 0 and return no search matches."""
    idx = ClapIndex(_fake_mass(tmp_path), logger, filename_stem="test_empty")
    await idx.load()
    assert len(idx) == 0
    results = await idx.search(_random_embedding(), k=5)
    assert results == []
    await idx.close()


@pytest.mark.asyncio
async def test_add_contains_roundtrip(tmp_path: Path, logger: logging.Logger) -> None:
    """After add(), contains() returns True for that track and False for others."""
    idx = ClapIndex(_fake_mass(tmp_path), logger, filename_stem="test_add")
    await idx.load()
    await idx.add("spotify", "track_1", _random_embedding(seed=1))
    assert idx.contains("spotify", "track_1") is True
    assert idx.contains("spotify", "track_2") is False
    assert idx.contains("tidal", "track_1") is False
    await idx.close()


@pytest.mark.asyncio
async def test_search_finds_exact_match_closest(tmp_path: Path, logger: logging.Logger) -> None:
    """A query embedding equal to a stored embedding should rank that track first."""
    idx = ClapIndex(_fake_mass(tmp_path), logger, filename_stem="test_search")
    await idx.load()
    emb_a = _random_embedding(seed=1)
    emb_b = _random_embedding(seed=2)
    await idx.add("spotify", "track_a", emb_a)
    await idx.add("spotify", "track_b", emb_b)

    results = await idx.search(emb_a, k=2)
    assert len(results) == 2
    # top result should be track_a with very small cosine distance
    assert results[0][0] == "spotify"
    assert results[0][1] == "track_a"
    assert results[0][2] < 0.01
    await idx.close()


@pytest.mark.asyncio
async def test_get_embedding_by_item_id_round_trip(tmp_path: Path, logger: logging.Logger) -> None:
    """get_embedding_by_item_id returns (provider, vector) for a stored track."""
    idx = ClapIndex(_fake_mass(tmp_path), logger, filename_stem="test_get_emb")
    await idx.load()
    emb = _random_embedding(seed=7)
    await idx.add("spotify", "track_xyz", emb)

    result = idx.get_embedding_by_item_id("track_xyz")
    assert result is not None
    provider, vec = result
    assert provider == "spotify"
    assert vec.shape == (CLAP_EMBEDDING_DIM,)
    # usearch f16 storage incurs tiny precision loss; tolerate it
    assert np.allclose(vec, emb, atol=1e-2)
    await idx.close()


@pytest.mark.asyncio
async def test_get_embedding_by_item_id_missing_returns_none(
    tmp_path: Path, logger: logging.Logger
) -> None:
    """Missing item_id returns None rather than raising."""
    idx = ClapIndex(_fake_mass(tmp_path), logger, filename_stem="test_get_emb_missing")
    await idx.load()
    assert idx.get_embedding_by_item_id("not_in_index") is None
    await idx.close()


@pytest.mark.asyncio
async def test_query_sync_matches_async_search(tmp_path: Path, logger: logging.Logger) -> None:
    """query_sync returns identical results to await search() for sync callers."""
    idx = ClapIndex(_fake_mass(tmp_path), logger, filename_stem="test_query_sync")
    await idx.load()
    emb_a = _random_embedding(seed=1)
    emb_b = _random_embedding(seed=2)
    await idx.add("spotify", "track_a", emb_a)
    await idx.add("spotify", "track_b", emb_b)

    sync_results = idx.query_sync(emb_a, k=2)
    async_results = await idx.search(emb_a, k=2)
    assert sync_results == async_results
    assert sync_results[0][1] == "track_a"
    await idx.close()


@pytest.mark.asyncio
async def test_save_and_reload_preserves_state(tmp_path: Path, logger: logging.Logger) -> None:
    """Saving then loading a fresh ClapIndex should restore all entries."""
    mass = _fake_mass(tmp_path)
    idx = ClapIndex(mass, logger, filename_stem="test_reload")
    await idx.load()
    emb = _random_embedding(seed=42)
    await idx.add("spotify", "track_x", emb)
    await idx.save()
    assert len(idx) == 1

    # Construct a new instance pointing at the same storage path / stem
    idx2 = ClapIndex(mass, logger, filename_stem="test_reload")
    await idx2.load()
    assert len(idx2) == 1
    assert idx2.contains("spotify", "track_x") is True
    results = await idx2.search(emb, k=1)
    assert results[0][:2] == ("spotify", "track_x")
    await idx2.close()


@pytest.mark.asyncio
async def test_reset_clears_state_and_files(tmp_path: Path, logger: logging.Logger) -> None:
    """reset() should empty the index and delete on-disk files."""
    mass = _fake_mass(tmp_path)
    idx = ClapIndex(mass, logger, filename_stem="test_reset")
    await idx.load()
    await idx.add("spotify", "track_1", _random_embedding(seed=1))
    await idx.save()
    assert idx._index_path.exists()
    assert idx._keys_path.exists()

    await idx.reset()
    assert len(idx) == 0
    assert not idx._index_path.exists()
    assert not idx._keys_path.exists()
    await idx.close()


@pytest.mark.asyncio
async def test_readd_same_track_replaces(tmp_path: Path, logger: logging.Logger) -> None:
    """Calling add() again for the same track should not error and should leave count unchanged."""
    idx = ClapIndex(_fake_mass(tmp_path), logger, filename_stem="test_readd")
    await idx.load()
    await idx.add("spotify", "track_1", _random_embedding(seed=1))
    await idx.add("spotify", "track_1", _random_embedding(seed=2))
    assert len(idx) == 1
    await idx.close()


@pytest.mark.asyncio
async def test_rejects_wrong_shape(tmp_path: Path, logger: logging.Logger) -> None:
    """Embeddings of unexpected shape should be rejected without raising."""
    idx = ClapIndex(_fake_mass(tmp_path), logger, filename_stem="test_shape")
    await idx.load()
    bogus = np.zeros(42, dtype=np.float32)
    await idx.add("spotify", "track_1", bogus)
    assert idx.contains("spotify", "track_1") is False
    assert len(idx) == 0
    await idx.close()
