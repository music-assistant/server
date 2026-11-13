"""ListenBrainz playlist provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ProviderFeature

from music_assistant.constants import CONF_USERNAME
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.listenbrainz.api_client import ListenBrainzAPIClient
from music_assistant.providers.listenbrainz.constants import (
    CONF_ENABLE_WEEKLY_EXPLORATION,
    CONF_ENABLE_WEEKLY_JAMS,
    CONF_ENABLE_YEARLY_MISSED_TRACKS,
    CONF_ENABLE_YEARLY_TOP_DISCOVERIES,
)
from music_assistant.providers.listenbrainz.playlists import ListenBrainzPlaylistManager

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import Playlist, Track
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {ProviderFeature.BROWSE}
REFRESH_TASK_ID = "listenbrainz_refresh_playlists"
REFRESH_INTERVAL_HOURS = 6
INITIAL_REFRESH_DELAY = 20


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the ListenBrainz provider."""
    return ListenBrainzProvider(mass, manifest, config, SUPPORTED_FEATURES)


class ListenBrainzProvider(PluginProvider):
    """Expose ListenBrainz recommendation playlists."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the playlist filtering options."""
        return (
            ConfigEntry(
                key=CONF_ENABLE_WEEKLY_JAMS,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                required=False,
                category="playlists",
            ),
            ConfigEntry(
                key=CONF_ENABLE_WEEKLY_EXPLORATION,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                required=False,
                category="playlists",
            ),
            ConfigEntry(
                key=CONF_ENABLE_YEARLY_MISSED_TRACKS,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
                category="playlists",
            ),
            ConfigEntry(
                key=CONF_ENABLE_YEARLY_TOP_DISCOVERIES,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
                category="playlists",
            ),
        )

    async def handle_async_init(self) -> None:
        """Initialize the ListenBrainz API client."""
        self.api = ListenBrainzAPIClient(self)
        self.username = str(self.get_setup_value(CONF_USERNAME) or "")
        self.playlists_manager = ListenBrainzPlaylistManager(self)
        await self.playlists_manager.load_cache()

        self.mass.tasks.register_scheduled_task(
            task_id=f"{REFRESH_TASK_ID}_{self.instance_id}",
            name="Refresh ListenBrainz playlists",
            handler=self.playlists_manager.refresh,
            schedule=TaskSchedule.hourly(every=REFRESH_INTERVAL_HOURS),
        )

        self.mass.call_later(
            INITIAL_REFRESH_DELAY,
            self.playlists_manager.refresh,
            task_id=f"{REFRESH_TASK_ID}_initial_{self.instance_id}",
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider and remove its playlist refresh tasks."""
        self.mass.tasks.unregister_scheduled_task(
            f"{REFRESH_TASK_ID}_{self.instance_id}",
            clear_persisted_state=is_removed,
        )
        self.mass.cancel_timer(f"{REFRESH_TASK_ID}_initial_{self.instance_id}")
        await super().unload(is_removed)

    async def browse(self, path: str) -> list[Playlist]:
        """Return the user's recommendation playlists."""
        return await self.playlists_manager.browse()

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Return a ListenBrainz playlist."""
        return await self.playlists_manager.get_playlist(prov_playlist_id)

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Return resolved tracks from a ListenBrainz playlist."""
        return await self.playlists_manager.get_playlist_tracks(prov_playlist_id, page)
