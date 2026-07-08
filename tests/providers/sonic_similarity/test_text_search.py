"""Unit tests for the lazy CLAP text encoder and _handle_text_search dispatcher hook."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from music_assistant_models.errors import MusicAssistantError

from music_assistant.providers.sonic_similarity.similarity import ScoredCandidate
from tests.providers.sonic_similarity.conftest import make_track

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


def _make_tensor_chain(vector: np.ndarray) -> MagicMock:
    """Build a tensor-like MagicMock whose .detach().cpu().numpy() yields vector."""
    chain = MagicMock()
    chain.detach.return_value = chain
    chain.cpu.return_value = chain
    chain.numpy.return_value = vector
    return chain


def _make_mock_encoder(*vectors: np.ndarray) -> MagicMock:
    """
    Build a CLAP-like encoder returning one tensor chain per prompt.

    get_text_embeddings(prompts) yields a chain for each prompt, drawn in order
    from the supplied vectors, so multi-prompt calls (query + exclude) work.
    """
    chains = [_make_tensor_chain(v) for v in vectors]
    encoder = MagicMock()
    encoder.get_text_embeddings.side_effect = lambda prompts: chains[: len(prompts)]
    return encoder


def _unit_vector(*components: float) -> np.ndarray:
    """Return a 1024-dim float32 vector with the leading components set, rest zero."""
    vec = np.zeros((1024,), dtype=np.float32)
    vec[: len(components)] = components
    return vec


class TestGetTextEncoder:
    """Tests for SonicSimilarityPlugin._get_text_encoder (lazy load + cache)."""

    @pytest.mark.asyncio
    async def test_returns_cached_encoder_on_second_call(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """Second call returns the cached encoder without reloading."""
        plugin = make_plugin()
        loader = MagicMock(return_value="ENCODER_SENTINEL")
        plugin._load_text_encoder = loader

        first = await plugin._get_text_encoder()
        second = await plugin._get_text_encoder()

        assert first == "ENCODER_SENTINEL"
        assert second == "ENCODER_SENTINEL"
        assert loader.call_count == 1

    @pytest.mark.asyncio
    async def test_returns_none_when_load_raises(self, make_plugin: Callable[..., Any]) -> None:
        """Encoder load failure is swallowed; method returns None and caches None."""
        plugin = make_plugin()
        plugin._load_text_encoder = MagicMock(side_effect=RuntimeError("boom"))

        result = await plugin._get_text_encoder()

        assert result is None
        assert plugin._text_encoder is None


class TestEmbedTextQuery:
    """Tests for SonicSimilarityPlugin._embed_text_query (template + exclusion math)."""

    @pytest.mark.asyncio
    async def test_returns_unit_vector(self, make_plugin: Callable[..., Any]) -> None:
        """A plain query yields the unit-normalised encoder embedding."""
        plugin = make_plugin(clap_enabled=True)
        plugin._load_text_encoder = lambda: _make_mock_encoder(_unit_vector(3.0, 4.0))

        result = await plugin._embed_text_query("disco")

        assert result is not None
        np.testing.assert_allclose(result[:2], [0.6, 0.8], atol=1e-6)

    @pytest.mark.asyncio
    async def test_exclude_subtracts_normalised_direction(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """Exclude subtracts the unit exclude direction: [1,0]-[0,1] -> [.707,-.707]."""
        plugin = make_plugin(clap_enabled=True)
        keep, excl = _unit_vector(1.0), _unit_vector(0.0, 1.0)
        plugin._load_text_encoder = lambda: _make_mock_encoder(keep, excl)

        result = await plugin._embed_text_query("loud", exclude="fast", exclude_weight=1.0)

        assert result is not None
        np.testing.assert_allclose(result[:2], [0.70710677, -0.70710677], atol=1e-6)

    @pytest.mark.asyncio
    async def test_exclude_weight_zero_returns_keep(self, make_plugin: Callable[..., Any]) -> None:
        """weight=0 leaves the query embedding unchanged."""
        plugin = make_plugin(clap_enabled=True)
        keep, excl = _unit_vector(1.0), _unit_vector(0.0, 1.0)
        plugin._load_text_encoder = lambda: _make_mock_encoder(keep, excl)

        result = await plugin._embed_text_query("loud", exclude="fast", exclude_weight=0.0)

        assert result is not None
        np.testing.assert_allclose(result[:2], [1.0, 0.0], atol=1e-6)

    @pytest.mark.asyncio
    async def test_query_equals_exclude_returns_none(self, make_plugin: Callable[..., Any]) -> None:
        """Identical keep/exclude cancel to a zero vector -> None (cosine-unsafe)."""
        plugin = make_plugin(clap_enabled=True)
        same = _unit_vector(1.0)
        plugin._load_text_encoder = lambda: _make_mock_encoder(same, same)

        result = await plugin._embed_text_query("loud", exclude="loud", exclude_weight=1.0)

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_query_returns_none_without_encoding(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """A whitespace-only query short-circuits to None and never encodes."""
        plugin = make_plugin(clap_enabled=True)
        encoder = _make_mock_encoder(_unit_vector(1.0))
        plugin._load_text_encoder = lambda: encoder

        result = await plugin._embed_text_query("   ")

        assert result is None
        encoder.get_text_embeddings.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_norm_embedding_returns_none(self, make_plugin: Callable[..., Any]) -> None:
        """A zero-norm embedding cannot be ranked by cosine -> None."""
        plugin = make_plugin(clap_enabled=True)
        plugin._load_text_encoder = lambda: _make_mock_encoder(np.zeros((1024,), dtype=np.float32))

        result = await plugin._embed_text_query("disco")

        assert result is None


class TestHandleTextSearch:
    """Tests for SonicSimilarityPlugin._handle_text_search."""

    @pytest.mark.asyncio
    async def test_empty_query_reports_empty_query(self, make_plugin: Callable[..., Any]) -> None:
        """A whitespace-only query is rejected with the empty_query reason."""
        plugin = make_plugin(clap_enabled=True)
        plugin._clap_index.__len__ = MagicMock(return_value=5)

        result = await plugin._handle_text_search("   ")

        assert result["analyzed"] is False
        assert result["reason"] == "empty_query"

    @pytest.mark.asyncio
    async def test_exclude_weight_clamped_to_range(self, make_plugin: Callable[..., Any]) -> None:
        """exclude_weight is clamped to [0, 2] before reaching the embedder."""
        plugin = make_plugin(clap_enabled=True)
        plugin._clap_index.__len__ = MagicMock(return_value=5)
        plugin._embed_text_query = AsyncMock(return_value=None)

        await plugin._handle_text_search("disco", exclude="vocals", exclude_weight=9.0)

        assert plugin._embed_text_query.await_args.kwargs["exclude_weight"] == 2.0

    @pytest.mark.asyncio
    async def test_returns_clap_index_empty_when_no_index(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """Missing CLAP index short-circuits with clap_index_empty."""
        plugin = make_plugin()

        result = await plugin._handle_text_search("disco")

        assert result["analyzed"] is False
        assert result["reason"] == "clap_index_empty"
        assert result["items"] == []

    @pytest.mark.asyncio
    async def test_returns_clap_index_empty_when_index_is_empty(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """Empty CLAP index (len == 0) short-circuits with clap_index_empty."""
        plugin = make_plugin(clap_enabled=True)

        result = await plugin._handle_text_search("disco")

        assert result["analyzed"] is False
        assert result["reason"] == "clap_index_empty"

    @pytest.mark.asyncio
    async def test_text_encoder_unavailable_when_load_fails(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """When the encoder fails to load the hook reports text_encoder_unavailable."""
        plugin = make_plugin(clap_enabled=True)
        plugin._clap_index.__len__ = MagicMock(return_value=5)
        plugin._load_text_encoder = MagicMock(side_effect=RuntimeError("nope"))

        result = await plugin._handle_text_search("disco")

        assert result["analyzed"] is False
        assert result["reason"] == "text_encoder_unavailable"
        assert result["items"] == []

    @pytest.mark.asyncio
    async def test_happy_path_returns_ranked_items_without_resolve(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """resolve=False returns ranked (provider, item_id, distance) entries."""
        plugin = make_plugin(clap_enabled=True)
        plugin._clap_index.__len__ = MagicMock(return_value=5)
        vector = _unit_vector(1.0)
        plugin._load_text_encoder = lambda: _make_mock_encoder(vector)
        plugin._clap_index.search = AsyncMock(
            return_value=[
                ScoredCandidate("track_a", "spotify", 0.1),
                ScoredCandidate("track_b", "tidal", 0.2),
            ]
        )

        result = await plugin._handle_text_search("disco", limit=5)

        assert result["analyzed"] is True
        assert result["query"] == "disco"
        assert result["items"] == [
            {"provider": "spotify", "item_id": "track_a", "distance": 0.1},
            {"provider": "tidal", "item_id": "track_b", "distance": 0.2},
        ]

    @pytest.mark.asyncio
    async def test_happy_path_resolve_true_adds_name_and_artist(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """resolve=True augments each entry with name + comma-joined artist string."""
        plugin = make_plugin(clap_enabled=True)
        plugin._clap_index.__len__ = MagicMock(return_value=5)
        vector = _unit_vector(1.0)
        plugin._load_text_encoder = lambda: _make_mock_encoder(vector)
        plugin._clap_index.search = AsyncMock(
            return_value=[
                ScoredCandidate("track_a", "spotify", 0.1),
                ScoredCandidate("track_b", "tidal", 0.2),
            ]
        )
        mock_mass.music.tracks.get.side_effect = [
            make_track("track_a", provider="spotify", name="X", artists=("A1", "A2")),
            make_track("track_b", provider="tidal", name="Y", artists=("B1",)),
        ]

        result = await plugin._handle_text_search("disco", resolve=True)

        assert result["analyzed"] is True
        items_by_id = {entry["item_id"]: entry for entry in result["items"]}
        assert items_by_id["track_a"]["name"] == "X"
        assert items_by_id["track_a"]["artist"] == "A1, A2"
        assert items_by_id["track_b"]["name"] == "Y"
        assert items_by_id["track_b"]["artist"] == "B1"

    @pytest.mark.asyncio
    async def test_resolve_true_handles_music_assistant_error(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """A failed resolve falls back to '(unknown)'/'' but still returns the entry."""
        plugin = make_plugin(clap_enabled=True)
        plugin._clap_index.__len__ = MagicMock(return_value=5)
        vector = _unit_vector(1.0)
        plugin._load_text_encoder = lambda: _make_mock_encoder(vector)
        plugin._clap_index.search = AsyncMock(
            return_value=[
                ScoredCandidate("track_a", "spotify", 0.1),
                ScoredCandidate("track_b", "tidal", 0.2),
            ]
        )
        mock_mass.music.tracks.get.side_effect = [
            MusicAssistantError("not found"),
            make_track("track_b", provider="tidal", name="Y", artists=("B1",)),
        ]

        result = await plugin._handle_text_search("disco", resolve=True)

        assert result["analyzed"] is True
        items_by_id = {entry["item_id"]: entry for entry in result["items"]}
        assert items_by_id["track_a"]["name"] == "(unknown)"
        assert items_by_id["track_a"]["artist"] == ""
        assert items_by_id["track_b"]["name"] == "Y"
        assert items_by_id["track_b"]["artist"] == "B1"
