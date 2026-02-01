"""YouSee Musik musicprovider support for MusicAssistant."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant_models.errors import (
    LoginFailed,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemType,
    Playlist,
    RecommendationFolder,
    SearchResults,
    Track,
)

from music_assistant.constants import (
    CONF_PASSWORD,
    CONF_USERNAME,
)
from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.yousee.api_client import YouSeeAPIClient
from music_assistant.providers.yousee.auth_manager import YouSeeAuthManager
from music_assistant.providers.yousee.library import YouSeeLibraryManager
from music_assistant.providers.yousee.media import YouSeeMediaManager
from music_assistant.providers.yousee.playlist import YouSeePlaylistManager
from music_assistant.providers.yousee.recommendations import YouSeeRecommendationsManager
from music_assistant.providers.yousee.streaming import YouSeeStreamingManager

if TYPE_CHECKING:
    from music_assistant_models.enums import (
        MediaType,
    )
    from music_assistant_models.media_items import (
        Album,
        Artist,
        MediaItemType,
        Playlist,
        RecommendationFolder,
        SearchResults,
        Track,
    )
    from music_assistant_models.streamdetails import StreamDetails


class YouSeeMusikProvider(MusicProvider):
    """Provider implementation for YouSee Musik."""

    auth: YouSeeAuthManager

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if not self.config.get_value(CONF_USERNAME) or not self.config.get_value(CONF_PASSWORD):
            msg = "Invalid login credentials"
            raise LoginFailed(msg)
        # try to get a token, raise if that fails
        self.auth = YouSeeAuthManager(self)
        self.api = YouSeeAPIClient(self)
        self.library = YouSeeLibraryManager(self)
        self.media = YouSeeMediaManager(self)
        self.playlist = YouSeePlaylistManager(self)
        self.streaming = YouSeeStreamingManager(self)
        self.recommendations_manager = YouSeeRecommendationsManager(self)

        token = await self.auth.auth_token()
        if not token:
            msg = f"Login failed for user {self.config.get_value(CONF_USERNAME)}"
            raise LoginFailed(msg)

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        return await self.media.search(search_query, media_types, limit)

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from the provider."""
        async for artist in self.library.get_artists():
            yield artist

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        async for album in self.library.get_albums():
            yield album

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        async for track in self.library.get_tracks():
            yield track

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve library/subscribed playlists from the provider."""
        async for playlist in self.library.get_playlists():
            yield playlist

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        return await self.media.get_artist(prov_artist_id)

    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        return await self.media.get_artist_albums(prov_artist_id)

    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of most popular tracks for the given artist."""
        return await self.media.get_artist_toptracks(prov_artist_id)

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        return await self.media.get_album(prov_album_id)

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        return await self.media.get_track(prov_track_id)

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        return await self.media.get_playlist(prov_playlist_id)

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_album_tracks(
        self,
        prov_album_id: str,
    ) -> list[Track]:
        """Get album tracks for given album id."""
        return await self.media.get_album_tracks(prov_album_id)

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_playlist_tracks(
        self,
        prov_playlist_id: str,
        page: int = 0,
    ) -> list[Track]:
        """Get all playlist tracks for given playlist id."""
        return await self.media.get_playlist_tracks(prov_playlist_id, page)

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to provider's library. Return true on success."""
        return await self.library.add_item(item)

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on success."""
        return await self.library.remove_item(prov_item_id, media_type)

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        return await self.playlist.add_tracks(prov_playlist_id, prov_track_ids)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        return await self.playlist.remove_tracks(prov_playlist_id, positions_to_remove)

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        return await self.playlist.create(name)

    @use_cache(3600 * 24)  # Cache for 24 hours
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of similar tracks based on the provided track."""
        return await self.media.get_similar_tracks(prov_track_id, limit)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track."""
        return await self.streaming.get_stream_details(item_id, media_type)

    async def on_streamed(
        self,
        streamdetails: StreamDetails,
    ) -> None:
        """
        Handle callback when given streamdetails completed streaming.

        To get the number of seconds streamed, see streamdetails.seconds_streamed.
        To get the number of seconds seeked/skipped, see streamdetails.seek_position.
        Note that seconds_streamed is the total streamed seconds, so without seeked time.

        NOTE: Due to internal and player buffering,
        this may be called in advance of the actual completion.
        """
        await self.streaming.report_playback(
            streamdetails,
        )

    @use_cache(3600 * 24)  # Cache for 1 day
    async def recommendations(self) -> list[RecommendationFolder]:
        """
        Get this provider's recommendations.

        Returns an actual (and often personalised) list of recommendations
        from this provider for the user/account.
        """
        return await self.recommendations_manager.get_recommendations()
