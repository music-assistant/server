"""Tests for ProviderFeature.SEARCH wiring and the search() dispatcher hook."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import MusicAssistantError

from music_assistant.providers.sonic_similarity import SonicSimilarityPlugin, setup
from music_assistant.providers.sonic_similarity import clap_index as clap_index_module
from music_assistant.providers.sonic_similarity.similarity import ScoredCandidate
from tests.providers.sonic_similarity.conftest import make_track

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


def _make_config(*, enable_text_search: bool) -> MagicMock:
    """Build a ProviderConfig mock with the keys setup()/__init__ read."""
    config = MagicMock()
    config_values = {
        "log_level": "GLOBAL",
        "aa_provider_domain": "sonic_analysis",
        "enable_clap_index": False,
        "enable_text_search": enable_text_search,
        "enable_discover_row": True,
        "discover_preset": "discover",
        "discover_diversity": 0.2,
    }
    config.get_value = lambda key: config_values.get(key)
    return config


def _make_manifest() -> MagicMock:
    """Build a ProviderManifest mock with the attributes the base Provider reads."""
    manifest = MagicMock()
    manifest.instance_id = "test-instance-id"
    manifest.domain = "sonic_similarity"
    return manifest


class TestSetupConditionalSearch:
    """setup() advertises ProviderFeature.SEARCH only when text search is enabled."""

    @pytest.mark.asyncio
    async def test_search_feature_present_when_text_search_enabled(
        self, mock_mass: MagicMock
    ) -> None:
        """CONF_ENABLE_TEXT_SEARCH=True → plugin advertises SEARCH."""
        plugin = await setup(mock_mass, _make_manifest(), _make_config(enable_text_search=True))
        assert ProviderFeature.SEARCH in plugin.supported_features

    @pytest.mark.asyncio
    async def test_search_feature_absent_when_text_search_disabled(
        self, mock_mass: MagicMock
    ) -> None:
        """CONF_ENABLE_TEXT_SEARCH=False → plugin does not advertise SEARCH."""
        plugin = await setup(mock_mass, _make_manifest(), _make_config(enable_text_search=False))
        assert ProviderFeature.SEARCH not in plugin.supported_features


class TestTextEncoderWarmsLazily:
    """
    The GPT2 text encoder is not warmed at load; the first search() warms it lazily.

    Warming the ~500MB encoder eagerly at startup defeats the point of an opt-in
    feature, so loaded_in_mass leaves it cold and the first cold search() kicks off
    a one-time background warm (see TestSearch.test_schedules_warm_when_encoder_cold).
    """

    @pytest.mark.asyncio
    async def test_no_warm_task_at_load_when_text_search_enabled(
        self, mock_mass: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """text_search_enabled → loaded_in_mass does NOT warm the encoder (it loads on first query)."""

        async def _noop_load(_self: Any) -> None:
            return None

        # text_search auto-enables CLAP; stub its load() so we don't touch disk.
        monkeypatch.setattr(clap_index_module.ClapIndex, "load", _noop_load)

        plugin = await setup(mock_mass, _make_manifest(), _make_config(enable_text_search=True))
        assert isinstance(plugin, SonicSimilarityPlugin)
        await plugin.loaded_in_mass()

        mock_mass.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_warm_task_when_text_search_disabled(self, mock_mass: MagicMock) -> None:
        """text_search_enabled=False → no background warm scheduled."""
        plugin = await setup(mock_mass, _make_manifest(), _make_config(enable_text_search=False))
        await plugin.loaded_in_mass()

        mock_mass.create_task.assert_not_called()


def _make_tensor_chain(vector: np.ndarray) -> MagicMock:
    """Build a CLAP-tensor-like MagicMock whose .detach().cpu().numpy() yields vector."""
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


class TestSearch:
    """Tests for SonicSimilarityPlugin.search (the ProviderFeature.SEARCH dispatcher)."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_track_not_in_media_types(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """We only search tracks; non-track media_types return empty SearchResults."""
        plugin = make_plugin(clap_enabled=True)
        plugin._clap_index.__len__ = MagicMock(return_value=5)

        result = await plugin.search("disco", [MediaType.ALBUM, MediaType.ARTIST])

        assert list(result.tracks) == []
        # The encoder must not even be touched when no track type was requested.
        plugin._clap_index.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_clap_index(self, make_plugin: Callable[..., Any]) -> None:
        """Without a CLAP index attached the search short-circuits."""
        plugin = make_plugin()  # clap_enabled=False → _clap_index stays None

        result = await plugin.search("disco", [MediaType.TRACK])

        assert list(result.tracks) == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_clap_index_is_empty(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """An empty CLAP index (len == 0) short-circuits without encoding."""
        plugin = make_plugin(clap_enabled=True)  # default len == 0

        result = await plugin.search("disco", [MediaType.TRACK])

        assert list(result.tracks) == []
        plugin._clap_index.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_schedules_warm_when_encoder_cold(self, make_plugin: Callable[..., Any]) -> None:
        """
        A cold encoder short-circuits to empty but kicks off a one-time background warm.

        The encoder loads on first query rather than at startup, but the load runs off
        the request path (via mass.create_task) so the global SEARCH dispatcher never
        blocks on the ~500MB GPT2 download.
        """
        plugin = make_plugin(clap_enabled=True)
        plugin._clap_index.__len__ = MagicMock(return_value=5)
        # Sentinel: search() must not load the encoder synchronously on the request path.
        plugin._load_text_encoder = MagicMock(
            side_effect=RuntimeError("must not load synchronously")
        )

        result = await plugin.search("disco", [MediaType.TRACK])

        assert list(result.tracks) == []
        plugin._load_text_encoder.assert_not_called()
        plugin._clap_index.search.assert_not_called()
        plugin.mass.create_task.assert_called_once_with(
            plugin._get_text_encoder, task_id="sonic_similarity_text_encoder_warm"
        )

    @pytest.mark.asyncio
    async def test_happy_path_returns_resolved_tracks(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """Encoded query → CLAP matches → resolved Track objects in SearchResults.tracks."""
        plugin = make_plugin(clap_enabled=True)
        plugin._clap_index.__len__ = MagicMock(return_value=5)
        vector = np.full((1024,), 0.1, dtype=np.float32)
        plugin._text_encoder = _make_mock_encoder(vector)
        plugin._clap_index.search = AsyncMock(
            return_value=[
                ScoredCandidate("track_a", "spotify", 0.1),
                ScoredCandidate("track_b", "tidal", 0.2),
            ]
        )
        track_a = make_track("track_a", provider="spotify", name="A")
        track_b = make_track("track_b", provider="tidal", name="B")
        mock_mass.music.tracks.get.side_effect = [track_a, track_b]

        result = await plugin.search("disco", [MediaType.TRACK], limit=5)

        assert list(result.tracks) == [track_a, track_b]
        plugin._clap_index.search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_drops_resolves_that_raise(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """An unresolvable item is silently dropped; the rest pass through."""
        plugin = make_plugin(clap_enabled=True)
        plugin._clap_index.__len__ = MagicMock(return_value=5)
        vector = np.full((1024,), 0.1, dtype=np.float32)
        plugin._text_encoder = _make_mock_encoder(vector)
        plugin._clap_index.search = AsyncMock(
            return_value=[
                ScoredCandidate("track_a", "spotify", 0.1),
                ScoredCandidate("track_b", "tidal", 0.2),
            ]
        )
        track_b = make_track("track_b", provider="tidal", name="B")

        async def _fake_get(item_id: str, _provider: str) -> MagicMock:
            if item_id == "track_a":
                raise MusicAssistantError("nope")
            return track_b

        mock_mass.music.tracks.get.side_effect = _fake_get

        result = await plugin.search("disco", [MediaType.TRACK], limit=5)

        assert list(result.tracks) == [track_b]

    @pytest.mark.asyncio
    async def test_passes_limit_to_clap_index_search(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """The limit kwarg is forwarded as the k argument to CLAP index search."""
        plugin = make_plugin(clap_enabled=True)
        plugin._clap_index.__len__ = MagicMock(return_value=20)
        vector = np.full((1024,), 0.1, dtype=np.float32)
        plugin._text_encoder = _make_mock_encoder(vector)
        plugin._clap_index.search = AsyncMock(return_value=[])
        mock_mass.music.tracks.get = AsyncMock()

        await plugin.search("disco", [MediaType.TRACK], limit=7)

        # The CLAP index search is called positionally as (embedding, k).
        call = plugin._clap_index.search.await_args
        assert call.args[1] == 7
