"""Tests for the sonic_analysis plugin provider."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from music_assistant.constants import DB_TABLE_SONIC_SIGNATURES
from music_assistant.helpers.sonic_analysis import (
    SIGNATURE_DIMENSIONS,
    SIGNATURE_VERSION,
    SonicSignature,
)
from music_assistant.providers.sonic_analysis import VOYAGER_INDEX_FILENAME, SonicAnalysisProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_mass() -> MagicMock:
    """Return a MagicMock that looks enough like MusicAssistant for provider tests."""
    mass = MagicMock()
    mass.subscribe = MagicMock(return_value=lambda: None)
    mass.storage_path = "test_sonic_analysis"

    # music controller with an async database
    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.get_row = AsyncMock(return_value=None)
    db.insert_or_replace = AsyncMock(return_value=1)
    mass.music = MagicMock()
    mass.music.database = db

    return mass


def _make_provider(mass: MagicMock) -> SonicAnalysisProvider:
    """Instantiate SonicAnalysisProvider with minimal mocked dependencies."""
    manifest = MagicMock()
    manifest.name = "Sonic Analysis"
    manifest.domain = "sonic_analysis"
    config = MagicMock()
    # get_value must return a plain string so the logger level branch doesn't crash
    config.get_value = MagicMock(return_value="GLOBAL")

    return SonicAnalysisProvider(mass, manifest, config, set())


# ---------------------------------------------------------------------------
# Tests: handle_async_init
# ---------------------------------------------------------------------------


class TestHandleAsyncInit:
    """Tests for SonicAnalysisProvider.handle_async_init."""

    @pytest.mark.asyncio
    async def test_creates_sonic_signatures_table(self) -> None:
        """handle_async_init must issue a CREATE TABLE for sonic_signatures."""
        mass = _make_mock_mass()
        provider = _make_provider(mass)

        with patch("music_assistant.providers.sonic_analysis.voyager", create=True):
            await provider.handle_async_init()

        db = mass.music.database
        assert db.execute.called, "database.execute was never called"

        # Collect all SQL strings passed to execute()
        all_sql: list[str] = []
        for call in db.execute.call_args_list:
            args, _ = call
            if args:
                all_sql.append(str(args[0]))

        table_name = DB_TABLE_SONIC_SIGNATURES
        create_calls = [s for s in all_sql if "CREATE TABLE" in s and table_name in s]
        assert create_calls, (
            f"No CREATE TABLE statement found for '{table_name}'. SQL calls seen: {all_sql}"
        )

    @pytest.mark.asyncio
    async def test_initialises_corpus_stats_to_none(self) -> None:
        """After handle_async_init with no DB row, corpus stats must be None."""
        mass = _make_mock_mass()
        mass.music.database.get_row = AsyncMock(return_value=None)
        provider = _make_provider(mass)

        with patch("music_assistant.providers.sonic_analysis.voyager", create=True):
            await provider.handle_async_init()

        assert provider.corpus_means is None
        assert provider.corpus_stds is None


# ---------------------------------------------------------------------------
# Tests: get_sonic_signature / set_sonic_signature
# ---------------------------------------------------------------------------


class TestSignatureRoundTrip:
    """Tests for get/set sonic signature round-trip."""

    @pytest.mark.asyncio
    async def test_get_returns_none_when_no_row(self) -> None:
        """get_sonic_signature must return None when the DB has no matching row."""
        mass = _make_mock_mass()
        mass.music.database.get_row = AsyncMock(return_value=None)
        provider = _make_provider(mass)

        with patch("music_assistant.providers.sonic_analysis.voyager", create=True):
            await provider.handle_async_init()

        result = await provider.get_sonic_signature("track_1", "local")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_signature_from_db(self) -> None:
        """get_sonic_signature must deserialise a DB row into a SonicSignature."""
        mass = _make_mock_mass()
        features = [float(i) for i in range(38)]
        db_row: Mapping[str, Any] = {
            "item_id": "track_1",
            "provider": "local",
            "features": json.dumps(features),
            "version": SIGNATURE_VERSION,
        }
        mass.music.database.get_row = AsyncMock(return_value=db_row)
        provider = _make_provider(mass)

        with patch("music_assistant.providers.sonic_analysis.voyager", create=True):
            await provider.handle_async_init()

        result = await provider.get_sonic_signature("track_1", "local")
        assert isinstance(result, SonicSignature)
        assert result.features == features
        assert result.version == SIGNATURE_VERSION

    @pytest.mark.asyncio
    async def test_set_calls_insert_or_replace(self) -> None:
        """set_sonic_signature must persist the signature to the DB."""
        mass = _make_mock_mass()
        provider = _make_provider(mass)

        with patch("music_assistant.providers.sonic_analysis.voyager", create=True):
            await provider.handle_async_init()

        sig = SonicSignature(features=[1.0] * 38, version=SIGNATURE_VERSION)
        await provider.set_sonic_signature("track_1", "local", sig)

        db = mass.music.database
        assert db.insert_or_replace.called, "insert_or_replace was never called"
        call_args = db.insert_or_replace.call_args
        assert call_args is not None

        # First positional arg is the table name
        args, kwargs = call_args
        table = args[0] if args else kwargs.get("table")
        assert table == DB_TABLE_SONIC_SIGNATURES

    @pytest.mark.asyncio
    async def test_set_then_get_round_trips(self) -> None:
        """set_sonic_signature followed by get_sonic_signature must return equivalent data."""
        mass = _make_mock_mass()
        features = [float(i) * 0.1 for i in range(38)]
        sig = SonicSignature(features=features, version=SIGNATURE_VERSION)

        # Capture what was stored so get can return it
        stored: dict[str, Any] = {}

        async def fake_insert_or_replace(_table: str, values: dict[str, Any]) -> int:
            stored.update(values)
            return 1

        async def fake_get_row(_table: str, match: dict[str, Any]) -> Mapping[str, Any] | None:
            if stored and stored.get("item_id") == match.get("item_id"):
                return stored
            return None

        mass.music.database.insert_or_replace = AsyncMock(side_effect=fake_insert_or_replace)
        mass.music.database.get_row = AsyncMock(side_effect=fake_get_row)

        provider = _make_provider(mass)
        with patch("music_assistant.providers.sonic_analysis.voyager", create=True):
            await provider.handle_async_init()

        await provider.set_sonic_signature("track_1", "local", sig)
        result = await provider.get_sonic_signature("track_1", "local")

        assert result is not None
        assert result.features == features
        assert result.version == SIGNATURE_VERSION


# ---------------------------------------------------------------------------
# Tests: unload
# ---------------------------------------------------------------------------


class TestUnload:
    """Tests for SonicAnalysisProvider.unload."""

    @pytest.mark.asyncio
    async def test_unload_calls_unsubscribes(self) -> None:
        """Unload must call every registered unsubscribe callback."""
        mass = _make_mock_mass()
        provider = _make_provider(mass)

        with patch("music_assistant.providers.sonic_analysis.voyager", create=True):
            await provider.handle_async_init()

        # Manually register a mock unsubscribe callback
        unsubscribe_mock = MagicMock()
        provider._on_unload.append(unsubscribe_mock)

        await provider.unload()

        unsubscribe_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: Voyager index
# ---------------------------------------------------------------------------


class TestVoyagerIndex:
    """Tests for Voyager ANN index methods."""

    def _make_provider_with_storage(self, tmp_path: Any) -> tuple[SonicAnalysisProvider, MagicMock]:
        """Create a provider whose storage_path points to tmp_path."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        provider = _make_provider(mass)
        return provider, mass

    def test_init_creates_fresh_index_when_no_file(self, tmp_path: Any) -> None:
        """_init_voyager_index must create a new empty index when no file exists."""
        provider, _ = self._make_provider_with_storage(tmp_path)
        provider._init_voyager_index()

        assert provider._voyager_index is not None
        assert provider._voyager_index.num_elements == 0

    def test_init_loads_existing_index_from_disk(self, tmp_path: Any) -> None:
        """_init_voyager_index must load an existing index file if present."""
        provider, _ = self._make_provider_with_storage(tmp_path)

        # Build and save an index with a known item
        provider._init_voyager_index()
        rng = np.random.default_rng(seed=0)
        vec = rng.random((1, SIGNATURE_DIMENSIONS), dtype=np.float32)
        provider._add_to_index(99, vec[0].tolist())
        provider._save_voyager_index()

        # Create a new provider instance pointing to the same path
        provider2, _ = self._make_provider_with_storage(tmp_path)
        provider2._init_voyager_index()

        assert provider2._voyager_index.num_elements == 1

    def test_add_to_index_increases_element_count(self, tmp_path: Any) -> None:
        """_add_to_index must add a vector to the index."""
        provider, _ = self._make_provider_with_storage(tmp_path)
        provider._init_voyager_index()

        vec = [float(i) / SIGNATURE_DIMENSIONS for i in range(SIGNATURE_DIMENSIONS)]
        provider._add_to_index(1, vec)

        assert provider._voyager_index.num_elements == 1

    def test_query_returns_empty_list_for_empty_index(self, tmp_path: Any) -> None:
        """_query_index must return [] when the index has no elements."""
        provider, _ = self._make_provider_with_storage(tmp_path)
        provider._init_voyager_index()

        query_vec = [0.5] * SIGNATURE_DIMENSIONS
        results = provider._query_index(query_vec)

        assert results == []

    def test_query_returns_id_distance_pairs(self, tmp_path: Any) -> None:
        """_query_index must return list of (item_id, distance) tuples."""
        provider, _ = self._make_provider_with_storage(tmp_path)
        provider._init_voyager_index()

        vec = [float(i) / SIGNATURE_DIMENSIONS for i in range(SIGNATURE_DIMENSIONS)]
        provider._add_to_index(42, vec)

        results = provider._query_index(vec, k=1)

        assert len(results) == 1
        item_id, distance = results[0]
        assert item_id == 42
        assert isinstance(distance, float)

    def test_query_k_clamped_to_num_elements(self, tmp_path: Any) -> None:
        """_query_index must not fail when k > num_elements."""
        provider, _ = self._make_provider_with_storage(tmp_path)
        provider._init_voyager_index()

        vec = [float(i) / SIGNATURE_DIMENSIONS for i in range(SIGNATURE_DIMENSIONS)]
        provider._add_to_index(1, vec)

        # k=25 but only 1 element — should return 1 result without error
        results = provider._query_index(vec, k=25)

        assert len(results) == 1

    def test_similar_vector_ranks_higher_than_dissimilar(self, tmp_path: Any) -> None:
        """A nearly-identical vector must have a lower distance than a very different one."""
        provider, _ = self._make_provider_with_storage(tmp_path)
        provider._init_voyager_index()

        # Reference vector
        base = np.zeros(SIGNATURE_DIMENSIONS, dtype=np.float32)
        base[0] = 1.0

        # Nearly identical: tiny perturbation
        similar = base.copy()
        similar[1] = 0.001

        # Very different: orthogonal direction
        different = np.zeros(SIGNATURE_DIMENSIONS, dtype=np.float32)
        different[-1] = 1.0

        provider._add_to_index(1, base.tolist())
        provider._add_to_index(2, similar.tolist())
        provider._add_to_index(3, different.tolist())

        results = provider._query_index(base.tolist(), k=3)
        assert len(results) >= 2

        ids_by_rank = [r[0] for r in results]
        # similar (id=2) should rank before different (id=3)
        assert ids_by_rank.index(2) < ids_by_rank.index(3)

    @pytest.mark.asyncio
    async def test_save_voyager_index_writes_file(self, tmp_path: Any) -> None:
        """_save_voyager_index must write the index file to storage_path."""
        provider, _ = self._make_provider_with_storage(tmp_path)
        provider._init_voyager_index()

        vec = [0.1] * SIGNATURE_DIMENSIONS
        provider._add_to_index(7, vec)
        provider._save_voyager_index()

        index_path = tmp_path / VOYAGER_INDEX_FILENAME
        assert index_path.exists()
        assert index_path.stat().st_size > 0

    @pytest.mark.asyncio
    async def test_unload_saves_voyager_index(self, tmp_path: Any) -> None:
        """unload() must persist the Voyager index to disk."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        provider = _make_provider(mass)
        await provider.handle_async_init()

        vec = [0.2] * SIGNATURE_DIMENSIONS
        provider._add_to_index(5, vec)

        await provider.unload()

        index_path = tmp_path / VOYAGER_INDEX_FILENAME
        assert index_path.exists()

    @pytest.mark.asyncio
    async def test_handle_async_init_initialises_voyager_index(self, tmp_path: Any) -> None:
        """handle_async_init must initialise the Voyager index."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        provider = _make_provider(mass)

        await provider.handle_async_init()

        assert hasattr(provider, "_voyager_index")
        assert provider._voyager_index is not None
