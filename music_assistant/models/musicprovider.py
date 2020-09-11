"""Model and helpers for Music Providers."""

from abc import abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from music_assistant.models.media_types import (
    Album,
    Artist,
    MediaType,
    Playlist,
    Radio,
    SearchResult,
    Track,
)
from music_assistant.models.provider import Provider, ProviderType
from music_assistant.models.streamdetails import StreamDetails


@dataclass
class MusicProvider(Provider):
    """
    Base class for a Musicprovider.

    Should be overriden in the provider specific implementation.
    """

    type: ProviderType = ProviderType.MUSIC_PROVIDER

    @property
    def supported_mediatypes(self) -> List[MediaType]:
        """Return MediaTypes the provider supports."""
        return [
            MediaType.Album,
            MediaType.Artist,
            MediaType.Playlist,
            MediaType.Radio,
            MediaType.Track,
        ]

    @abstractmethod
    async def async_search(
        self, search_query: str, media_types=Optional[List[MediaType]], limit: int = 5
    ) -> SearchResult:
        """
        Perform search on musicprovider.

            :param search_query: Search query.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: Number of items to return in the search (per type).
        """
        raise NotImplementedError

    @abstractmethod
    async def async_get_library_artists(self) -> List[Artist]:
        """Retrieve library artists from the provider."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_library_albums(self) -> List[Album]:
        """Retrieve library albums from the provider."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_library_tracks(self) -> List[Track]:
        """Retrieve library tracks from the provider."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_library_playlists(self) -> List[Playlist]:
        """Retrieve library/subscribed playlists from the provider."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_radios(self) -> List[Radio]:
        """Retrieve library/subscribed radio stations from the provider."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_artist_albums(self, prov_artist_id: str) -> List[Album]:
        """Get a list of all albums for the given artist."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_artist_toptracks(self, prov_artist_id: str) -> List[Track]:
        """Get a list of most popular tracks for the given artist."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_album_tracks(self, prov_album_id: str) -> List[Track]:
        """Get album tracks for given album id."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_playlist_tracks(self, prov_playlist_id: str) -> List[Track]:
        """Get all playlist tracks for given playlist id."""
        raise NotImplementedError

    @abstractmethod
    async def async_library_add(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Add item to provider's library. Return true on succes."""
        raise NotImplementedError

    @abstractmethod
    async def async_library_remove(
        self, prov_item_id: str, media_type: MediaType
    ) -> bool:
        """Remove item from provider's library. Return true on succes."""
        raise NotImplementedError

    @abstractmethod
    async def async_add_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> bool:
        """Add track(s) to playlist. Return true on succes."""
        raise NotImplementedError

    @abstractmethod
    async def async_remove_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> bool:
        """Remove track(s) from playlist. Return true on succes."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        raise NotImplementedError
