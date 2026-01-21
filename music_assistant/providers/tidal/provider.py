"""Tidal music provider implementation."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import LoginFailed
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemType,
    Playlist,
    RecommendationFolder,
    SearchResults,
    Track,
)

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .api_client import TidalAPIClient
from .auth_manager import TidalAuthManager
from .constants import (
    CACHE_CATEGORY_RECOMMENDATIONS,
    CONF_AUTH_TOKEN,
    CONF_EXPIRY_TIME,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
)
from .library import TidalLibraryManager
from .media import TidalMediaManager
from .playlist import TidalPlaylistManager
from .recommendations import TidalRecommendationManager
from .streaming import TidalStreamingManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant


SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.PLAYLIST_CREATE,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.BROWSE,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.LYRICS,
}


class TidalProvider(MusicProvider):
    """Implementation of a Tidal MusicProvider."""

    def __init__(self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig):
        """Initialize Tidal provider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self.auth = TidalAuthManager(
            http_session=mass.http_session,
            config_updater=self._update_auth_config,
            logger=self.logger,
        )
        self.api = TidalAPIClient(self)
        self.library = TidalLibraryManager(self)
        self.media = TidalMediaManager(self)
        self.playlists = TidalPlaylistManager(self)
        self.recommendations_manager = TidalRecommendationManager(self)
        self.streaming = TidalStreamingManager(self)

    def _update_auth_config(self, auth_info: dict[str, Any]) -> None:
        """Update auth config with new auth info."""
        self.update_config_value(CONF_AUTH_TOKEN, auth_info["access_token"], encrypted=True)
        self.update_config_value(CONF_REFRESH_TOKEN, auth_info["refresh_token"], encrypted=True)
        self.update_config_value(CONF_EXPIRY_TIME, auth_info["expires_at"])
        self.update_config_value(CONF_USER_ID, auth_info["userId"])

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        access_token = self.config.get_value(CONF_AUTH_TOKEN)
        refresh_token = self.config.get_value(CONF_REFRESH_TOKEN)
        expires_at = self.config.get_value(CONF_EXPIRY_TIME)
        user_id = self.config.get_value(CONF_USER_ID)

        if not access_token or not refresh_token:
            raise LoginFailed("Missing authentication data")

        if isinstance(expires_at, str) and "T" in expires_at:
            try:
                dt = datetime.fromisoformat(expires_at)
                expires_at = dt.timestamp()
                self.update_config_value(CONF_EXPIRY_TIME, expires_at)
            except ValueError:
                expires_at = 0

        auth_data = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "userId": user_id,
        }

        if not await self.auth.initialize(json.dumps(auth_data)):
            raise LoginFailed("Failed to authenticate with Tidal")

        api_result = await self.api.get("sessions")
        user_info = api_result[0] if isinstance(api_result, tuple) else api_result
        logged_in_user = await self.get_user(str(user_info.get("userId")))
        await self.auth.update_user_info(logged_in_user, str(user_info.get("sessionId")))

    async def get_user(self, prov_user_id: str) -> dict[str, Any]:
        """Get user information."""
        return await self.api.get_data(f"users/{prov_user_id}")

    @use_cache(3600 * 24 * 14)
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Perform search on musicprovider."""
        return await self.media.search(search_query, media_types, limit)

    @use_cache(3600 * 24)
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Get similar tracks for given track id."""
        return await self.media.get_similar_tracks(prov_track_id, limit)

    @use_cache(3600 * 24 * 30)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details for given artist id."""
        return await self.media.get_artist(prov_artist_id)

    @use_cache(3600 * 24 * 30)
    async def get_album(self, prov_album_id: str) -> Album:
        """Get album details for given album id."""
        return await self.media.get_album(prov_album_id)

    @use_cache(3600 * 24 * 30)
    async def get_track(self, prov_track_id: str) -> Track:
        """Get track details for given track id."""
        return await self.media.get_track(prov_track_id)

    @use_cache(3600 * 24 * 30)
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details for given playlist id."""
        return await self.media.get_playlist(prov_playlist_id)

    @use_cache(3600 * 24 * 30)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        return await self.media.get_album_tracks(prov_album_id)

    @use_cache(3600 * 24 * 7)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        return await self.media.get_artist_albums(prov_artist_id)

    @use_cache(3600 * 24 * 7)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        return await self.media.get_artist_toptracks(prov_artist_id)

    @use_cache(3600 * 3)
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        return await self.media.get_playlist_tracks(prov_playlist_id, page)

    async def get_stream_details(
        self, item_id: str, media_type: MediaType = MediaType.TRACK
    ) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        return await self.streaming.get_stream_details(item_id)

    def get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        """Create a generic item mapping."""
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.instance_id,
            name=name,
        )

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Tidal."""
        async for item in self.library.get_artists():
            yield item

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Tidal."""
        async for item in self.library.get_albums():
            yield item

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Tidal."""
        async for item in self.library.get_tracks():
            yield item

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        async for item in self.library.get_playlists():
            yield item

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to library."""
        return await self.library.add_item(item)

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from library."""
        return await self.library.remove_item(prov_item_id, media_type)

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        return await self.playlists.create(name)

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        await self.playlists.add_tracks(prov_playlist_id, prov_track_ids)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        await self.playlists.remove_tracks(prov_playlist_id, positions_to_remove)

    @use_cache(expiration=3600, category=CACHE_CATEGORY_RECOMMENDATIONS)
    async def recommendations(self) -> list[RecommendationFolder]:
        """Get this provider's recommendations organized into folders."""
        return await self.recommendations_manager.get_recommendations()
