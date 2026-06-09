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


def _make_mock_encoder(vector: np.ndarray) -> MagicMock:
    """Build a CLAP-like encoder whose get_text_embeddings([q]) yields [tensor_chain]."""
    encoder = MagicMock()
    encoder.get_text_embeddings.return_value = [_make_tensor_chain(vector)]
    return encoder


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


class TestHandleTextSearch:
    """Tests for SonicSimilarityPlugin._handle_text_search."""

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
        vector = np.zeros((1024,), dtype=np.float32)
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
        vector = np.zeros((1024,), dtype=np.float32)
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
        vector = np.zeros((1024,), dtype=np.float32)
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
