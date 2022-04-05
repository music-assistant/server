"""Model for a Music Providers."""
from __future__ import annotations

from abc import abstractmethod
from logging import Logger
from typing import List, Optional

from music_assistant.helpers.typing import MusicAssistant
from music_assistant.models.media_items import (
    Album,
    Artist,
    MediaItemType,
    MediaType,
    Playlist,
    Radio,
    Track,
)
from music_assistant.models.player_queue import StreamDetails


class MusicProvider:
    """Model for a Music Provider."""

    _attr_id: str = None
    _attr_name: str = None
    _attr_available: bool = True
    _attr_supported_mediatypes: List[MediaType] = []
    mass: MusicAssistant = None  # set by setup
    logger: Logger = None  # set by setup

    @abstractmethod
    async def setup(self) -> None:
        """
        Handle async initialization of the provider.

        Called when provider is registered.
        """

    @property
    def id(self) -> str:
        """Return provider ID for this provider."""
        return self._attr_id

    @property
    def name(self) -> str:
        """Return provider Name for this provider."""
        return self._attr_name

    @property
    def available(self) -> bool:
        """Return boolean if this provider is available/initialized."""
        return self._attr_available

    @property
    def supported_mediatypes(self) -> List[MediaType]:
        """Return MediaTypes the provider supports."""
        return self._attr_supported_mediatypes

    async def search(
        self, search_query: str, media_types=Optional[List[MediaType]], limit: int = 5
    ) -> List[MediaItemType]:
        """
        Perform search on musicprovider.

            :param search_query: Search query.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: Number of items to return in the search (per type).
        """
        raise NotImplementedError

    async def get_library_artists(self) -> List[Artist]:
        """Retrieve library artists from the provider."""
        if MediaType.ARTIST in self.supported_mediatypes:
            raise NotImplementedError

    async def get_library_albums(self) -> List[Album]:
        """Retrieve library albums from the provider."""
        if MediaType.ALBUM in self.supported_mediatypes:
            raise NotImplementedError

    async def get_library_tracks(self) -> List[Track]:
        """Retrieve library tracks from the provider."""
        if MediaType.TRACK in self.supported_mediatypes:
            raise NotImplementedError

    async def get_library_playlists(self) -> List[Playlist]:
        """Retrieve library/subscribed playlists from the provider."""
        if MediaType.PLAYLIST in self.supported_mediatypes:
            raise NotImplementedError

    async def get_library_radios(self) -> List[Radio]:
        """Retrieve library/subscribed radio stations from the provider."""
        if MediaType.RADIO in self.supported_mediatypes:
            raise NotImplementedError

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        if MediaType.ARTIST in self.supported_mediatypes:
            raise NotImplementedError

    async def get_artist_albums(self, prov_artist_id: str) -> List[Album]:
        """Get a list of all albums for the given artist."""
        if MediaType.ALBUM in self.supported_mediatypes:
            raise NotImplementedError

    async def get_artist_toptracks(self, prov_artist_id: str) -> List[Track]:
        """Get a list of most popular tracks for the given artist."""
        if MediaType.TRACK in self.supported_mediatypes:
            raise NotImplementedError

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        if MediaType.ALBUM in self.supported_mediatypes:
            raise NotImplementedError

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        if MediaType.TRACK in self.supported_mediatypes:
            raise NotImplementedError

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if MediaType.PLAYLIST in self.supported_mediatypes:
            raise NotImplementedError

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        if MediaType.RADIO in self.supported_mediatypes:
            raise NotImplementedError

    async def get_album_tracks(self, prov_album_id: str) -> List[Track]:
        """Get album tracks for given album id."""
        if MediaType.ALBUM in self.supported_mediatypes:
            raise NotImplementedError

    async def get_playlist_tracks(self, prov_playlist_id: str) -> List[Track]:
        """Get all playlist tracks for given playlist id."""
        if MediaType.PLAYLIST in self.supported_mediatypes:
            raise NotImplementedError

    async def library_add(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Add item to provider's library. Return true on succes."""
        raise NotImplementedError

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on succes."""
        raise NotImplementedError

    async def add_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> bool:
        """Add track(s) to playlist. Return true on succes."""
        if MediaType.PLAYLIST in self.supported_mediatypes:
            raise NotImplementedError

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> bool:
        """Remove track(s) from playlist. Return true on succes."""
        if MediaType.PLAYLIST in self.supported_mediatypes:
            raise NotImplementedError

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        raise NotImplementedError

    # some helper methods below
    async def get_library_items(self, media_type: MediaType) -> List[MediaItemType]:
        """Return library items for given media_type."""
        if media_type == MediaType.ARTIST:
            return await self.get_library_artists()
        if media_type == MediaType.ALBUM:
            return await self.get_library_albums()
        if media_type == MediaType.TRACK:
            return await self.get_library_tracks()
        if media_type == MediaType.PLAYLIST:
            return await self.get_library_playlists()
        if media_type == MediaType.RADIO:
            return await self.get_library_radios()

    async def get_item(self, media_type: MediaType, prov_item_id: str) -> MediaItemType:
        """Get single MediaItem from provider."""
        if media_type == MediaType.ARTIST:
            return await self.get_artist(prov_item_id)
        if media_type == MediaType.ALBUM:
            return await self.get_album(prov_item_id)
        if media_type == MediaType.TRACK:
            return await self.get_track(prov_item_id)
        if media_type == MediaType.PLAYLIST:
            return await self.get_playlist(prov_item_id)
        if media_type == MediaType.RADIO:
            return await self.get_radio(prov_item_id)
