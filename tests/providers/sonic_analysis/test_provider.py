"""Tests for the sonic_analysis plugin provider."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.enums import EventType

from music_assistant.constants import DB_TABLE_SONIC_SIGNATURES
from music_assistant.helpers.sonic_analysis import (
    SIGNATURE_DIMENSIONS,
    SIGNATURE_VERSION,
    SonicSignature,
)
from music_assistant.providers.sonic_analysis import (
    CONF_ANALYZE_ON_PLAY,
    CONF_ANALYZE_ON_SYNC,
    CONF_MAX_CONCURRENT_ANALYSES,
    USEARCH_INDEX_FILENAME,
    SonicAnalysisProvider,
)

try:
    from usearch.index import Index as _USearchIndex
except ImportError:
    _USearchIndex = None  # type: ignore[assignment, misc]

_usearch_available = _USearchIndex is not None
_requires_usearch = pytest.mark.skipif(
    not _usearch_available, reason="usearch package not installed"
)

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

    def _get_value(key: str) -> Any:
        if key == CONF_MAX_CONCURRENT_ANALYSES:
            return 2
        # default string return keeps other branches (e.g. logger level) working
        return "GLOBAL"

    config.get_value = MagicMock(side_effect=_get_value)

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

        with patch("music_assistant.providers.sonic_analysis.USearchIndex", create=True):
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

        with patch("music_assistant.providers.sonic_analysis.USearchIndex", create=True):
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

        with patch("music_assistant.providers.sonic_analysis.USearchIndex", create=True):
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

        with patch("music_assistant.providers.sonic_analysis.USearchIndex", create=True):
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

        with patch("music_assistant.providers.sonic_analysis.USearchIndex", create=True):
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
        with patch("music_assistant.providers.sonic_analysis.USearchIndex", create=True):
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

        with patch("music_assistant.providers.sonic_analysis.USearchIndex", create=True):
            await provider.handle_async_init()

        # Manually register a mock unsubscribe callback
        unsubscribe_mock = MagicMock()
        provider._on_unload.append(unsubscribe_mock)

        await provider.unload()

        unsubscribe_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: USearch index
# ---------------------------------------------------------------------------


@_requires_usearch
class TestUSearchIndex:
    """Tests for USearch ANN index methods."""

    def _make_provider_with_storage(self, tmp_path: Any) -> tuple[SonicAnalysisProvider, MagicMock]:
        """Create a provider whose storage_path points to tmp_path."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        provider = _make_provider(mass)
        return provider, mass

    def test_init_creates_fresh_index_when_no_file(self, tmp_path: Any) -> None:
        """_init_search_index must create a new empty index when no file exists."""
        provider, _ = self._make_provider_with_storage(tmp_path)
        provider._init_search_index()

        assert provider._search_index is not None
        assert len(provider._search_index) == 0

    def test_init_loads_existing_index_from_disk(self, tmp_path: Any) -> None:
        """_init_search_index must load an existing index file if present."""
        provider, _ = self._make_provider_with_storage(tmp_path)

        # Build and save an index with a known item
        provider._init_search_index()
        rng = np.random.default_rng(seed=0)
        vec = rng.random(SIGNATURE_DIMENSIONS, dtype=np.float32)
        provider._add_to_index(99, vec.tolist())
        provider._save_search_index()

        # Create a new provider instance pointing to the same path
        provider2, _ = self._make_provider_with_storage(tmp_path)
        provider2._init_search_index()

        assert len(provider2._search_index) == 1

    def test_add_to_index_increases_element_count(self, tmp_path: Any) -> None:
        """_add_to_index must add a vector to the index."""
        provider, _ = self._make_provider_with_storage(tmp_path)
        provider._init_search_index()

        vec = [float(i) / SIGNATURE_DIMENSIONS for i in range(SIGNATURE_DIMENSIONS)]
        provider._add_to_index(1, vec)

        assert len(provider._search_index) == 1

    def test_query_returns_empty_list_for_empty_index(self, tmp_path: Any) -> None:
        """_query_index must return [] when the index has no elements."""
        provider, _ = self._make_provider_with_storage(tmp_path)
        provider._init_search_index()

        query_vec = [0.5] * SIGNATURE_DIMENSIONS
        results = provider._query_index(query_vec)

        assert results == []

    def test_query_returns_id_distance_pairs(self, tmp_path: Any) -> None:
        """_query_index must return list of (item_id, distance) tuples."""
        provider, _ = self._make_provider_with_storage(tmp_path)
        provider._init_search_index()

        vec = [float(i) / SIGNATURE_DIMENSIONS for i in range(SIGNATURE_DIMENSIONS)]
        provider._add_to_index(42, vec)

        results = provider._query_index(vec, k=1)

        assert len(results) == 1
        item_id, distance = results[0]
        assert item_id == 42
        assert isinstance(distance, float)

    def test_query_k_clamped_to_num_elements(self, tmp_path: Any) -> None:
        """_query_index must not fail when k > len(index)."""
        provider, _ = self._make_provider_with_storage(tmp_path)
        provider._init_search_index()

        vec = [float(i) / SIGNATURE_DIMENSIONS for i in range(SIGNATURE_DIMENSIONS)]
        provider._add_to_index(1, vec)

        # k=25 but only 1 element — should return 1 result without error
        results = provider._query_index(vec, k=25)

        assert len(results) == 1

    def test_similar_vector_ranks_higher_than_dissimilar(self, tmp_path: Any) -> None:
        """A nearly-identical vector must have a lower distance than a very different one."""
        provider, _ = self._make_provider_with_storage(tmp_path)
        provider._init_search_index()

        # Use dense random vectors so that I8 quantization preserves meaningful differences.
        rng = np.random.default_rng(42)

        # Reference vector: dense random
        base = rng.standard_normal(SIGNATURE_DIMENSIONS).astype(np.float32)

        # Nearly identical: base plus very small noise
        similar = base + rng.standard_normal(SIGNATURE_DIMENSIONS).astype(np.float32) * 0.01

        # Very different: negated base plus noise — cosine distance ≈ 2
        different = -base + rng.standard_normal(SIGNATURE_DIMENSIONS).astype(np.float32) * 0.1

        provider._add_to_index(1, base.tolist())
        provider._add_to_index(2, similar.tolist())
        provider._add_to_index(3, different.tolist())

        results = provider._query_index(base.tolist(), k=3)
        assert len(results) >= 2

        ids_by_rank = [r[0] for r in results]
        # similar (id=2) should rank before different (id=3)
        assert ids_by_rank.index(2) < ids_by_rank.index(3)

    @pytest.mark.asyncio
    async def test_save_search_index_writes_file(self, tmp_path: Any) -> None:
        """_save_search_index must write the index file to storage_path."""
        provider, _ = self._make_provider_with_storage(tmp_path)
        provider._init_search_index()

        vec = [0.1] * SIGNATURE_DIMENSIONS
        provider._add_to_index(7, vec)
        provider._save_search_index()

        index_path = tmp_path / USEARCH_INDEX_FILENAME
        assert index_path.exists()
        assert index_path.stat().st_size > 0

    @pytest.mark.asyncio
    async def test_unload_saves_search_index(self, tmp_path: Any) -> None:
        """unload() must persist the USearch index to disk."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        provider = _make_provider(mass)
        await provider.handle_async_init()

        vec = [0.2] * SIGNATURE_DIMENSIONS
        provider._add_to_index(5, vec)

        await provider.unload()

        index_path = tmp_path / USEARCH_INDEX_FILENAME
        assert index_path.exists()

    @pytest.mark.asyncio
    async def test_handle_async_init_initialises_search_index(self, tmp_path: Any) -> None:
        """handle_async_init must initialise the USearch index."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        provider = _make_provider(mass)

        await provider.handle_async_init()

        assert hasattr(provider, "_search_index")
        assert provider._search_index is not None


# ---------------------------------------------------------------------------
# Tests: loaded_in_mass — event subscriptions (Task 7)
# ---------------------------------------------------------------------------


def _make_provider_with_config(
    mass: MagicMock, **config_overrides: bool | int
) -> SonicAnalysisProvider:
    """Instantiate SonicAnalysisProvider with configurable boolean config values."""
    manifest = MagicMock()
    manifest.name = "Sonic Analysis"
    manifest.domain = "sonic_analysis"
    config = MagicMock()

    defaults: dict[str, Any] = {
        CONF_ANALYZE_ON_PLAY: True,
        CONF_ANALYZE_ON_SYNC: True,
        "max_concurrent_analyses": 2,
    }
    defaults.update(config_overrides)
    config.get_value = MagicMock(side_effect=lambda key: defaults.get(key, "GLOBAL"))

    return SonicAnalysisProvider(mass, manifest, config, set())


class TestLoadedInMass:
    """Tests for SonicAnalysisProvider.loaded_in_mass event subscriptions."""

    @pytest.mark.asyncio
    async def test_subscribes_to_media_item_played_when_analyze_on_play_enabled(
        self, tmp_path: Any
    ) -> None:
        """loaded_in_mass must subscribe to MEDIA_ITEM_PLAYED when analyze_on_play is True."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        provider = _make_provider_with_config(
            mass, **{CONF_ANALYZE_ON_PLAY: True, CONF_ANALYZE_ON_SYNC: False}
        )

        await provider.handle_async_init()
        await provider.loaded_in_mass()

        subscribed_event_types = [
            c.args[1] for c in mass.subscribe.call_args_list if len(c.args) >= 2
        ]
        assert EventType.MEDIA_ITEM_PLAYED in subscribed_event_types

    @pytest.mark.asyncio
    async def test_subscribes_to_media_item_added_when_analyze_on_sync_enabled(
        self, tmp_path: Any
    ) -> None:
        """loaded_in_mass must subscribe to MEDIA_ITEM_ADDED when analyze_on_sync is True."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        provider = _make_provider_with_config(
            mass, **{CONF_ANALYZE_ON_PLAY: False, CONF_ANALYZE_ON_SYNC: True}
        )

        await provider.handle_async_init()
        await provider.loaded_in_mass()

        subscribed_event_types = [
            c.args[1] for c in mass.subscribe.call_args_list if len(c.args) >= 2
        ]
        assert EventType.MEDIA_ITEM_ADDED in subscribed_event_types

    @pytest.mark.asyncio
    async def test_does_not_subscribe_to_played_when_analyze_on_play_disabled(
        self, tmp_path: Any
    ) -> None:
        """loaded_in_mass must not subscribe to MEDIA_ITEM_PLAYED when analyze_on_play is False."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        provider = _make_provider_with_config(
            mass, **{CONF_ANALYZE_ON_PLAY: False, CONF_ANALYZE_ON_SYNC: False}
        )

        await provider.handle_async_init()
        await provider.loaded_in_mass()

        subscribed_event_types = [
            c.args[1] for c in mass.subscribe.call_args_list if len(c.args) >= 2
        ]
        assert EventType.MEDIA_ITEM_PLAYED not in subscribed_event_types

    @pytest.mark.asyncio
    async def test_does_not_subscribe_to_added_when_analyze_on_sync_disabled(
        self, tmp_path: Any
    ) -> None:
        """loaded_in_mass must not subscribe to MEDIA_ITEM_ADDED when analyze_on_sync is False."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        provider = _make_provider_with_config(
            mass, **{CONF_ANALYZE_ON_PLAY: False, CONF_ANALYZE_ON_SYNC: False}
        )

        await provider.handle_async_init()
        await provider.loaded_in_mass()

        subscribed_event_types = [
            c.args[1] for c in mass.subscribe.call_args_list if len(c.args) >= 2
        ]
        assert EventType.MEDIA_ITEM_ADDED not in subscribed_event_types

    @pytest.mark.asyncio
    async def test_unsubscribe_callables_stored_in_on_unload(self, tmp_path: Any) -> None:
        """Unsubscribe callables returned by mass.subscribe must be stored in _on_unload."""
        unsub_played = MagicMock()
        unsub_added = MagicMock()

        call_index = 0

        def subscribe_side_effect(*_args: Any, **_kwargs: Any) -> MagicMock:
            nonlocal call_index
            result = unsub_played if call_index == 0 else unsub_added
            call_index += 1
            return result

        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        mass.subscribe = MagicMock(side_effect=subscribe_side_effect)
        provider = _make_provider_with_config(
            mass, **{CONF_ANALYZE_ON_PLAY: True, CONF_ANALYZE_ON_SYNC: True}
        )

        await provider.handle_async_init()
        await provider.loaded_in_mass()

        assert unsub_played in provider._on_unload
        assert unsub_added in provider._on_unload

    @pytest.mark.asyncio
    async def test_both_subscriptions_registered_when_both_enabled(self, tmp_path: Any) -> None:
        """loaded_in_mass must register both subscriptions when both config flags are True."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        provider = _make_provider_with_config(
            mass, **{CONF_ANALYZE_ON_PLAY: True, CONF_ANALYZE_ON_SYNC: True}
        )

        await provider.handle_async_init()
        await provider.loaded_in_mass()

        assert mass.subscribe.call_count == 2


# ---------------------------------------------------------------------------
# Tests: _analyze_track (Task 8)
# ---------------------------------------------------------------------------


class TestAnalyzeTrack:
    """Tests for SonicAnalysisProvider._analyze_track."""

    def _make_sine_audio(self, duration: float = 3.0, sample_rate: int = 22050) -> np.ndarray:
        """Generate a synthetic sine wave for testing."""
        t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
        return (np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)

    @pytest.mark.asyncio
    async def test_analyze_track_stores_signature_in_db(self, tmp_path: Any) -> None:
        """_analyze_track must persist the extracted signature via set_sonic_signature."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        provider = _make_provider_with_config(mass)
        await provider.handle_async_init()

        audio = self._make_sine_audio()
        sig = await provider._analyze_track("track_1", "local", audio, 22050)

        assert sig is not None
        assert len(sig.features) == SIGNATURE_DIMENSIONS

        # Verify insert_or_replace was called with the expected item_id
        db = mass.music.database
        assert db.insert_or_replace.called
        stored_calls = [
            c
            for c in db.insert_or_replace.call_args_list
            if c.args and c.args[0] == DB_TABLE_SONIC_SIGNATURES
        ]
        assert stored_calls, "No insert_or_replace call found for sonic_signatures table"
        stored_values = stored_calls[-1].args[1]
        assert stored_values["item_id"] == "track_1"
        assert stored_values["provider"] == "local"
        stored_features = json.loads(stored_values["features"])
        assert len(stored_features) == SIGNATURE_DIMENSIONS

    @pytest.mark.asyncio
    @_requires_usearch
    async def test_analyze_track_adds_to_search_index_when_corpus_stats_set(
        self, tmp_path: Any
    ) -> None:
        """_analyze_track must add a normalised vector to the index when corpus stats are set."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        provider = _make_provider_with_config(mass)
        await provider.handle_async_init()

        # Seed corpus stats so normalisation is possible
        provider.corpus_means = [0.0] * SIGNATURE_DIMENSIONS
        provider.corpus_stds = [1.0] * SIGNATURE_DIMENSIONS

        before = len(provider._search_index)
        audio = self._make_sine_audio()
        await provider._analyze_track("track_42", "local", audio, 22050)

        assert len(provider._search_index) == before + 1

    @pytest.mark.asyncio
    @_requires_usearch
    async def test_analyze_track_does_not_add_to_index_without_corpus_stats(
        self, tmp_path: Any
    ) -> None:
        """_analyze_track must skip the index when corpus stats are absent."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        provider = _make_provider_with_config(mass)
        await provider.handle_async_init()

        # Corpus stats explicitly None (default after init with empty DB)
        assert provider.corpus_means is None

        before = len(provider._search_index)
        audio = self._make_sine_audio()
        await provider._analyze_track("track_99", "local", audio, 22050)

        assert len(provider._search_index) == before

    @pytest.mark.asyncio
    async def test_analyze_track_respects_semaphore(self, tmp_path: Any) -> None:
        """_analyze_track must use _analysis_semaphore to limit concurrency."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        provider = _make_provider_with_config(mass, max_concurrent_analyses=1)
        await provider.handle_async_init()

        assert hasattr(provider, "_analysis_semaphore")
        assert isinstance(provider._analysis_semaphore, asyncio.Semaphore)


# ---------------------------------------------------------------------------
# Tests: _handle_similar_tracks API endpoint (Task 9)
# ---------------------------------------------------------------------------

_USEARCH_PATCH = "music_assistant.providers.sonic_analysis.USearchIndex"


class TestHandleSimilarTracks:
    """Tests for SonicAnalysisProvider._handle_similar_tracks."""

    @pytest.mark.asyncio
    async def test_missing_item_id_returns_400(self, tmp_path: Any) -> None:
        """_handle_similar_tracks must return HTTP 400 when item_id is missing."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        provider = _make_provider(mass)

        with patch(_USEARCH_PATCH, create=True):
            await provider.handle_async_init()

        request = make_mocked_request("GET", "/api/sonic_analysis/similar")

        response = await provider._handle_similar_tracks(request)
        assert response.status == 400

    @pytest.mark.asyncio
    async def test_no_signature_returns_analyzed_false(self, tmp_path: Any) -> None:
        """_handle_similar_tracks must return analyzed=false when no signature exists."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        # get_rows returns empty list — no signature stored
        mass.music.database.get_rows = AsyncMock(return_value=[])
        provider = _make_provider(mass)

        with patch(_USEARCH_PATCH, create=True):
            await provider.handle_async_init()

        request = make_mocked_request("GET", "/api/sonic_analysis/similar", match_info={})
        request._rel_url = request._rel_url.with_query({"item_id": "track_1"})

        response = await provider._handle_similar_tracks(request)
        assert response.status == 200
        body = json.loads(response.body)
        assert body["analyzed"] is False
        assert body["items"] == []
        assert body["seed_track_id"] == "track_1"

    @pytest.mark.asyncio
    async def test_limit_defaults_to_25(self, tmp_path: Any) -> None:
        """_handle_similar_tracks must use limit=25 by default."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        mass.music.database.get_rows = AsyncMock(return_value=[])
        provider = _make_provider(mass)

        with patch(_USEARCH_PATCH, create=True):
            await provider.handle_async_init()

        request = make_mocked_request("GET", "/api/sonic_analysis/similar", match_info={})
        request._rel_url = request._rel_url.with_query({"item_id": "track_1"})

        response = await provider._handle_similar_tracks(request)
        assert response.status == 200

    @pytest.mark.asyncio
    async def test_limit_capped_at_100(self, tmp_path: Any) -> None:
        """_handle_similar_tracks must cap limit at 100."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        mass.music.database.get_rows = AsyncMock(return_value=[])
        provider = _make_provider(mass)

        with patch(_USEARCH_PATCH, create=True):
            await provider.handle_async_init()

        request = make_mocked_request("GET", "/api/sonic_analysis/similar", match_info={})
        request._rel_url = request._rel_url.with_query({"item_id": "track_1", "limit": "999"})

        response = await provider._handle_similar_tracks(request)
        assert response.status == 200

    @pytest.mark.asyncio
    async def test_with_signature_returns_analyzed_true(self, tmp_path: Any) -> None:
        """_handle_similar_tracks must return analyzed=true when signature is found."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)

        features = [float(i) * 0.01 for i in range(38)]
        db_row: dict[str, Any] = {
            "item_id": "track_1",
            "provider": "local",
            "features": json.dumps(features),
            "version": SIGNATURE_VERSION,
        }
        mass.music.database.get_rows = AsyncMock(return_value=[db_row])
        mass.music.tracks = MagicMock()
        mass.music.tracks.get = AsyncMock(return_value=None)

        provider = _make_provider(mass)
        with patch(_USEARCH_PATCH, create=True):
            await provider.handle_async_init()

        provider.corpus_means = [0.0] * SIGNATURE_DIMENSIONS
        provider.corpus_stds = [1.0] * SIGNATURE_DIMENSIONS

        # Directly set a mock index that returns empty results (no neighbours)
        mock_index = MagicMock()
        mock_index.__len__ = MagicMock(return_value=0)
        provider._search_index = mock_index

        request = make_mocked_request("GET", "/api/sonic_analysis/similar", match_info={})
        request._rel_url = request._rel_url.with_query({"item_id": "track_1", "limit": "5"})

        response = await provider._handle_similar_tracks(request)
        assert response.status == 200
        body = json.loads(response.body)
        assert body["analyzed"] is True
        assert body["seed_track_id"] == "track_1"
        assert isinstance(body["items"], list)

    @pytest.mark.asyncio
    async def test_route_registered_in_loaded_in_mass(self, tmp_path: Any) -> None:
        """loaded_in_mass must register the /api/sonic_analysis/similar GET route."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        mass.webserver = MagicMock()
        mass.webserver.register_dynamic_route = MagicMock(return_value=lambda: None)
        provider = _make_provider(mass)

        with patch(_USEARCH_PATCH, create=True):
            await provider.handle_async_init()
        await provider.loaded_in_mass()

        registered_paths = [
            call.args[0]
            for call in mass.webserver.register_dynamic_route.call_args_list
            if call.args
        ]
        assert "/api/sonic_analysis/similar" in registered_paths


# ---------------------------------------------------------------------------
# Tests: _rebuild_search_index (Task 10)
# ---------------------------------------------------------------------------


def _make_mock_usearch_index() -> MagicMock:
    """Return a MagicMock that behaves like a minimal USearch Index.

    Tracks added items so len() reflects actual calls to add().
    """
    index = MagicMock()
    _items: list[Any] = []

    def _add(key: int, _vector: Any) -> None:
        _items.append(key)

    index.add.side_effect = _add
    index.__len__ = MagicMock(side_effect=lambda: len(_items))
    return index


def _make_mock_usearch_class(index: MagicMock) -> MagicMock:
    """Return a MagicMock for USearchIndex that yields `index` from Index(...)."""
    mock_cls = MagicMock()
    mock_cls.return_value = index
    mock_cls.restore = MagicMock(return_value=index)
    return mock_cls


class TestRebuildSearchIndex:
    """Tests for SonicAnalysisProvider._rebuild_search_index."""

    @pytest.mark.asyncio
    async def test_rebuild_with_two_signatures(self, tmp_path: Any) -> None:
        """_rebuild_search_index must populate the index with all stored signatures."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)

        features_a = [0.1] * SIGNATURE_DIMENSIONS
        features_b = [0.9] * SIGNATURE_DIMENSIONS

        rows = [
            {
                "item_id": "track_a",
                "provider": "local",
                "features": json.dumps(features_a),
                "version": SIGNATURE_VERSION,
            },
            {
                "item_id": "track_b",
                "provider": "local",
                "features": json.dumps(features_b),
                "version": SIGNATURE_VERSION,
            },
        ]
        mass.music.database.get_rows = AsyncMock(return_value=rows)

        mock_index = _make_mock_usearch_index()
        mock_cls = _make_mock_usearch_class(mock_index)

        provider = _make_provider(mass)
        with patch(_USEARCH_PATCH, mock_cls):
            await provider.handle_async_init()
            await provider._rebuild_search_index()

        assert len(provider._search_index) == 2

    @pytest.mark.asyncio
    async def test_rebuild_saves_corpus_stats(self, tmp_path: Any) -> None:
        """_rebuild_search_index must save new corpus stats to the DB."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)

        features_a = [0.2] * SIGNATURE_DIMENSIONS
        features_b = [0.8] * SIGNATURE_DIMENSIONS

        rows = [
            {
                "item_id": "track_a",
                "provider": "local",
                "features": json.dumps(features_a),
                "version": SIGNATURE_VERSION,
            },
            {
                "item_id": "track_b",
                "provider": "local",
                "features": json.dumps(features_b),
                "version": SIGNATURE_VERSION,
            },
        ]
        mass.music.database.get_rows = AsyncMock(return_value=rows)

        mock_cls = _make_mock_usearch_class(_make_mock_usearch_index())

        provider = _make_provider(mass)
        with patch(_USEARCH_PATCH, mock_cls):
            await provider.handle_async_init()
            await provider._rebuild_search_index()

        corpus_stats_calls = [
            call
            for call in mass.music.database.insert_or_replace.call_args_list
            if call.args and call.args[1].get("item_id") == "__corpus_stats__"
        ]
        assert corpus_stats_calls, "insert_or_replace not called with __corpus_stats__ sentinel"

    @pytest.mark.asyncio
    async def test_rebuild_skips_corpus_stats_row(self, tmp_path: Any) -> None:
        """_rebuild_search_index must not include the __corpus_stats__ sentinel in the index."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)

        features_a = [0.5] * SIGNATURE_DIMENSIONS

        rows = [
            {
                "item_id": "__corpus_stats__",
                "provider": "__corpus_stats__",
                "features": json.dumps({"means": [0.0] * 38, "stds": [1.0] * 38}),
                "version": SIGNATURE_VERSION,
            },
            {
                "item_id": "track_a",
                "provider": "local",
                "features": json.dumps(features_a),
                "version": SIGNATURE_VERSION,
            },
        ]
        mass.music.database.get_rows = AsyncMock(return_value=rows)

        mock_index = _make_mock_usearch_index()
        mock_cls = _make_mock_usearch_class(mock_index)

        provider = _make_provider(mass)
        with patch(_USEARCH_PATCH, mock_cls):
            await provider.handle_async_init()
            await provider._rebuild_search_index()

        assert len(provider._search_index) == 1

    @pytest.mark.asyncio
    async def test_rebuild_with_no_signatures_logs_and_returns(self, tmp_path: Any) -> None:
        """_rebuild_search_index must return early when there are no track signatures."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        mass.music.database.get_rows = AsyncMock(return_value=[])

        mock_cls = _make_mock_usearch_class(_make_mock_usearch_index())

        provider = _make_provider(mass)
        with patch(_USEARCH_PATCH, mock_cls):
            await provider.handle_async_init()
            await provider._rebuild_search_index()

        # Index must remain empty — rebuild returned early
        assert len(provider._search_index) == 0


# ---------------------------------------------------------------------------
# Tests: background backfill (Task 11)
# ---------------------------------------------------------------------------


def _make_mock_track(
    item_id: str, provider_instance: str, provider_item_id: str = "",
) -> MagicMock:
    """Return a minimal mock Track with item_id and provider_mappings."""
    track = MagicMock()
    track.item_id = item_id
    mapping = MagicMock()
    mapping.provider_instance = provider_instance
    mapping.item_id = provider_item_id or item_id
    track.provider_mappings = [mapping]
    return track


class TestBackfill:
    """Tests for SonicAnalysisProvider._backfill_unanalyzed_tracks and loaded_in_mass scheduling."""

    @pytest.mark.asyncio
    async def test_loaded_in_mass_schedules_backfill_when_analyze_on_sync_enabled(
        self, tmp_path: Any
    ) -> None:
        """loaded_in_mass must schedule backfill via mass.create_task.

        Applies when analyze_on_sync is True.
        """
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        mass.create_task = MagicMock()

        provider = _make_provider_with_config(
            mass, **{CONF_ANALYZE_ON_PLAY: False, CONF_ANALYZE_ON_SYNC: True}
        )

        with patch(_USEARCH_PATCH, create=True):
            await provider.handle_async_init()

        with patch.object(
            provider, "_backfill_unanalyzed_tracks", return_value=None
        ) as mock_backfill:
            await provider.loaded_in_mass()

        mass.tasks.create_task.assert_called_once()
        call_kwargs = mass.tasks.create_task.call_args[1]
        assert call_kwargs["task_id"] == "sonic_analysis_backfill"
        assert callable(call_kwargs["handler"])

    @pytest.mark.asyncio
    async def test_loaded_in_mass_does_not_schedule_backfill_when_analyze_on_sync_disabled(
        self, tmp_path: Any
    ) -> None:
        """loaded_in_mass must not call mass.create_task for backfill.

        Applies when analyze_on_sync is False.
        """
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)
        mass.create_task = MagicMock()

        provider = _make_provider_with_config(
            mass, **{CONF_ANALYZE_ON_PLAY: False, CONF_ANALYZE_ON_SYNC: False}
        )

        with patch(_USEARCH_PATCH, create=True):
            await provider.handle_async_init()

        await provider.loaded_in_mass()

        mass.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_backfill_skips_tracks_with_existing_signatures(self, tmp_path: Any) -> None:
        """_backfill_unanalyzed_tracks must not analyze tracks that already have a signature."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)

        track = _make_mock_track("track_1", "local")
        mass.music.tracks = MagicMock()
        mass.music.tracks.library_items = AsyncMock(return_value=[track])

        existing_sig = MagicMock()

        provider = _make_provider_with_config(mass)
        with patch(_USEARCH_PATCH, create=True):
            await provider.handle_async_init()

        get_sig = patch.object(
            provider, "get_sonic_signature", AsyncMock(return_value=existing_sig)
        )
        fetch = patch.object(provider, "_fetch_and_analyze", AsyncMock())
        with get_sig, fetch as mock_fetch:
            await provider._backfill_unanalyzed_tracks()

        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_backfill_analyzes_tracks_without_signatures(self, tmp_path: Any) -> None:
        """_backfill_unanalyzed_tracks must call _fetch_and_analyze for tracks with no signature."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)

        track = _make_mock_track("track_1", "local")
        mass.music.tracks = MagicMock()
        mass.music.tracks.library_items = AsyncMock(return_value=[track])

        provider = _make_provider_with_config(mass)
        with patch(_USEARCH_PATCH, create=True):
            await provider.handle_async_init()

        get_sig = patch.object(provider, "get_sonic_signature", AsyncMock(return_value=None))
        rebuild = patch.object(provider, "_rebuild_search_index", AsyncMock())
        with (
            get_sig,
            patch.object(provider, "_fetch_and_analyze", AsyncMock()) as mock_fetch,
            rebuild,
        ):
            await provider._backfill_unanalyzed_tracks()

        mock_fetch.assert_called_once_with("track_1", "local", "track_1")

    @pytest.mark.asyncio
    async def test_backfill_handles_library_fetch_error_gracefully(self, tmp_path: Any) -> None:
        """_backfill_unanalyzed_tracks must return early and not raise if library_items fails."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)

        mass.music.tracks = MagicMock()
        mass.music.tracks.library_items = AsyncMock(side_effect=RuntimeError("DB error"))

        provider = _make_provider_with_config(mass)
        with patch(_USEARCH_PATCH, create=True):
            await provider.handle_async_init()

        # Must not raise
        await provider._backfill_unanalyzed_tracks()

    @pytest.mark.asyncio
    async def test_backfill_continues_after_per_track_failure(self, tmp_path: Any) -> None:
        """_backfill_unanalyzed_tracks must not abort after a single-track analysis failure."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)

        track_a = _make_mock_track("track_a", "local")
        track_b = _make_mock_track("track_b", "local")
        mass.music.tracks = MagicMock()
        mass.music.tracks.library_items = AsyncMock(return_value=[track_a, track_b])

        call_count = 0

        async def _fetch_side_effect(item_id: str, _provider: str, _prov_id: str | None = None) -> None:
            nonlocal call_count
            call_count += 1
            if item_id == "track_a":
                raise RuntimeError("Analysis failed")

        provider = _make_provider_with_config(mass)
        with patch(_USEARCH_PATCH, create=True):
            await provider.handle_async_init()

        get_sig = patch.object(provider, "get_sonic_signature", AsyncMock(return_value=None))
        fetch = patch.object(
            provider, "_fetch_and_analyze", AsyncMock(side_effect=_fetch_side_effect)
        )
        rebuild = patch.object(provider, "_rebuild_search_index", AsyncMock())
        with get_sig, fetch, rebuild:
            await provider._backfill_unanalyzed_tracks()

        # track_b must have been attempted despite track_a failing
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_backfill_rebuilds_index_after_analyzing_tracks(self, tmp_path: Any) -> None:
        """_backfill_unanalyzed_tracks must call _rebuild_search_index after analyzing tracks."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)

        track = _make_mock_track("track_1", "local")
        mass.music.tracks = MagicMock()
        mass.music.tracks.library_items = AsyncMock(return_value=[track])

        provider = _make_provider_with_config(mass)
        with patch(_USEARCH_PATCH, create=True):
            await provider.handle_async_init()

        get_sig = patch.object(provider, "get_sonic_signature", AsyncMock(return_value=None))
        fetch = patch.object(provider, "_fetch_and_analyze", AsyncMock())
        rebuild = patch.object(provider, "_rebuild_search_index", AsyncMock())
        with get_sig, fetch, rebuild as mock_rebuild:
            await provider._backfill_unanalyzed_tracks()

        mock_rebuild.assert_called_once()

    @pytest.mark.asyncio
    async def test_backfill_does_not_rebuild_index_when_no_tracks_analyzed(
        self, tmp_path: Any
    ) -> None:
        """_backfill_unanalyzed_tracks must not rebuild the index when all tracks were skipped."""
        mass = _make_mock_mass()
        mass.storage_path = str(tmp_path)

        track = _make_mock_track("track_1", "local")
        mass.music.tracks = MagicMock()
        mass.music.tracks.library_items = AsyncMock(return_value=[track])

        existing_sig = MagicMock()

        provider = _make_provider_with_config(mass)
        with patch(_USEARCH_PATCH, create=True):
            await provider.handle_async_init()

        get_sig = patch.object(
            provider, "get_sonic_signature", AsyncMock(return_value=existing_sig)
        )
        rebuild = patch.object(provider, "_rebuild_search_index", AsyncMock())
        with get_sig, rebuild as mock_rebuild:
            await provider._backfill_unanalyzed_tracks()

        mock_rebuild.assert_not_called()
