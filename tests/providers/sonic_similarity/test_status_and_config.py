"""Unit tests for plugin setup-time behavior, status text, and action dispatch."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ConfigEntryType, EventType
from music_assistant_models.errors import ActionUnavailable

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Any

from music_assistant.providers.sonic_similarity import SonicSimilarityPlugin
from music_assistant.providers.sonic_similarity import clap_index as clap_index_module
from music_assistant.providers.sonic_similarity import provider as provider_module
from music_assistant.providers.sonic_similarity.constants import (
    ACTION_REBUILD_18DIM,
    ACTION_REBUILD_CLAP,
    CONF_LABEL_STATUS_18DIM,
    REBUILD_TASK_ID,
    SUPPORTED_FEATURES,
)


class TestCollectStatusText:
    """Tests for the _collect_status_text helper used by the plugin's status rows."""

    @pytest.mark.asyncio
    async def test_reports_pending_state_when_index_not_built(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """A loaded plugin with no indexed corpus reports empty counts and disabled engines."""
        plugin = make_plugin()  # no signatures → empty corpus, no search index

        eighteen, clap, text = plugin._collect_status_text()

        assert "0 tracks indexed" in eighteen
        assert "corpus stats pending" in eighteen
        assert clap == "Character engine: disabled"
        assert text == "Text encoder: disabled"

    @pytest.mark.asyncio
    async def test_returns_populated_18dim_status_when_provider_is_loaded(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """Loaded plugin with a primed corpus yields a populated 18-dim status line."""
        plugin = make_plugin(
            signatures={
                ("spotify", "a"): [0.1] * 18,
                ("spotify", "b"): [0.2] * 18,
            }
        )

        eighteen, _clap, _text = plugin._collect_status_text()

        assert "2 tracks indexed" in eighteen
        assert ("corpus stats ready" in eighteen) or ("2 signatures cached" in eighteen)

    @pytest.mark.asyncio
    async def test_clap_engine_disabled_when_clap_index_is_none(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """Without CLAP enabled the clap status string is the disabled sentinel."""
        plugin = make_plugin(signatures={("spotify", "a"): [0.1] * 18})

        _eighteen, clap, _text = plugin._collect_status_text()

        assert clap == "Character engine: disabled"

    @pytest.mark.asyncio
    async def test_clap_engine_status_reports_size_when_enabled(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """When CLAP is enabled the clap line reports the index size."""
        plugin = make_plugin(
            clap_enabled=True,
            signatures={("spotify", "a"): [0.1] * 18},
        )
        plugin._clap_index.__len__ = MagicMock(return_value=42)

        _eighteen, clap, _text = plugin._collect_status_text()

        assert "42 embeddings indexed" in clap

    @pytest.mark.asyncio
    async def test_text_encoder_cold_message_when_enabled_and_encoder_none(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """Text-search-enabled with no encoder loaded reports a cold-state message."""
        plugin = make_plugin(
            text_search_enabled=True,
            signatures={("spotify", "a"): [0.1] * 18},
        )

        _eighteen, _clap, text = plugin._collect_status_text()

        lowered = text.lower()
        assert ("cold" in lowered) or ("downloads on first query" in lowered)


class TestGetConfigEntries:
    """get_config_entries runs on the provider load path, so it reads in-memory state only."""

    @pytest.mark.asyncio
    async def test_status_labels_come_from_memory_without_database_access(
        self, mock_mass: MagicMock, make_plugin: Callable[..., Any]
    ) -> None:
        """The status rows render from the index state; no analysis controller call is made."""
        plugin = make_plugin(signatures={("spotify", "a"): [0.1] * 18})

        entries = await plugin.get_config_entries()

        assert mock_mass.streams.audio_analysis.mock_calls == []
        assert mock_mass.music.mock_calls == []
        status_entry = next(e for e in entries if e.key == CONF_LABEL_STATUS_18DIM)
        assert status_entry.type == ConfigEntryType.LABEL
        assert "1 tracks indexed" in str(status_entry.label)


class TestConfigEntriesActions:
    """Tests for handle_config_action (the one-shot rebuild buttons)."""

    @pytest.mark.asyncio
    async def test_action_rebuild_18dim_dispatches_to_provider(
        self, mock_mass: MagicMock, make_plugin: Callable[..., Any]
    ) -> None:
        """The 18-dim rebuild action fires create_task once and returns None."""
        plugin = make_plugin(signatures={("spotify", "a"): [0.1] * 18})
        plugin._rebuild_search_index = AsyncMock()

        result = await plugin.handle_config_action(ACTION_REBUILD_18DIM)

        assert mock_mass.create_task.call_count == 1
        assert result is None

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

        await plugin.handle_config_action(ACTION_REBUILD_CLAP)

        assert mock_mass.create_task.call_count == 1

    @pytest.mark.asyncio
    async def test_action_rebuild_clap_reports_failure_when_index_missing(
        self, mock_mass: MagicMock, make_plugin: Callable[..., Any]
    ) -> None:
        """Without a CLAP index the rebuild action reports failure instead of silently passing."""
        plugin = make_plugin(signatures={("spotify", "a"): [0.1] * 18})

        with pytest.raises(ActionUnavailable) as exc_info:
            await plugin.handle_config_action(ACTION_REBUILD_CLAP)

        assert exc_info.value.translation_key == "clap_index_unavailable"
        assert exc_info.value.translation_owner == "provider.sonic_similarity"
        assert mock_mass.create_task.call_count == 0


def _build_plugin_for_init(mock_mass: MagicMock) -> Any:
    """
    Construct a plugin without going through handle_async_init / loaded_in_mass.

    Returned as ``Any`` to match the project's existing test convention for
    plugin instances whose private methods get mock-swapped.
    """
    manifest = MagicMock()
    manifest.instance_id = "iid"
    manifest.domain = "sonic_similarity"
    config = MagicMock()
    config_values = {"log_level": "GLOBAL"}
    config.get_value = lambda key: config_values.get(key)
    return SonicSimilarityPlugin(mock_mass, manifest, config, SUPPORTED_FEATURES)


class TestDeferredIndexRebuild:
    """The index builds run as a background task started once the provider is registered."""

    @pytest.mark.asyncio
    async def test_loaded_in_mass_schedules_the_rebuild_instead_of_awaiting_it(
        self, mock_mass: MagicMock
    ) -> None:
        """loaded_in_mass returns with the indexes still empty and the rebuild task scheduled."""
        plugin = _build_plugin_for_loaded(mock_mass, clap_enabled=False)
        plugin._rebuild_search_index = AsyncMock()

        await plugin.loaded_in_mass()

        plugin._rebuild_search_index.assert_not_awaited()
        mock_mass.create_task.assert_called_once()
        assert mock_mass.create_task.call_args.kwargs["task_id"] == REBUILD_TASK_ID
        status = await plugin._handle_status()
        assert status["index_size"] == 0
        assert status["has_corpus_stats"] is False

    @pytest.mark.asyncio
    async def test_rebuild_indexes_runs_traits_then_character(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """With the Character index enabled the two rebuilds run one after the other."""
        plugin = make_plugin(clap_enabled=True)
        order: list[str] = []

        async def _traits() -> None:
            order.append("Traits")

        async def _character() -> None:
            order.append("Character")

        plugin._rebuild_search_index = _traits
        plugin._rebuild_clap_index_from_database = _character

        await plugin._rebuild_indexes()

        assert order == ["Traits", "Character"]

    @pytest.mark.asyncio
    async def test_rebuild_indexes_skips_character_when_disabled(
        self, make_plugin: Callable[..., Any]
    ) -> None:
        """Without a Character index only the Traits rebuild runs."""
        plugin = make_plugin()
        plugin._rebuild_search_index = AsyncMock()
        plugin._rebuild_clap_index_from_database = AsyncMock()

        await plugin._rebuild_indexes()

        plugin._rebuild_search_index.assert_awaited_once()
        plugin._rebuild_clap_index_from_database.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_rebuild_is_reported_by_status_and_label(
        self, mock_mass: MagicMock
    ) -> None:
        """A failing background rebuild lands in the status command and the status label."""
        plugin = _build_plugin_for_init(mock_mass)
        plugin._rebuild_search_index = AsyncMock(side_effect=RuntimeError("boom"))

        await plugin._rebuild_indexes()

        status = await plugin._handle_status()
        assert status["last_rebuild_error"] == {"Traits": "boom"}
        assert status["rebuild_in_progress"] is False
        eighteen, _clap, _text = plugin._collect_status_text()
        assert "last rebuild failed: boom" in eighteen

    @pytest.mark.asyncio
    async def test_unload_cancels_the_rebuild_before_closing_the_character_index(
        self, mock_mass: MagicMock, make_plugin: Callable[..., Any]
    ) -> None:
        """unload() stops a running rebuild first, so it cannot resume into a closed index."""
        plugin = make_plugin(clap_enabled=True)
        plugin._clap_index.close = AsyncMock()
        order = MagicMock()
        order.attach_mock(mock_mass.cancel_task, "cancel_task")
        order.attach_mock(plugin._clap_index.close, "close")

        await plugin.unload()

        assert [name for name, _args, _kwargs in order.mock_calls] == ["cancel_task", "close"]
        mock_mass.cancel_task.assert_called_once_with(REBUILD_TASK_ID)


def _build_plugin_for_loaded(mock_mass: MagicMock, *, clap_enabled: bool) -> Any:
    """Construct a plugin configured to attempt CLAP setup in loaded_in_mass."""
    manifest = MagicMock()
    manifest.instance_id = "iid"
    manifest.domain = "sonic_similarity"
    config = MagicMock()
    config_values = {
        "log_level": "GLOBAL",
        "enable_clap_index": clap_enabled,
        "enable_text_search": False,
    }
    config.get_value = lambda key: config_values.get(key)
    return SonicSimilarityPlugin(mock_mass, manifest, config, SUPPORTED_FEATURES)


class TestLoadedInMassClapResilience:
    """loaded_in_mass must keep the 18-dim engine alive when optional CLAP setup fails."""

    @pytest.mark.asyncio
    async def test_clap_setup_failure_leaves_plugin_loaded(
        self, mock_mass: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ClapIndex.load() failure is swallowed; _clap_index ends up None."""

        async def _boom(_self: Any) -> None:
            raise RuntimeError("usearch missing")

        monkeypatch.setattr(clap_index_module.ClapIndex, "load", _boom)

        plugin = _build_plugin_for_loaded(mock_mass, clap_enabled=True)
        await plugin.loaded_in_mass()

        assert plugin._clap_index is None


class TestSafeRebuild:
    """_safe_rebuild swallows background-task failures into _last_rebuild_error."""

    @pytest.mark.asyncio
    async def test_failure_populates_last_rebuild_error(self, mock_mass: MagicMock) -> None:
        """A raising rebuild fn is caught; the error message lands in the dict."""
        plugin = _build_plugin_for_init(mock_mass)

        async def _boom() -> None:
            raise RuntimeError("disk full")

        await plugin._safe_rebuild("Traits", _boom)

        assert plugin._last_rebuild_error == {"Traits": "disk full"}

    @pytest.mark.asyncio
    async def test_success_clears_prior_error(self, mock_mass: MagicMock) -> None:
        """A subsequent successful rebuild removes the stale error entry."""
        plugin = _build_plugin_for_init(mock_mass)
        plugin._last_rebuild_error["Traits"] = "earlier failure"

        async def _ok() -> None:
            return None

        await plugin._safe_rebuild("Traits", _ok)

        assert "Traits" not in plugin._last_rebuild_error

    @pytest.mark.asyncio
    async def test_errors_are_per_label(self, mock_mass: MagicMock) -> None:
        """A CLAP failure does not clobber an unrelated 18-dim error entry."""
        plugin = _build_plugin_for_init(mock_mass)
        plugin._last_rebuild_error["Traits"] = "existing 18-dim error"

        async def _boom() -> None:
            raise RuntimeError("clap broke")

        await plugin._safe_rebuild("Character", _boom)

        assert plugin._last_rebuild_error == {
            "Traits": "existing 18-dim error",
            "Character": "clap broke",
        }

    @pytest.mark.asyncio
    async def test_success_signals_providers_updated(self, mock_mass: MagicMock) -> None:
        """A finished rebuild signals PROVIDERS_UPDATED so the status labels re-render."""
        plugin = _build_plugin_for_init(mock_mass)

        async def _ok() -> None:
            return None

        await plugin._safe_rebuild("Traits", _ok)

        mock_mass.signal_event.assert_called_once_with(
            EventType.PROVIDERS_UPDATED, data=mock_mass.providers
        )

    @pytest.mark.asyncio
    async def test_failure_signals_providers_updated(self, mock_mass: MagicMock) -> None:
        """A failed rebuild still signals, so the recorded error reaches the status labels."""
        plugin = _build_plugin_for_init(mock_mass)

        async def _boom() -> None:
            raise RuntimeError("disk full")

        await plugin._safe_rebuild("Traits", _boom)

        mock_mass.signal_event.assert_called_once_with(
            EventType.PROVIDERS_UPDATED, data=mock_mass.providers
        )


class TestStatusTextRebuildErrors:
    """_collect_status_text surfaces _last_rebuild_error entries on the matching engine line."""

    @pytest.mark.asyncio
    async def test_18dim_error_appears_on_18dim_line(self, mock_mass: MagicMock) -> None:
        """A 18-dim rebuild error is appended to the 18-dim status line."""
        plugin = _build_plugin_for_init(mock_mass)
        plugin._last_rebuild_error["Traits"] = "disk full"

        eighteen, _clap, _text = plugin._collect_status_text()

        assert "last rebuild failed: disk full" in eighteen

    @pytest.mark.asyncio
    async def test_clap_error_appears_on_clap_line(self, mock_mass: MagicMock) -> None:
        """A CLAP rebuild error is appended to the CLAP status line."""
        plugin = _build_plugin_for_init(mock_mass)
        plugin._clap_index = MagicMock()
        plugin._clap_index.__len__ = MagicMock(return_value=42)
        plugin._last_rebuild_error["Character"] = "usearch native crash"

        _eighteen, clap, _text = plugin._collect_status_text()

        assert "last rebuild failed: usearch native crash" in clap


def _make_analysis_data() -> Any:
    """Return an AudioAnalysisData with every assemble_vector-required field set."""
    from music_assistant.models.audio_analysis import AudioAnalysisData  # noqa: PLC0415

    return AudioAnalysisData(
        bpm=120.0,
        energy=0.5,
        danceability=0.6,
        loudness_integrated=-9.0,
        loudness_range=4.0,
        brightness=0.7,
        harmonic_complexity=0.3,
        roughness=0.2,
        rhythmic_regularity=0.8,
        key="C",
        mode="major",
    )


class TestRebuildSearchIndexLocked:
    """The 18-dim rebuild path: corpus stats, atomic swap, versioned file writes."""

    @pytest.mark.asyncio
    async def test_empty_iter_preserves_prior_state(self, mock_mass: MagicMock) -> None:
        """No rows from the controller → early return, prior state untouched."""
        plugin = _build_plugin_for_init(mock_mass)
        plugin._signature_cache = {("spotify", "old"): [0.5] * 18}
        plugin.corpus_means = [0.0] * 18
        plugin.corpus_stds = [1.0] * 18
        mock_mass._iter_merged_audio_analysis_rows_data = []

        await plugin._rebuild_search_index_locked()

        # Prior state preserved (no swap happened).
        assert plugin._signature_cache == {("spotify", "old"): [0.5] * 18}
        assert plugin.corpus_means == [0.0] * 18

    @pytest.mark.asyncio
    async def test_rows_present_but_unassemblable_preserves_prior_state(
        self, mock_mass: MagicMock
    ) -> None:
        """Rows without the required scalar fields are skipped; index isn't rebuilt."""
        plugin = _build_plugin_for_init(mock_mass)
        plugin._signature_cache = {("spotify", "old"): [0.5] * 18}
        plugin.corpus_means = [0.0] * 18

        from music_assistant.models.audio_analysis import AudioAnalysisData  # noqa: PLC0415

        unassemblable = AudioAnalysisData()  # all fields None
        mock_mass._iter_merged_audio_analysis_rows_data = [
            ("track_a", "spotify", unassemblable),
        ]

        await plugin._rebuild_search_index_locked()

        # Prior state preserved — assemble_vector returned None for every row.
        assert plugin._signature_cache == {("spotify", "old"): [0.5] * 18}

    @pytest.mark.asyncio
    async def test_unload_stops_a_running_build_before_removing_the_index_files(
        self, mock_mass: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Removing the plugin mid-build cancels the task and waits for the worker to stop."""
        plugin = _build_plugin_for_init(mock_mass)
        mock_mass.storage_path = str(tmp_path)
        mock_mass._iter_merged_audio_analysis_rows_data = [
            ("track_a", "spotify", _make_analysis_data()),
            ("track_b", "tidal", _make_analysis_data()),
        ]
        in_worker = threading.Event()
        release_worker = threading.Event()
        builder = MagicMock()

        def _blocking_add(*_args: Any) -> None:
            in_worker.set()
            release_worker.wait(timeout=5)

        builder.add = MagicMock(side_effect=_blocking_add)
        monkeypatch.setattr(plugin, "_make_empty_index", lambda: builder)
        save_index = MagicMock()
        monkeypatch.setattr(provider_module, "save_index", save_index)

        build = asyncio.ensure_future(plugin._rebuild_search_index_locked())
        # the tracked rebuild task is cancelled by mass.cancel_task; the worker thread is not
        mock_mass.cancel_task.side_effect = lambda _task_id: build.cancel()
        await asyncio.get_running_loop().run_in_executor(None, in_worker.wait)
        unload = asyncio.ensure_future(plugin.unload(is_removed=True))
        await asyncio.sleep(0)  # unload is now waiting on the worker
        assert not unload.done()
        release_worker.set()
        await unload
        with pytest.raises(asyncio.CancelledError):
            await build

        mock_mass.cancel_task.assert_called_once_with(REBUILD_TASK_ID)
        assert builder.add.call_count == 1
        save_index.assert_not_called()
        assert plugin._search_index is None
        assert plugin.corpus_means is None
        assert list(tmp_path.glob("*.usearch")) == []

    @pytest.mark.asyncio
    async def test_happy_path_populates_state_and_writes_file(
        self, mock_mass: MagicMock, tmp_path: Path
    ) -> None:
        """Valid rows produce a populated signature cache, corpus stats, and an on-disk index file."""
        plugin = _build_plugin_for_init(mock_mass)
        mock_mass.storage_path = str(tmp_path)
        mock_mass._iter_merged_audio_analysis_rows_data = [
            ("track_a", "spotify", _make_analysis_data()),
            ("track_b", "tidal", _make_analysis_data()),
        ]

        await plugin._rebuild_search_index_locked()

        assert ("track_a", "spotify") in plugin._signature_cache
        assert ("track_b", "tidal") in plugin._signature_cache
        assert plugin.corpus_means is not None
        assert len(plugin.corpus_means) == 18
        assert plugin.corpus_stds is not None
        assert len(plugin.corpus_stds) == 18
        assert len(plugin._search_index) == 2
        # Versioned file matches the domain-aware template.
        files = list(tmp_path.glob("sonic_signatures_sonic_analysis_*.usearch"))
        assert len(files) == 1

    @pytest.mark.asyncio
    async def test_rebuild_cleans_up_prior_versioned_file(
        self, mock_mass: MagicMock, tmp_path: Path
    ) -> None:
        """A successful rebuild unlinks the previous versioned file for the same domain."""
        plugin = _build_plugin_for_init(mock_mass)
        mock_mass.storage_path = str(tmp_path)
        mock_mass._iter_merged_audio_analysis_rows_data = [
            ("track_a", "spotify", _make_analysis_data()),
        ]

        await plugin._rebuild_search_index_locked()
        first_files = list(tmp_path.glob("sonic_signatures_sonic_analysis_*.usearch"))
        assert len(first_files) == 1

        # Sleep one ms so the next rebuild's version timestamp differs.
        await asyncio.sleep(0.002)

        await plugin._rebuild_search_index_locked()
        after_files = list(tmp_path.glob("sonic_signatures_sonic_analysis_*.usearch"))
        assert len(after_files) == 1
        # The new file replaces the old one (different timestamp).
        assert after_files[0] != first_files[0]
