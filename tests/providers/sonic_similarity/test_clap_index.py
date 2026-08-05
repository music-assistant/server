"""
Smoke tests for the optional 1024-dim CLAP usearch index helper.

Round-trip coverage: deterministic labels, add/contains/get, and
persistence to the sonic_similarity_clap.usearch filename stem under
the configured storage_path.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest

from music_assistant.providers.sonic_similarity.clap_index import (
    CLAP_EMBEDDING_DIM,
    ClapIndex,
    derive_label,
)

if TYPE_CHECKING:
    from types import SimpleNamespace


@pytest.fixture
def logger() -> logging.Logger:
    """Provide a quiet logger for the tests."""
    lg = logging.getLogger("test_clap_index_relocation")
    lg.addHandler(logging.NullHandler())
    return lg


def _make_mass(tmp_path: Path) -> SimpleNamespace:
    """Build a minimal mass stand-in exposing storage_path."""
    from types import SimpleNamespace  # noqa: PLC0415

    return SimpleNamespace(storage_path=str(tmp_path))


def _unit_vec(seed: int) -> np.ndarray:
    """Return a deterministic L2-normalized 1024-dim vector."""
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(CLAP_EMBEDDING_DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def test_derive_label_is_deterministic_and_provider_aware() -> None:
    """Same inputs → same label; different providers give different labels."""
    a = derive_label("spotify", "track123")
    b = derive_label("spotify", "track123")
    c = derive_label("tidal", "track123")
    assert a == b
    assert a != c


@pytest.mark.asyncio
async def test_round_trip_persists_under_sonic_similarity_stem(
    tmp_path: Path, logger: logging.Logger
) -> None:
    """add/contains/get_embedding_by_item_id round-trip + file persistence check."""
    idx = ClapIndex(_make_mass(tmp_path), logger)  # type: ignore[arg-type]
    await idx.load()

    vec = _unit_vec(1)
    await idx.add("spotify", "track_xyz", vec)

    assert idx.contains("spotify", "track_xyz")
    result = idx.get_embedding_by_item_id("track_xyz")
    assert result is not None
    prov, stored_vec = result
    assert prov == "spotify"
    assert stored_vec.shape == (CLAP_EMBEDDING_DIM,)

    await idx.save()
    assert (tmp_path / "sonic_similarity_clap.usearch").exists()
    assert (tmp_path / "sonic_similarity_clap_keys.json").exists()


@pytest.mark.asyncio
async def test_get_embedding_by_item_id_missing_returns_none(
    tmp_path: Path, logger: logging.Logger
) -> None:
    """Lookup of an unknown item returns None, not an exception."""
    idx = ClapIndex(_make_mass(tmp_path), logger)  # type: ignore[arg-type]
    await idx.load()
    assert idx.get_embedding_by_item_id("not_in_index") is None


@pytest.mark.asyncio
async def test_get_embedding_o1_lookup_matches_provider(
    tmp_path: Path, logger: logging.Logger
) -> None:
    """get_embedding returns the vector for the right provider and None otherwise."""
    idx = ClapIndex(_make_mass(tmp_path), logger)  # type: ignore[arg-type]
    await idx.load()
    vec = _unit_vec(7)
    await idx.add("spotify", "track_xyz", vec)

    stored = idx.get_embedding("spotify", "track_xyz")
    assert stored is not None
    assert stored.shape == (CLAP_EMBEDDING_DIM,)
    np.testing.assert_allclose(stored, vec, atol=1e-3)

    # Same item_id under a different provider derives a different label → miss.
    assert idx.get_embedding("tidal", "track_xyz") is None
    assert idx.get_embedding("spotify", "not_in_index") is None


@pytest.mark.asyncio
async def test_save_does_not_leave_tmp_file_behind(tmp_path: Path, logger: logging.Logger) -> None:
    """The atomic-rename path consumes the .tmp file; nothing lingers after success."""
    idx = ClapIndex(_make_mass(tmp_path), logger)  # type: ignore[arg-type]
    await idx.load()
    await idx.add("spotify", "track1", _unit_vec(1))

    await idx.save()

    assert not (tmp_path / "sonic_similarity_clap_keys.json.tmp").exists()
    assert (tmp_path / "sonic_similarity_clap_keys.json").exists()


@pytest.mark.asyncio
async def test_save_writes_keys_before_index_so_a_crash_doesnt_orphan_labels(
    tmp_path: Path,
    logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the binary index save fails mid-flight, the keys file is already up-to-date."""
    idx = ClapIndex(_make_mass(tmp_path), logger)  # type: ignore[arg-type]
    await idx.load()
    await idx.add("spotify", "track1", _unit_vec(1))

    # Force the index save to fail after the keys file has already been written.
    def _boom(_path: str) -> None:
        raise RuntimeError("disk full mid-save")

    monkeypatch.setattr(idx._index, "save", _boom)

    with pytest.raises(RuntimeError, match="disk full"):
        await idx.save()

    # Keys file was renamed in before the index step blew up; no .tmp lingers.
    keys_path = tmp_path / "sonic_similarity_clap_keys.json"
    assert keys_path.exists()
    assert "track1" in keys_path.read_text(encoding="utf-8")
    assert not (tmp_path / "sonic_similarity_clap_keys.json.tmp").exists()


@pytest.mark.asyncio
async def test_close_waits_for_a_save_whose_task_was_cancelled(
    tmp_path: Path,
    logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server shutdown cancels the save task; its worker keeps writing and must finish alone."""
    idx = ClapIndex(_make_mass(tmp_path), logger)  # type: ignore[arg-type]
    await idx.load()
    await idx.add("spotify", "track1", _unit_vec(1))

    real_save = idx._index.save
    in_save = threading.Event()
    release = threading.Event()
    writers_lock = threading.Lock()
    writers = 0

    def _slow_save(path: str) -> None:
        nonlocal writers
        with writers_lock:
            writers += 1
        in_save.set()
        # parked here rather than sleeping, so the assertion below can never
        # be decided by how fast the machine happens to be
        assert release.wait(10), "close() never let the save finish"
        real_save(path)

    monkeypatch.setattr(idx._index, "save", _slow_save)

    save_task = asyncio.create_task(idx.save())
    assert await asyncio.to_thread(in_save.wait, 10)
    save_task.cancel()
    with suppress(asyncio.CancelledError):
        await save_task

    # the unload that follows task cancellation on shutdown
    close_task = asyncio.create_task(idx.close())

    # an unguarded close would start writing here; the lock keeps it queued
    # behind the worker still parked above
    deadline = time.monotonic() + 0.25
    while time.monotonic() < deadline and writers < 2:
        await asyncio.sleep(0.01)
    assert writers == 1

    release.set()
    await close_task

    keys_path = tmp_path / "sonic_similarity_clap_keys.json"
    assert "track1" in keys_path.read_text(encoding="utf-8")
    assert idx._index is None


@pytest.mark.asyncio
async def test_add_landing_after_release_leaves_no_phantom_entry(
    tmp_path: Path, logger: logging.Logger
) -> None:
    """An insert that reaches the worker after teardown must not record a key without a vector."""
    idx = ClapIndex(_make_mass(tmp_path), logger)  # type: ignore[arg-type]
    await idx.load()
    await idx.close()

    # the worker an in-flight add() would have reached once the index was released
    applied = idx._add_sync(derive_label("spotify", "late"), _unit_vec(1), "spotify", "late")

    assert applied is False
    assert len(idx) == 0
    assert not idx.contains("spotify", "late")


@pytest.mark.asyncio
async def test_corrupt_index_with_valid_keys_drops_keys_to_avoid_phantom_contains(
    tmp_path: Path, logger: logging.Logger
) -> None:
    """A corrupt .usearch file alongside a valid keys file must clear both, not strand contains()."""
    keys_path = tmp_path / "sonic_similarity_clap_keys.json"
    index_path = tmp_path / "sonic_similarity_clap.usearch"
    # Plausible-shaped keys file referencing a label that no index backs.
    stale_label = derive_label("spotify", "phantom_track")
    keys_path.write_text(f'{{"{stale_label}": ["spotify", "phantom_track"]}}', encoding="utf-8")
    # Garbage bytes — usearch.load() will raise.
    index_path.write_bytes(b"not a usearch index")

    idx = ClapIndex(_make_mass(tmp_path), logger)  # type: ignore[arg-type]
    await idx.load()

    assert len(idx) == 0
    assert not idx.contains("spotify", "phantom_track")
    assert not keys_path.exists()
    assert not index_path.exists()


@pytest.mark.asyncio
async def test_missing_index_with_orphan_keys_drops_keys_to_avoid_phantom_contains(
    tmp_path: Path, logger: logging.Logger
) -> None:
    """A keys file present without its index is meaningless — must be cleaned up on load."""
    keys_path = tmp_path / "sonic_similarity_clap_keys.json"
    stale_label = derive_label("tidal", "orphan_track")
    keys_path.write_text(f'{{"{stale_label}": ["tidal", "orphan_track"]}}', encoding="utf-8")

    idx = ClapIndex(_make_mass(tmp_path), logger)  # type: ignore[arg-type]
    await idx.load()

    assert len(idx) == 0
    assert not idx.contains("tidal", "orphan_track")
    assert not keys_path.exists()
