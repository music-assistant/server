"""Smoke tests for the optional 1024-dim CLAP usearch index helper.

Round-trip coverage: deterministic labels, add/contains/get, and
persistence to the sonic_similarity_clap.usearch filename stem under
the configured storage_path.
"""

from __future__ import annotations

import logging
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
