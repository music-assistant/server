"""Model and helpers for Music Providers."""

from abc import abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from music_assistant.mass import MusicAssistant
from music_assistant.models.media_types import (
    Album,
    Artist,
    MediaType,
    Playlist,
    Radio,
    SearchResult,
    StreamDetails,
    Track,
)
from music_assistant.models.config_entry import ConfigEntry


@dataclass
class MusicProvider:
    """
        Base class for a Musicprovider.
        Should be overriden in the provider specific implementation.
    """
    mass: Optional[MusicAssistant]

    @abstractmethod
    @property
    def provider_id(self) -> str:
        """Return Provider id of this provider."""
        return None

    @abstractmethod
    @property
    def name(self) -> str:
        """Return name of this provider."""
        return None

    @abstractmethod
    @property
    def config_entries(self) -> List[ConfigEntry]:
        """Return custom config entries for this provider."""
        return []

    @abstractmethod
    async def async_setup(self, conf: dict) -> bool:
        """Setup the provider, will be called on start. Return True if succesfull."""
        return False

    @abstractmethod
    async def async_search(self, search_string: str, media_types=List[MediaType],
                           limit: int = 5) -> SearchResult:
        """Perform search on the provider."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_library_artists(self) -> List[Artist]:
        """Retrieve library artists from the provider. Iterator."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_library_albums(self) -> List[Album]:
        """Retrieve library albums from the provider. Iterator."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_library_tracks(self) -> List[Track]:
        """Retrieve library tracks from the provider. Iterator."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_library_playlists(self) -> List[Playlist]:
        """Retrieve library/subscribed playlists from the provider. Iterator."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_radios(self) -> List[Radio]:
        """Retrieve library/subscribed radio stations from the provider. Iterator."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id"""
        raise NotImplementedError

    @abstractmethod
    async def async_get_artist_albums(self, prov_artist_id: str) -> List[Album]:
        """Get a list of all albums for the given artist. Iterator."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_artist_toptracks(self, prov_artist_id: str) -> List[Track]:
        """Get a list of most popular tracks for the given artist. Iterator."""
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
        """Get full playlist details by id"""
        raise NotImplementedError

    @abstractmethod
    async def async_get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id"""
        raise NotImplementedError

    @abstractmethod
    async def async_get_album_tracks(self, prov_album_id: str) -> List[Track]:
        """Get album tracks for given album id. Iterator."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_playlist_tracks(self, prov_playlist_id: str) -> List[Track]:
        """Get all playlist tracks for given playlist id. Iterator."""
        raise NotImplementedError

    @abstractmethod
    async def async_add_library(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Add item to provider's library. Return true on succes."""
        raise NotImplementedError

    @abstractmethod
    async def async_remove_library(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on succes."""
        raise NotImplementedError

    @abstractmethod
    async def async_add_playlist_tracks(
            self, prov_playlist_id: str, prov_track_ids: List[str]) -> bool:
        """Add track(s) to playlist. Return true on succes."""
        raise NotImplementedError

    @abstractmethod
    async def async_remove_playlist_tracks(
            self, prov_playlist_id: str, prov_track_ids: List[str]) -> bool:
        """Remove track(s) from playlist. Return true on succes."""
        raise NotImplementedError

    @abstractmethod
    async def async_get_stream_details(
            self, track_id: str) -> StreamDetails:
        """get streamdetails for a track"""
        raise NotImplementedError
