"""Tests for the ListenBrainz provider lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_USERNAME
from music_assistant.providers.listenbrainz import ListenBrainzProvider
from music_assistant.providers.listenbrainz.api_client import ListenBrainzAPIClient
from music_assistant.providers.listenbrainz.playlists import ListenBrainzPlaylistManager

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant


def _provider() -> ListenBrainzProvider:
    """Return a minimally initialized provider."""
    provider = ListenBrainzProvider.__new__(ListenBrainzProvider)
    provider.manifest = cast("ProviderManifest", MagicMock(domain="listenbrainz"))
    provider.config = cast(
        "ProviderConfig",
        MagicMock(instance_id="listenbrainz--test", name=None),
    )
    provider.mass = cast("MusicAssistant", MagicMock())
    cast("MagicMock", provider.mass.cache).get = AsyncMock(return_value=None)
    return provider


def test_provider_defers_playlist_manager_until_async_initialization() -> None:
    """The playlist manager is created after the provider dependencies are initialized."""
    mass = cast("MusicAssistant", MagicMock())
    manifest = cast("ProviderManifest", MagicMock(domain="listenbrainz"))
    config = cast(
        "ProviderConfig",
        MagicMock(instance_id="listenbrainz--test", name=None),
    )
    cast("MagicMock", config.get_value).return_value = None

    provider = ListenBrainzProvider(mass, manifest, config)

    assert not hasattr(provider, "playlists_manager")


async def test_playlist_filter_options_default_to_weekly_playlists() -> None:
    """Weekly playlist types are enabled by default while yearly types are disabled."""
    provider = _provider()

    entries = await provider.get_config_entries()

    assert [(entry.key, entry.type, entry.default_value) for entry in entries] == [
        ("enable_weekly_jams", ConfigEntryType.BOOLEAN, True),
        ("enable_weekly_exploration", ConfigEntryType.BOOLEAN, True),
        ("enable_yearly_missed_tracks", ConfigEntryType.BOOLEAN, False),
        ("enable_yearly_top_discoveries", ConfigEntryType.BOOLEAN, False),
    ]


async def test_provider_initializes_and_schedules_playlist_refreshes() -> None:
    """Initialization restores playlists before registering the expected refresh tasks."""
    provider = _provider()
    get_value = cast("MagicMock", provider.config.get_value)
    get_value.return_value = "synthetic-user"
    cast("MagicMock", provider.mass.config.get).return_value = None
    cast("MagicMock", provider.config.values.get).return_value = None

    async def load_cache() -> None:
        assert isinstance(provider.api, ListenBrainzAPIClient)
        assert isinstance(provider.playlists_manager, ListenBrainzPlaylistManager)
        assert provider.username == "synthetic-user"
        cast("MagicMock", provider.mass.tasks.register_scheduled_task).assert_not_called()
        cast("MagicMock", provider.mass.call_later).assert_not_called()

    with patch.object(
        ListenBrainzPlaylistManager, "load_cache", AsyncMock(side_effect=load_cache)
    ) as restore:
        await provider.handle_async_init()

    restore.assert_awaited_once_with()

    get_value.assert_any_call(CONF_USERNAME, None)
    register_scheduled_task = cast("MagicMock", provider.mass.tasks.register_scheduled_task)
    register_scheduled_task.assert_called_once()
    scheduled_task = register_scheduled_task.call_args.kwargs
    assert scheduled_task["task_id"] == "listenbrainz_refresh_playlists_listenbrainz--test"
    assert scheduled_task["name"] == "Refresh ListenBrainz playlists"
    assert scheduled_task["handler"] == provider.playlists_manager.refresh
    assert scheduled_task["schedule"] == TaskSchedule.hourly(every=6)
    cast("MagicMock", provider.mass.call_later).assert_called_once_with(
        20,
        provider.playlists_manager.refresh,
        task_id="listenbrainz_refresh_playlists_initial_listenbrainz--test",
    )


async def test_provider_unload_removes_playlist_refresh_tasks() -> None:
    """Unloading the provider removes its recurring and delayed refresh work."""
    provider = _provider()

    await provider.unload(is_removed=True)

    cast("MagicMock", provider.mass.tasks.unregister_scheduled_task).assert_called_once_with(
        "listenbrainz_refresh_playlists_listenbrainz--test",
        clear_persisted_state=True,
    )
    cast("MagicMock", provider.mass.cancel_timer).assert_called_once_with(
        "listenbrainz_refresh_playlists_initial_listenbrainz--test"
    )
