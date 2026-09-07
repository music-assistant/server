"""Tests for the cross-provider hooks (get_similar_tracks / recommendations rows + items)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import RecommendationFolder

from tests.common import use_real_create_task
from tests.providers.sonic_similarity.conftest import make_item_mapping, make_track

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

ROW_ID = "inspired_by_recently_played"


class TestGetSimilarTracks:
    """Tests for SonicSimilarityPlugin.get_similar_tracks."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_corpus_not_ready(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """No corpus means no candidates — the dispatcher gets an empty list."""
        plugin = make_plugin()
        track = make_track("any_id")
        result = await plugin.get_similar_tracks(track)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_mapping_intersects_index(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """A track whose mappings don't match the index produces no seed."""
        plugin = make_plugin(signatures={("spotify", "known_id"): [0.1] * 18})
        track = make_track("unknown_id", provider="other_provider")
        plugin._handle_similar = AsyncMock(return_value={"items": []})
        result = await plugin.get_similar_tracks(track)
        assert result == []
        plugin._handle_similar.assert_not_called()

    @pytest.mark.asyncio
    async def test_primary_cache_hit_returns_resolved_tracks(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """A mapping matching the (item_id, provider_instance) cache is used as the seed."""
        plugin = make_plugin(signatures={("spotify", "seed_id"): [0.1] * 18})
        track = make_track("seed_id", provider="spotify")
        plugin._handle_similar = AsyncMock(
            return_value={
                "items": [{"item_id": "result1", "provider": "spotify", "distance": 0.5}],
            }
        )
        resolved_track = make_track("result1")
        mock_mass.music.tracks.get = AsyncMock(return_value=resolved_track)

        result = await plugin.get_similar_tracks(track, limit=10)

        assert result == [resolved_track]
        plugin._handle_similar.assert_awaited_once_with(
            item_id="seed_id", seed_provider="spotify", limit=10, preset="balanced", diversity=0.0
        )
        mock_mass.music.tracks.get.assert_awaited_once_with("result1", "spotify")

    @pytest.mark.asyncio
    async def test_falls_back_to_signatures_by_id(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """When (item_id, provider_instance) misses, the by-id fallback supplies the seed."""
        plugin = make_plugin(signatures={("spotify", "track_a"): [0.1] * 18})
        # Mapping uses a different provider_instance but the same item_id, so the
        # primary cache misses and the by-id fallback should kick in.
        track = make_track("track_a", provider="tidal")
        plugin._handle_similar = AsyncMock(
            return_value={
                "items": [{"item_id": "r1", "provider": "spotify", "distance": 0.2}],
            }
        )
        mock_mass.music.tracks.get = AsyncMock(return_value=make_track("r1"))

        result = await plugin.get_similar_tracks(track)

        assert len(result) == 1
        plugin._handle_similar.assert_awaited_once_with(
            item_id="track_a", seed_provider=None, limit=25, preset="balanced", diversity=0.0
        )

    @pytest.mark.asyncio
    async def test_skips_tracks_that_fail_to_resolve(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """Resolution failures are swallowed; only successfully resolved tracks come back."""
        plugin = make_plugin(signatures={("spotify", "seed_id"): [0.1] * 18})
        track = make_track("seed_id", provider="spotify")
        plugin._handle_similar = AsyncMock(
            return_value={
                "items": [
                    {"item_id": "good", "provider": "spotify", "distance": 0.1},
                    {"item_id": "bad", "provider": "spotify", "distance": 0.2},
                ],
            }
        )
        good_track = make_track("good")

        async def _fake_get(item_id: str, _provider: str, **_kwargs: Any) -> MagicMock:
            if item_id == "bad":
                raise MusicAssistantError("nope")
            return good_track

        mock_mass.music.tracks.get = AsyncMock(side_effect=_fake_get)

        result = await plugin.get_similar_tracks(track)
        assert result == [good_track]

    @pytest.mark.asyncio
    async def test_skips_library_mapping(self, make_plugin: Callable[..., Any]) -> None:
        """Library mappings are skipped even when their item_id is indexed."""
        plugin = make_plugin(signatures={("spotify", "indexed_id"): [0.1] * 18})
        track = make_track("indexed_id", provider="other")
        # Two mappings: a library one matching the cache by item_id (should be
        # skipped) and a streaming one with a non-matching id.
        library_mapping = MagicMock()
        library_mapping.item_id = "indexed_id"
        library_mapping.provider_instance = "library"
        library_mapping.provider_domain = "library"
        streaming_mapping = MagicMock()
        streaming_mapping.item_id = "other_id"
        streaming_mapping.provider_instance = "other"
        streaming_mapping.provider_domain = "other"
        track.provider_mappings = [library_mapping, streaming_mapping]
        plugin._handle_similar = AsyncMock(return_value={"items": []})

        result = await plugin.get_similar_tracks(track)
        assert result == []
        plugin._handle_similar.assert_not_called()


class TestGetRecommendations:
    """Tests for SonicSimilarityPlugin.get_recommendations (rows, without items)."""

    @pytest.mark.asyncio
    async def test_returns_static_row_without_backend_io(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """The row descriptor is returned with empty items and zero backend calls."""
        plugin = make_plugin(signatures={("spotify", "seed_id"): [0.1] * 18})
        result = await plugin.get_recommendations()
        assert len(result) == 1
        folder = result[0]
        assert isinstance(folder, RecommendationFolder)
        assert folder.item_id == ROW_ID
        assert folder.provider == plugin.instance_id
        assert folder.name == "Inspired by recently played"
        assert folder.translation_key == ROW_ID
        assert folder.icon == "mdi-shimmer"
        assert len(folder.items) == 0
        # Rows are contractually I/O-free.
        assert mock_mass.music.recently_played.await_count == 0
        assert mock_mass.music.tracks.get.await_count == 0

    @pytest.mark.asyncio
    async def test_returns_empty_when_disabled_via_config(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """When CONF_ENABLE_DISCOVER_ROW is False, no row is advertised."""
        plugin = make_plugin(
            discover_row_enabled=False,
            signatures={("spotify", "seed_id"): [0.1] * 18},
        )
        result = await plugin.get_recommendations()
        assert result == []
        assert mock_mass.music.recently_played.await_count == 0

    @pytest.mark.asyncio
    async def test_row_advertised_even_when_corpus_not_ready(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """Engine readiness is an items-time concern; the row itself is still advertised."""
        plugin = make_plugin()
        result = await plugin.get_recommendations()
        assert len(result) == 1
        assert result[0].item_id == ROW_ID
        assert mock_mass.music.recently_played.await_count == 0


class TestGetRecommendationItems:
    """Tests for SonicSimilarityPlugin.get_recommendation_items."""

    @pytest.fixture(autouse=True)
    def _real_create_task(self, mock_mass: MagicMock) -> None:
        """Let the @use_cache decorator on the row fetch dispatch through a real task."""
        use_real_create_task(mock_mass)

    @pytest.mark.asyncio
    async def test_unknown_item_id_returns_empty_without_backend_io(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """An item_id we never advertised yields [] and triggers no backend fetch."""
        plugin = make_plugin(signatures={("spotify", "seed_id"): [0.1] * 18})
        plugin._handle_similar = AsyncMock(return_value={"items": []})
        result = await plugin.get_recommendation_items("bogus_row")
        assert result == []
        assert mock_mass.music.recently_played.await_count == 0
        plugin._handle_similar.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_empty_when_disabled_via_config(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """When CONF_ENABLE_DISCOVER_ROW is False, short-circuit before touching the corpus."""
        plugin = make_plugin(
            discover_row_enabled=False,
            signatures={("spotify", "seed_id"): [0.1] * 18},
        )
        result = await plugin.get_recommendation_items(ROW_ID)
        assert result == []
        # And we never queried recently_played at all.
        assert mock_mass.music.recently_played.await_count == 0

    @pytest.mark.asyncio
    async def test_returns_empty_when_corpus_not_ready(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """Without a corpus, the advertised row serves no items."""
        plugin = make_plugin()
        result = await plugin.get_recommendation_items(ROW_ID)
        assert result == []

    @pytest.mark.asyncio
    async def test_passes_preset_and_diversity_to_handle_similar(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """Preset + diversity config values propagate to each _handle_similar call."""
        plugin = make_plugin(
            signatures={("spotify", "seed1"): [0.1] * 18},
            discover_preset="vibe",
            discover_diversity=7,
        )
        mock_mass.music.recently_played = AsyncMock(return_value=[make_item_mapping("recent1")])
        recent_track = make_track("seed1", provider="spotify")
        resolved = make_track("r1")

        async def _fake_get(item_id: str, _provider: str, **_kwargs: Any) -> MagicMock:
            return recent_track if item_id == "recent1" else resolved

        mock_mass.music.tracks.get = AsyncMock(side_effect=_fake_get)
        plugin._handle_similar = AsyncMock(
            return_value={
                "items": [{"item_id": "r1", "provider": "spotify", "distance": 0.3}],
            }
        )

        await plugin.get_recommendation_items(ROW_ID)

        plugin._handle_similar.assert_awaited()
        # Every call should carry the configured preset + diversity.
        for call in plugin._handle_similar.await_args_list:
            assert call.kwargs["preset"] == "vibe"
            assert call.kwargs["diversity"] == 0.7

    @pytest.mark.asyncio
    async def test_requests_partial_plays_from_recently_played(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """
        The items build asks for partial plays and does NOT filter on user_initiated.

        MA sets user_initiated=True only for items the user explicitly chose (a
        container such as an album, artist, playlist or genre, or a single track played
        directly). Tracks that play as part of a container or as radio fill -- exactly
        the track-level plays we want as seeds -- stay user_initiated=False, so
        restricting to user_initiated=True would exclude them.
        """
        plugin = make_plugin(signatures={("spotify", "seed_id"): [0.1] * 18})
        mock_mass.music.recently_played = AsyncMock(return_value=[])
        await plugin.get_recommendation_items(ROW_ID)
        call_kwargs = mock_mass.music.recently_played.await_args.kwargs
        assert call_kwargs["fully_played_only"] is False
        assert "user_initiated_only" not in call_kwargs

    @pytest.mark.asyncio
    async def test_returns_empty_when_recently_played_is_empty(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """No recent tracks → no seeds → empty result."""
        plugin = make_plugin(signatures={("spotify", "seed_id"): [0.1] * 18})
        mock_mass.music.recently_played = AsyncMock(return_value=[])
        result = await plugin.get_recommendation_items(ROW_ID)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_seed_intersects_index(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """Recent tracks whose mappings don't intersect the index produce no items."""
        plugin = make_plugin(signatures={("spotify", "known_id"): [0.1] * 18})
        mock_mass.music.recently_played = AsyncMock(return_value=[make_item_mapping("lib_1")])
        # Resolved track has only a non-matching mapping.
        mock_mass.music.tracks.get = AsyncMock(
            return_value=make_track("some_other_id", provider="other_provider")
        )
        plugin._handle_similar = AsyncMock(return_value={"items": []})

        result = await plugin.get_recommendation_items(ROW_ID)
        assert result == []
        plugin._handle_similar.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_returns_resolved_items(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """One recent track resolves to one indexed seed and produces the row's items."""
        plugin = make_plugin(signatures={("spotify", "seed1"): [0.1] * 18})
        mock_mass.music.recently_played = AsyncMock(return_value=[make_item_mapping("recent1")])
        recent_track = make_track("seed1", provider="spotify")
        resolved = make_track("r1")

        async def _fake_get(item_id: str, _provider: str, **_kwargs: Any) -> MagicMock:
            if item_id == "recent1":
                return recent_track
            return resolved

        mock_mass.music.tracks.get = AsyncMock(side_effect=_fake_get)
        plugin._handle_similar = AsyncMock(
            return_value={
                "items": [{"item_id": "r1", "provider": "spotify", "distance": 0.3}],
            }
        )

        result = await plugin.get_recommendation_items(ROW_ID)

        assert list(result) == [resolved]

    @pytest.mark.asyncio
    async def test_dedupes_across_multiple_seeds(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """Overlapping candidates from multiple seeds collapse to one entry each."""
        plugin = make_plugin(
            signatures={
                ("spotify", "seed1"): [0.1] * 18,
                ("spotify", "seed2"): [0.2] * 18,
            }
        )
        mock_mass.music.recently_played = AsyncMock(
            return_value=[
                make_item_mapping("recent1"),
                make_item_mapping("recent2"),
            ]
        )
        recent_track1 = make_track("seed1", provider="spotify")
        recent_track2 = make_track("seed2", provider="spotify")
        resolved_a = make_track("cand_a")
        resolved_b = make_track("cand_b")

        async def _fake_get(item_id: str, _provider: str, **_kwargs: Any) -> MagicMock:
            return {
                "recent1": recent_track1,
                "recent2": recent_track2,
                "cand_a": resolved_a,
                "cand_b": resolved_b,
            }[item_id]

        mock_mass.music.tracks.get = AsyncMock(side_effect=_fake_get)

        async def _fake_handle_similar(*, item_id: str, **_kwargs: object) -> dict[str, Any]:
            if item_id == "seed1":
                return {
                    "items": [
                        {"item_id": "cand_a", "provider": "spotify", "distance": 0.1},
                        {"item_id": "cand_b", "provider": "spotify", "distance": 0.2},
                    ],
                }
            return {
                "items": [
                    # cand_a is a repeat of seed1's first hit; cand_b also overlaps.
                    {"item_id": "cand_a", "provider": "spotify", "distance": 0.3},
                    {"item_id": "cand_b", "provider": "spotify", "distance": 0.4},
                ],
            }

        plugin._handle_similar = AsyncMock(side_effect=_fake_handle_similar)

        result = await plugin.get_recommendation_items(ROW_ID)

        item_ids = [item.item_id for item in result]
        # Each unique candidate appears exactly once.
        assert sorted(item_ids) == ["cand_a", "cand_b"]

    @pytest.mark.asyncio
    async def test_clap_engine_routes_discover_through_clap(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """discover_engine=clap fans out via _handle_similar_clap, not the 18-dim path."""
        plugin = make_plugin(
            clap_enabled=True,
            discover_engine="clap",
            signatures={("spotify", "seed1"): [0.1] * 18},
        )
        # CLAP index reports the recent track's mapping as present.
        plugin._clap_index.contains = MagicMock(return_value=True)
        mock_mass.music.recently_played = AsyncMock(return_value=[make_item_mapping("recent1")])
        recent_track = make_track("seed1", provider="spotify")
        resolved = make_track("r1")

        async def _fake_get(item_id: str, _provider: str, **_kwargs: Any) -> MagicMock:
            return recent_track if item_id == "recent1" else resolved

        mock_mass.music.tracks.get = AsyncMock(side_effect=_fake_get)
        plugin._handle_similar_clap = AsyncMock(
            return_value={"items": [{"item_id": "r1", "provider": "spotify", "distance": 0.3}]}
        )
        plugin._handle_similar = AsyncMock(return_value={"items": []})

        result = await plugin.get_recommendation_items(ROW_ID)

        assert len(result) == 1
        plugin._handle_similar.assert_not_called()
        call = plugin._handle_similar_clap.await_args
        assert call.kwargs["item_id"] == "seed1"
        # The provider is forwarded so the seed embedding is fetched in O(1).
        assert call.kwargs["seed_provider"] == "spotify"
