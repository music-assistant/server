"""Unit tests for plugin setup-time behavior, status text, and action dispatch."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import SetupFailedError

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

from music_assistant.providers.sonic_similarity import (
    ACTION_REBUILD_18DIM,
    ACTION_REBUILD_CLAP,
    SUPPORTED_FEATURES,
    SonicSimilarityPlugin,
    _collect_status_text,
    _safe_aa_domain,
    get_config_entries,
)


class TestSafeAaDomain:
    """Tests for the _safe_aa_domain validator helper."""

    @pytest.mark.parametrize(
        "value",
        ["sonic_analysis", "spotify", "lastfm_recommendations", "foo123"],
    )
    def test_accepts_canonical_domain_strings(
        self, value: str, logger: logging.Logger, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Valid alphanumeric+underscore domains pass through unchanged and silently."""
        caplog.set_level(logging.WARNING, logger=logger.name)
        assert _safe_aa_domain(value, logger) == value
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    @pytest.mark.parametrize("value", [None, ""])
    def test_falls_back_to_default_when_none_or_empty(
        self,
        value: object,
        logger: logging.Logger,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """None and empty-string fall back to 'sonic_analysis' without warning."""
        caplog.set_level(logging.WARNING, logger=logger.name)
        assert _safe_aa_domain(value, logger) == "sonic_analysis"
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    @pytest.mark.parametrize("value", ["../etc/passwd", "_/../../sensitive", "foo/bar"])
    def test_path_traversal_strings_rejected_and_warn(
        self,
        value: str,
        logger: logging.Logger,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Strings containing slashes or '..' fall back to default and emit a warning."""
        caplog.set_level(logging.WARNING, logger=logger.name)
        assert _safe_aa_domain(value, logger) == "sonic_analysis"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    @pytest.mark.parametrize("value", ["foo bar", "foo.bar", "foo-bar"])
    def test_other_invalid_chars_rejected(
        self,
        value: str,
        logger: logging.Logger,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Spaces, periods, and hyphens are rejected by the strict pattern."""
        caplog.set_level(logging.WARNING, logger=logger.name)
        assert _safe_aa_domain(value, logger) == "sonic_analysis"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_whitespace_padded_values_are_stripped_before_validation(
        self, logger: logging.Logger, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Leading/trailing whitespace is stripped, then the inner value matches."""
        caplog.set_level(logging.WARNING, logger=logger.name)
        assert _safe_aa_domain("  spotify  ", logger) == "spotify"
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]


class TestCollectStatusText:
    """Tests for the _collect_status_text helper used by the plugin's status rows."""

    NOT_LOADED = (
        "18-dim engine: not yet loaded",
        "CLAP engine: disabled",
        "Text encoder: disabled",
    )

    @pytest.mark.asyncio
    async def test_returns_not_loaded_triple_when_instance_id_is_none(
        self, mock_mass: MagicMock
    ) -> None:
        """A None instance_id short-circuits to the not-yet-loaded triple."""
        assert await _collect_status_text(mock_mass, None) == self.NOT_LOADED

    @pytest.mark.asyncio
    async def test_returns_not_loaded_triple_when_get_provider_returns_non_plugin(
        self, mock_mass: MagicMock
    ) -> None:
        """A non-SonicSimilarityPlugin provider also returns the disabled triple."""
        mock_mass.get_provider.return_value = MagicMock()
        assert await _collect_status_text(mock_mass, "iid") == self.NOT_LOADED

    @pytest.mark.asyncio
    async def test_returns_populated_18dim_status_when_provider_is_loaded(
        self, mock_mass: MagicMock, make_plugin: Callable[..., Any]
    ) -> None:
        """Loaded plugin with a primed corpus yields a populated 18-dim status line."""
        plugin = make_plugin(
            signatures={
                ("spotify", "a"): [0.1] * 18,
                ("spotify", "b"): [0.2] * 18,
            }
        )
        mock_mass.get_provider.return_value = plugin

        eighteen, _clap, _text = await _collect_status_text(mock_mass, "iid")

        assert "2 tracks indexed" in eighteen
        assert ("corpus stats ready" in eighteen) or ("2 signatures cached" in eighteen)

    @pytest.mark.asyncio
    async def test_clap_engine_disabled_when_clap_index_is_none(
        self, mock_mass: MagicMock, make_plugin: Callable[..., Any]
    ) -> None:
        """Without CLAP enabled the clap status string is the disabled sentinel."""
        plugin = make_plugin(signatures={("spotify", "a"): [0.1] * 18})
        mock_mass.get_provider.return_value = plugin

        _eighteen, clap, _text = await _collect_status_text(mock_mass, "iid")

        assert clap == "CLAP engine: disabled"

    @pytest.mark.asyncio
    async def test_clap_engine_status_reports_size_when_enabled(
        self, mock_mass: MagicMock, make_plugin: Callable[..., Any]
    ) -> None:
        """When CLAP is enabled the clap line reports the index size."""
        plugin = make_plugin(
            clap_enabled=True,
            signatures={("spotify", "a"): [0.1] * 18},
        )
        plugin._clap_index.__len__ = MagicMock(return_value=42)
        mock_mass.get_provider.return_value = plugin

        _eighteen, clap, _text = await _collect_status_text(mock_mass, "iid")

        assert "42 embeddings indexed" in clap

    @pytest.mark.asyncio
    async def test_text_encoder_cold_message_when_enabled_and_encoder_none(
        self, mock_mass: MagicMock, make_plugin: Callable[..., Any]
    ) -> None:
        """Text-search-enabled with no encoder loaded reports a cold-state message."""
        plugin = make_plugin(
            text_search_enabled=True,
            signatures={("spotify", "a"): [0.1] * 18},
        )
        mock_mass.get_provider.return_value = plugin

        _eighteen, _clap, text = await _collect_status_text(mock_mass, "iid")

        lowered = text.lower()
        assert ("cold" in lowered) or ("downloads on first query" in lowered)

    @pytest.mark.asyncio
    async def test_coverage_pct_included_when_get_coverage_returns_counts(
        self, mock_mass: MagicMock, make_plugin: Callable[..., Any]
    ) -> None:
        """A real coverage object produces a percentage substring in the 18-dim line."""
        coverage = SimpleNamespace(analyzed=80, pending=20, stale_version=0, analysis_version=1)
        mock_mass.streams.audio_analysis.get_coverage = AsyncMock(return_value=coverage)
        plugin = make_plugin(signatures={("spotify", "a"): [0.1] * 18})
        mock_mass.get_provider.return_value = plugin

        eighteen, _clap, _text = await _collect_status_text(mock_mass, "iid")

        assert "80.0%" in eighteen


class TestConfigEntriesActions:
    """Tests for the action-dispatch branch of get_config_entries."""

    @pytest.mark.asyncio
    async def test_action_rebuild_18dim_dispatches_to_provider(
        self, mock_mass: MagicMock, make_plugin: Callable[..., Any]
    ) -> None:
        """The 18-dim rebuild action fires create_task once and still returns entries."""
        plugin = make_plugin(signatures={("spotify", "a"): [0.1] * 18})
        plugin._rebuild_search_index = AsyncMock()
        mock_mass.get_provider.return_value = plugin

        entries = await get_config_entries(
            mock_mass, instance_id="iid", action=ACTION_REBUILD_18DIM
        )

        assert mock_mass.create_task.call_count == 1
        assert entries

    @pytest.mark.asyncio
    async def test_action_rebuild_clap_dispatches_when_clap_enabled(
        self, mock_mass: MagicMock, make_plugin: Callable[..., Any]
    ) -> None:
        """The CLAP rebuild action fires create_task when _clap_index is present."""
        plugin = make_plugin(
            clap_enabled=True,
            signatures={("spotify", "a"): [0.1] * 18},
        )
        plugin._rebuild_clap_index_from_database = AsyncMock()
        mock_mass.get_provider.return_value = plugin

        await get_config_entries(mock_mass, instance_id="iid", action=ACTION_REBUILD_CLAP)

        assert mock_mass.create_task.call_count == 1

    @pytest.mark.asyncio
    async def test_action_rebuild_clap_noops_when_clap_disabled(
        self, mock_mass: MagicMock, make_plugin: Callable[..., Any]
    ) -> None:
        """The CLAP rebuild action is a no-op when the index isn't built."""
        plugin = make_plugin(signatures={("spotify", "a"): [0.1] * 18})
        mock_mass.get_provider.return_value = plugin

        await get_config_entries(mock_mass, instance_id="iid", action=ACTION_REBUILD_CLAP)

        assert mock_mass.create_task.call_count == 0

    @pytest.mark.asyncio
    async def test_action_without_instance_id_noops(self, mock_mass: MagicMock) -> None:
        """Without an instance_id the action branch is skipped entirely."""
        await get_config_entries(mock_mass, instance_id=None, action=ACTION_REBUILD_18DIM)

        assert mock_mass.create_task.call_count == 0

    @pytest.mark.asyncio
    async def test_action_when_get_provider_returns_wrong_type_noops(
        self, mock_mass: MagicMock
    ) -> None:
        """A non-plugin provider returned from get_provider skips the dispatch."""
        mock_mass.get_provider.return_value = MagicMock()

        await get_config_entries(mock_mass, instance_id="iid", action=ACTION_REBUILD_18DIM)

        assert mock_mass.create_task.call_count == 0


def _build_plugin_for_init(mock_mass: MagicMock) -> Any:
    """Construct a plugin without going through handle_async_init / loaded_in_mass.

    Returned as ``Any`` to match the project's existing test convention for
    plugin instances whose private methods get mock-swapped.
    """
    manifest = MagicMock()
    manifest.instance_id = "iid"
    manifest.domain = "sonic_similarity"
    config = MagicMock()
    config_values = {"log_level": "GLOBAL", "aa_provider_domain": "sonic_analysis"}
    config.get_value = lambda key: config_values.get(key)
    return SonicSimilarityPlugin(mock_mass, manifest, config, SUPPORTED_FEATURES)


class TestHandleAsyncInit:
    """handle_async_init surfaces rebuild failures as SetupFailedError so MA's loader sees them."""

    @pytest.mark.asyncio
    async def test_raises_setup_failed_when_rebuild_raises(self, mock_mass: MagicMock) -> None:
        """A rebuild failure during initial setup must surface as SetupFailedError."""
        plugin = _build_plugin_for_init(mock_mass)
        plugin._rebuild_search_index = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(SetupFailedError, match="18-dim search index"):
            await plugin.handle_async_init()

    @pytest.mark.asyncio
    async def test_succeeds_when_rebuild_succeeds(self, mock_mass: MagicMock) -> None:
        """The happy path: rebuild runs once, no exception escapes."""
        plugin = _build_plugin_for_init(mock_mass)
        plugin._rebuild_search_index = AsyncMock()

        await plugin.handle_async_init()

        plugin._rebuild_search_index.assert_awaited_once()
