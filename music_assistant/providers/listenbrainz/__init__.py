"""ListenBrainz playlist provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from music_assistant.constants import CONF_USERNAME
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.listenbrainz.api_client import ListenBrainzAPIClient
from music_assistant.providers.listenbrainz.playlist import ListenBrainzPlaylistManager

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import Playlist, Track
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {ProviderFeature.BROWSE}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the ListenBrainz provider."""
    return ListenBrainzProvider(mass, manifest, config, SUPPORTED_FEATURES)


class ListenBrainzProvider(PluginProvider):
    """Expose ListenBrainz recommendation playlists."""

    async def handle_async_init(self) -> None:
        """Initialize the ListenBrainz API client."""
        self.api = ListenBrainzAPIClient(self)
        self.username = str(self.config.get_value(CONF_USERNAME) or "")

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize the ListenBrainz provider."""
        super().__init__(mass, manifest, config, supported_features)
        self.playlists = ListenBrainzPlaylistManager(self)

    async def browse(self, path: str) -> list[Playlist]:
        """Return the user's recommendation playlists."""
        return await self.playlists.browse()

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Return a ListenBrainz playlist."""
        return await self.playlists.get_playlist(prov_playlist_id)

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Return resolved tracks from a ListenBrainz playlist."""
        return await self.playlists.get_playlist_tracks(prov_playlist_id, page)
