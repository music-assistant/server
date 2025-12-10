"""Jellyfin support for MusicAssistant."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, NoReturn

from music_assistant_models.media_items import (
    Album,
    Artist,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
)

from music_assistant.constants import UNKNOWN_ARTIST_ID_MBID
from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.jellyfin import config as jellyfin_config
from music_assistant.providers.jellyfin.auth import authenticate
from music_assistant.providers.jellyfin.library import JellyfinLibrary
from music_assistant.providers.jellyfin.search import JellyfinSearch
from music_assistant.providers.jellyfin.streaming import JellyfinStreaming

from .const import UNKNOWN_ARTIST_MAPPING

if TYPE_CHECKING:
    from aiojellyfin import Connection
    from music_assistant_models.enums import MediaType
    from music_assistant_models.streamdetails import StreamDetails

# Re-export config functions and constants for module interface
setup = jellyfin_config.setup
get_config_entries = jellyfin_config.get_config_entries
SUPPORTED_FEATURES = jellyfin_config.SUPPORTED_FEATURES
CONF_URL = jellyfin_config.CONF_URL
CONF_USERNAME = jellyfin_config.CONF_USERNAME
CONF_PASSWORD = jellyfin_config.CONF_PASSWORD
CONF_VERIFY_SSL = jellyfin_config.CONF_VERIFY_SSL


class JellyfinProvider(MusicProvider):
    """Provider for a jellyfin music library."""

    # client is initialized in async init
    _client: Connection | None = None
    _library: JellyfinLibrary | None = None
    _streaming: JellyfinStreaming | None = None
    _search: JellyfinSearch | None = None

    async def handle_async_init(self) -> None:
        """Initialize provider(instance) with given configuration."""
        username = str(self.config.get_value(CONF_USERNAME))
        password = str(self.config.get_value(CONF_PASSWORD))
        url = str(self.config.get_value(CONF_URL))
        verify_ssl = bool(self.config.get_value(CONF_VERIFY_SSL))
        http_session = self.mass.http_session if verify_ssl else self.mass.http_session_no_ssl

        # Authenticate with Jellyfin server
        self._client = await authenticate(
            server_id=self.mass.server_id,
            username=username,
            password=password,
            url=url,
            verify_ssl=verify_ssl,
            http_session=http_session,
            app_version=self.mass.version,
            logger=self.logger,
        )

        # Instantiate helpers that encapsulate provider logic
        assert self._client is not None
        self._library = JellyfinLibrary(self._client, self.logger, self.instance_id, self.domain)
        self._streaming = JellyfinStreaming(self._client, self.logger, self.instance_id)
        self._search = JellyfinSearch(self._client, self.logger, self.instance_id)

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return False

    async def _search_track(self, search_query: str, limit: int) -> list[Track]:
        """Search for tracks - delegates to search helper."""
        assert self._search is not None
        return await self._search.search_track(search_query, limit)

    async def _search_album(self, search_query: str, limit: int) -> list[Album]:
        """Search for albums - delegates to search helper."""
        assert self._search is not None
        return await self._search.search_album(search_query, limit)

    async def _search_artist(self, search_query: str, limit: int) -> list[Artist]:
        """Search for artists - delegates to search helper."""
        assert self._search is not None
        return await self._search.search_artist(search_query, limit)

    async def _search_playlist(self, search_query: str, limit: int) -> list[Playlist]:
        """Search for playlists - delegates to search helper."""
        assert self._search is not None
        return await self._search.search_playlist(search_query, limit)

    @use_cache(60 * 15)  # Cache for 15 minutes
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 20,
    ) -> SearchResults:
        """Perform search on the Jellyfin library.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: Number of items to return in the search (per type).
        """
        assert self._search is not None
        return await self._search.search(search_query, media_types, limit)

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Jellyfin Music."""
        assert self._library is not None
        async for artist in self._library.get_library_artists():
            yield artist

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Jellyfin Music."""
        assert self._library is not None
        assert self._library is not None
        async for album in self._library.get_library_albums():
            yield album

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Jellyfin Music."""
        assert self._library is not None
        assert self._library is not None
        async for track in self._library.get_library_tracks():
            yield track

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        assert self._library is not None
        assert self._library is not None
        async for playlist in self._library.get_library_playlists():
            yield playlist

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        assert self._library is not None
        return await self._library.get_album(prov_album_id)

    @use_cache(3600)  # Cache for 1 hour
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        assert self._library is not None
        return await self._library.get_album_tracks(prov_album_id)

    @use_cache(60 * 15)  # Cache for 15 minutes
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        assert self._library is not None
        if prov_artist_id == UNKNOWN_ARTIST_MAPPING.item_id:
            artist = Artist(
                item_id=UNKNOWN_ARTIST_MAPPING.item_id,
                name=UNKNOWN_ARTIST_MAPPING.name,
                provider=self.instance_id,
                provider_mappings={
                    ProviderMapping(
                        item_id=UNKNOWN_ARTIST_MAPPING.item_id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )
            artist.mbid = UNKNOWN_ARTIST_ID_MBID
            return artist

        return await self._library.get_artist(prov_artist_id)

    @use_cache(60 * 15)  # Cache for 15 minutes
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        assert self._library is not None
        return await self._library.get_track(prov_track_id)

    @use_cache(60 * 15)  # Cache for 15 minutes
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        assert self._library is not None
        return await self._library.get_playlist(prov_playlist_id)

    @use_cache(3600)  # Cache for 1 hour
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        assert self._library is not None
        return await self._library.get_playlist_tracks(prov_playlist_id, page)

    @use_cache(3600)  # Cache for 1 hour
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of albums for the given artist."""
        assert self._library is not None
        return await self._library.get_artist_albums(prov_artist_id)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        assert self._streaming is not None
        return await self._streaming.get_stream_details(item_id, media_type)

    @use_cache(3600)  # Cache for 1 hour
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        assert self._streaming is not None
        return await self._streaming.get_similar_tracks(prov_track_id, limit)

    # The following methods are not supported by the Jellyfin provider implementation
    # in this project. Implement small stubs to satisfy the abstract base class and
    # provide clear NotImplementedError messaging for callers.
    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist. Not supported by this provider."""
        raise NotImplementedError("Jellyfin provider does not support adding tracks to playlists")

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist. Not supported by this provider."""
        raise NotImplementedError(
            "Jellyfin provider does not support removing tracks from playlists"
        )

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name. Not supported."""
        raise NotImplementedError("Jellyfin provider does not support creating playlists")

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of most popular tracks for the given artist. Not supported."""
        raise NotImplementedError("Jellyfin provider does not provide artist top tracks")

    async def get_audiobook(self, prov_audiobook_id: str) -> NoReturn:
        """Get full audiobook details by id. Not supported."""
        raise NotImplementedError("Jellyfin provider does not support audiobooks")

    async def get_podcast(self, prov_podcast_id: str) -> NoReturn:
        """Get full podcast details by id. Not supported."""
        raise NotImplementedError("Jellyfin provider does not support podcasts")

    async def get_podcast_episode(self, prov_episode_id: str) -> NoReturn:
        """Get podcast episode details by id. Not supported."""
        raise NotImplementedError("Jellyfin provider does not support podcast episodes")

    async def get_radio(self, prov_radio_id: str) -> NoReturn:
        """Get full radio details by id. Not supported."""
        raise NotImplementedError("Jellyfin provider does not support radios")

    async def get_resume_position(self, item_id: str, media_type: MediaType) -> tuple[bool, int]:
        """Get resume position for audiobook/podcast. Not supported."""
        raise NotImplementedError("Jellyfin provider does not provide resume positions")
