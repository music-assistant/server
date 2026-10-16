"""Tests for the hourly periodic-refresh scheduled task."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.sonic_similarity import (
    SonicSimilarityPlugin,
    setup,
)
from music_assistant.providers.sonic_similarity import clap_index as clap_index_module
from music_assistant.providers.sonic_similarity.constants import (
    PERIODIC_REFRESH_INTERVAL_HOURS,
    PERIODIC_REFRESH_TASK_ID,
    SUPPORTED_FEATURES,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _make_manifest() -> MagicMock:
    """Build a ProviderManifest mock for setup()."""
    manifest = MagicMock()
    manifest.instance_id = "test-instance-id"
    manifest.domain = "sonic_similarity"
    return manifest


def _make_config() -> MagicMock:
    """Build a minimal ProviderConfig mock for setup()."""
    config = MagicMock()
    config_values = {
        "log_level": "GLOBAL",
        "enable_clap_index": False,
        "enable_text_search": False,
        "enable_discover_row": True,
        "discover_preset": "discover",
        "discover_diversity": 0.2,
    }
    config.get_value = lambda key: config_values.get(key)
    return config


class TestPeriodicRefresh:
    """_periodic_refresh skips when the row count is unchanged, rebuilds otherwise."""

    @pytest.mark.asyncio
    async def test_skips_rebuild_when_row_count_unchanged(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """No row count delta → no rebuild calls and counter stays put."""
        plugin = make_plugin(clap_enabled=True)
        plugin._last_seen_row_count = 42
        mock_mass.music.database.get_count_from_query = AsyncMock(return_value=42)
        plugin._rebuild_search_index = AsyncMock()
        plugin._rebuild_clap_index_from_database = AsyncMock()

        await plugin._periodic_refresh()

        plugin._rebuild_search_index.assert_not_awaited()
        plugin._rebuild_clap_index_from_database.assert_not_awaited()
        assert plugin._last_seen_row_count == 42

    @pytest.mark.asyncio
    async def test_skips_tick_while_a_rebuild_is_running(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """A tick that lands during a running build neither counts rows nor starts a second scan."""
        plugin = make_plugin(clap_enabled=True)
        plugin._last_seen_row_count = 0
        mock_mass.music.database.get_count_from_query = AsyncMock(return_value=42)
        plugin._rebuild_search_index = AsyncMock()
        plugin._rebuild_clap_index_from_database = AsyncMock()

        async with plugin._rebuild_lock:
            await plugin._periodic_refresh()

        mock_mass.music.database.get_count_from_query.assert_not_awaited()
        plugin._rebuild_search_index.assert_not_awaited()
        plugin._rebuild_clap_index_from_database.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_tick_during_the_startup_builds_row_count(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """A tick that lands while the startup build is still counting rows is skipped too."""
        plugin = make_plugin(clap_enabled=True)
        plugin._last_seen_row_count = 0
        release_count = asyncio.Event()

        async def _blocked_count(*_args: Any, **_kwargs: Any) -> int:
            await release_count.wait()
            return 5

        mock_mass.music.database.get_count_from_query = AsyncMock(side_effect=_blocked_count)
        plugin._rebuild_clap_index_from_database = AsyncMock()

        startup = asyncio.create_task(plugin._rebuild_indexes())
        await asyncio.sleep(0)  # the startup build is now parked in its row count
        await asyncio.wait_for(plugin._periodic_refresh(), timeout=1)

        assert mock_mass.music.database.get_count_from_query.await_count == 1
        release_count.set()
        await startup
        assert plugin._last_seen_row_count == 5
        plugin._rebuild_clap_index_from_database.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retries_a_failed_character_build_while_row_count_is_unchanged(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """Traits ok, Character fails: the next tick retries Character alone and clears the error."""
        plugin = make_plugin(clap_enabled=True)
        plugin._last_seen_row_count = 0
        mock_mass.music.database.get_count_from_query = AsyncMock(return_value=10)
        # Real _rebuild_search_index runs end-to-end (iter_merged is empty) and
        # advances _last_seen_row_count before Character fails.
        plugin._rebuild_clap_index_from_database = AsyncMock(
            side_effect=[RuntimeError("usearch crash"), None]
        )

        await plugin._periodic_refresh()

        assert plugin._last_seen_row_count == 10
        assert plugin._last_rebuild_error == {"Character": "usearch crash"}

        plugin._rebuild_search_index = AsyncMock()
        await plugin._periodic_refresh()

        plugin._rebuild_search_index.assert_not_awaited()
        assert plugin._rebuild_clap_index_from_database.await_count == 2
        assert plugin._last_rebuild_error == {}

    @pytest.mark.asyncio
    async def test_rebuilds_when_row_count_increased(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """Row count grew → both indexes rebuild; the real _rebuild_search_index bumps the counter."""
        plugin = make_plugin(clap_enabled=True)
        plugin._last_seen_row_count = 10
        mock_mass.music.database.get_count_from_query = AsyncMock(return_value=15)
        # Real _rebuild_search_index runs end-to-end (iter_merged is empty) and
        # bumps _last_seen_row_count via its own pre-count snapshot.
        plugin._rebuild_clap_index_from_database = AsyncMock()

        await plugin._periodic_refresh()

        plugin._rebuild_clap_index_from_database.assert_awaited_once()
        assert plugin._last_seen_row_count == 15

    @pytest.mark.asyncio
    async def test_rebuilds_when_row_count_decreased(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """Defensive: a row-count drop also triggers rebuild (handles future deletes)."""
        plugin = make_plugin(clap_enabled=True)
        plugin._last_seen_row_count = 50
        mock_mass.music.database.get_count_from_query = AsyncMock(return_value=40)
        plugin._rebuild_clap_index_from_database = AsyncMock()

        await plugin._periodic_refresh()

        plugin._rebuild_clap_index_from_database.assert_awaited_once()
        assert plugin._last_seen_row_count == 40

    @pytest.mark.asyncio
    async def test_counter_not_advanced_when_rebuild_fails(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """A failing 18-dim rebuild leaves the counter untouched so the next tick retries."""
        plugin = make_plugin(clap_enabled=True)
        plugin._last_seen_row_count = 10
        mock_mass.music.database.get_count_from_query = AsyncMock(return_value=15)
        # _rebuild_search_index raises → _safe_rebuild swallows into _last_rebuild_error
        # → counter is never advanced because the pre-count bump line is unreached.
        plugin._rebuild_search_index = AsyncMock(side_effect=RuntimeError("disk full"))
        plugin._rebuild_clap_index_from_database = AsyncMock()

        await plugin._periodic_refresh()

        assert plugin._last_seen_row_count == 10
        assert plugin._last_rebuild_error.get("Traits") == "disk full"

    @pytest.mark.asyncio
    async def test_count_query_failure_skips_tick(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """If the count query itself fails, skip the tick — no rebuild attempted."""
        plugin = make_plugin(clap_enabled=True)
        plugin._last_seen_row_count = 10
        mock_mass.music.database.get_count_from_query = AsyncMock(side_effect=RuntimeError("oops"))
        plugin._rebuild_search_index = AsyncMock()
        plugin._rebuild_clap_index_from_database = AsyncMock()

        await plugin._periodic_refresh()

        plugin._rebuild_search_index.assert_not_awaited()
        plugin._rebuild_clap_index_from_database.assert_not_awaited()
        assert plugin._last_seen_row_count == 10

    @pytest.mark.asyncio
    async def test_clap_skipped_when_index_disabled(
        self, make_plugin: Callable[..., Any], mock_mass: MagicMock
    ) -> None:
        """When CLAP isn't configured, only 18-dim is rebuilt."""
        plugin = make_plugin()  # clap_enabled=False → _clap_index stays None
        plugin._last_seen_row_count = 0
        mock_mass.music.database.get_count_from_query = AsyncMock(return_value=5)
        plugin._rebuild_clap_index_from_database = AsyncMock()

        await plugin._periodic_refresh()

        plugin._rebuild_clap_index_from_database.assert_not_awaited()


class TestPeriodicRefreshLifecycle:
    """loaded_in_mass registers the scheduled task; unload unregisters it."""

    @pytest.mark.asyncio
    async def test_loaded_in_mass_registers_scheduled_task(
        self, mock_mass: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """loaded_in_mass wires the periodic refresh into mass.tasks with the expected schedule."""

        async def _noop_load(_self: Any) -> None:
            return None

        # Defensive: load() is a no-op even though clap is disabled in this config.
        monkeypatch.setattr(clap_index_module.ClapIndex, "load", _noop_load)

        plugin = await setup(mock_mass, _make_manifest(), _make_config())
        assert isinstance(plugin, SonicSimilarityPlugin)
        await plugin.loaded_in_mass()

        mock_mass.tasks.register_scheduled_task.assert_called_once()
        call_kwargs = mock_mass.tasks.register_scheduled_task.call_args.kwargs
        assert call_kwargs["task_id"] == PERIODIC_REFRESH_TASK_ID
        assert call_kwargs["handler"] == plugin._periodic_refresh
        assert call_kwargs["schedule"].every == PERIODIC_REFRESH_INTERVAL_HOURS

    @pytest.mark.asyncio
    async def test_unload_unregisters_scheduled_task(self, mock_mass: MagicMock) -> None:
        """unload() detaches the periodic-refresh task from mass.tasks."""
        manifest = MagicMock()
        manifest.instance_id = "iid"
        manifest.domain = "sonic_similarity"
        plugin = SonicSimilarityPlugin(mock_mass, manifest, _make_config(), SUPPORTED_FEATURES)

        await plugin.unload()

        mock_mass.tasks.unregister_scheduled_task.assert_called_once_with(PERIODIC_REFRESH_TASK_ID)
