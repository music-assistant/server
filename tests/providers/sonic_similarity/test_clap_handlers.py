"""Tests for SonicSimilarityPlugin CLAP handler and rebuild methods."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from music_assistant.providers.sonic_similarity.similarity import ScoredCandidate
from tests.providers.sonic_similarity.conftest import make_analysis_row

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


class TestHandleSimilarClap:
    """Tests for SonicSimilarityPlugin._handle_similar_clap."""

    @pytest.mark.asyncio
    async def test_clap_index_disabled_when_no_index(self, make_plugin: Callable[..., Any]) -> None:
        """Returns clap_index_disabled when no CLAP index attached."""
        plugin = make_plugin(signatures={("spotify", "seed"): [0.0] * 18})
        result = await plugin._handle_similar_clap("any_id")
        assert result["analyzed"] is False
        assert result["reason"] == "clap_index_disabled"
        assert result["seed_track_id"] == "any_id"
        assert result["items"] == []

    @pytest.mark.asyncio
    async def test_seed_not_in_index(self, make_plugin: Callable[..., Any]) -> None:
        """Returns seed_not_in_index when get_embedding_by_item_id returns None."""
        plugin = make_plugin(clap_enabled=True, signatures={("spotify", "seed"): [0.0] * 18})
        result = await plugin._handle_similar_clap("seed")
        assert result["analyzed"] is False
        assert result["reason"] == "seed_not_in_index"
        assert result["items"] == []

    @pytest.mark.asyncio
    async def test_happy_path_excludes_seed(self, make_plugin: Callable[..., Any]) -> None:
        """Returns ranked items, drops the seed and respects limit ordering."""
        plugin = make_plugin(clap_enabled=True, signatures={("spotify", "seed"): [0.0] * 18})
        plugin._clap_index.get_embedding_by_item_id.return_value = (
            "spotify",
            np.zeros(1024, dtype=np.float32),
        )
        plugin._clap_index.search = AsyncMock(
            return_value=[
                ScoredCandidate("seed", "spotify", 0.0),
                ScoredCandidate("other1", "tidal", 0.2),
                ScoredCandidate("other2", "apple", 0.3),
            ]
        )
        result = await plugin._handle_similar_clap("seed", limit=2)
        assert result["analyzed"] is True
        assert result["seed_track_id"] == "seed"
        assert len(result["items"]) == 2
        ids = [item["item_id"] for item in result["items"]]
        assert "seed" not in ids
        assert ids == ["other1", "other2"]
        assert result["items"][0]["provider"] == "tidal"
        assert result["items"][0]["distance"] == 0.2

    @pytest.mark.asyncio
    async def test_respects_limit(self, make_plugin: Callable[..., Any]) -> None:
        """Caps the items list at limit even when search returns many candidates."""
        plugin = make_plugin(clap_enabled=True, signatures={("spotify", "seed"): [0.0] * 18})
        plugin._clap_index.get_embedding_by_item_id.return_value = (
            "spotify",
            np.zeros(1024, dtype=np.float32),
        )
        plugin._clap_index.search = AsyncMock(
            return_value=[
                ScoredCandidate(f"track_{i}", "spotify", float(i) * 0.01) for i in range(10)
            ]
        )
        result = await plugin._handle_similar_clap("seed", limit=3)
        assert result["analyzed"] is True
        assert len(result["items"]) == 3


class TestRebuildClapIndexFromDatabase:
    """Tests for SonicSimilarityPlugin._rebuild_clap_index_from_database."""

    @pytest.mark.asyncio
    async def test_noop_when_clap_index_disabled(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """Early-returns without touching audio_analysis when index disabled."""
        plugin = make_plugin()
        await plugin._rebuild_clap_index_from_database()
        assert mock_mass.streams.audio_analysis.iter_audio_analysis_rows.call_count == 0

    @pytest.mark.asyncio
    async def test_adds_new_embeddings_and_saves(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """Adds each unique new embedding to the index and persists once."""
        mock_mass._iter_audio_analysis_rows_data = [
            make_analysis_row(item_id="a", provider="spotify", clap_embedding=[0.1] * 1024),
            make_analysis_row(item_id="b", provider="spotify", clap_embedding=[0.2] * 1024),
        ]
        plugin = make_plugin(clap_enabled=True)
        await plugin._rebuild_clap_index_from_database()
        assert plugin._clap_index.add.await_count == 2
        call_args = [call.args for call in plugin._clap_index.add.await_args_list]
        # Each call: (provider, item_id, ndarray)
        assert call_args[0][0] == "spotify"
        assert call_args[0][1] == "a"
        assert isinstance(call_args[0][2], np.ndarray)
        assert call_args[0][2].shape == (1024,)
        assert call_args[1][0] == "spotify"
        assert call_args[1][1] == "b"
        assert isinstance(call_args[1][2], np.ndarray)
        plugin._clap_index.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_already_indexed_rows(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """Rows whose (provider, item_id) is contained() are skipped entirely."""
        mock_mass._iter_audio_analysis_rows_data = [
            make_analysis_row(item_id="a", clap_embedding=[0.1] * 1024),
            make_analysis_row(item_id="b", clap_embedding=[0.2] * 1024),
        ]
        plugin = make_plugin(clap_enabled=True)
        plugin._clap_index.contains = MagicMock(return_value=True)
        await plugin._rebuild_clap_index_from_database()
        plugin._clap_index.add.assert_not_awaited()
        plugin._clap_index.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_malformed_json(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """A row with non-JSON analysis_data is skipped without crashing."""
        malformed_row = {
            "item_id": "x",
            "provider": "spotify",
            "aa_provider_domain": "sonic_analysis",
            "analysis_data": "not-valid-json",
        }
        mock_mass._iter_audio_analysis_rows_data = [malformed_row]
        plugin = make_plugin(clap_enabled=True)
        await plugin._rebuild_clap_index_from_database()
        plugin._clap_index.add.assert_not_awaited()
        plugin._clap_index.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_rows_missing_clap_embedding(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """Rows whose extra_data lacks clap_embedding are skipped."""
        mock_mass._iter_audio_analysis_rows_data = [
            make_analysis_row(item_id="x", clap_embedding=None),
        ]
        plugin = make_plugin(clap_enabled=True)
        await plugin._rebuild_clap_index_from_database()
        plugin._clap_index.add.assert_not_awaited()
        plugin._clap_index.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_wrong_shape_embedding(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """Rows whose embedding parses but isn't 1024-dim are rejected."""
        mock_mass._iter_audio_analysis_rows_data = [
            make_analysis_row(item_id="x", clap_embedding=[0.1] * 100),
        ]
        plugin = make_plugin(clap_enabled=True)
        await plugin._rebuild_clap_index_from_database()
        plugin._clap_index.add.assert_not_awaited()
        plugin._clap_index.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dedupes_within_single_rebuild(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """Duplicate (provider, item_id) rows in a single rebuild add at most once."""
        mock_mass._iter_audio_analysis_rows_data = [
            make_analysis_row(item_id="a", provider="spotify", clap_embedding=[0.1] * 1024),
            make_analysis_row(item_id="a", provider="spotify", clap_embedding=[0.1] * 1024),
        ]
        plugin = make_plugin(clap_enabled=True)
        await plugin._rebuild_clap_index_from_database()
        assert plugin._clap_index.add.await_count == 1
