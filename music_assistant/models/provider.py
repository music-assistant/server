"""Model for a Music Providers."""
from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, Any, AsyncGenerator, Dict, List, Optional

from music_assistant.models.config import MusicProviderConfig
from music_assistant.models.enums import MediaType, ProviderType
from music_assistant.models.media_items import (
    Album,
    Artist,
    MediaItemType,
    Playlist,
    Radio,
    Track,
)
from music_assistant.models.player_queue import StreamDetails

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


class MusicProvider:
    """Model for a Music Provider."""

    _attr_name: str = None
    _attr_type: ProviderType = None
    _attr_available: bool = True
    _attr_supported_mediatypes: List[MediaType] = []

    def __init__(self, mass: MusicAssistant, config: MusicProviderConfig) -> None:
        """Initialize MusicProvider."""
        self.mass = mass
        self.config = config
        self.logger = mass.logger
        self.cache = mass.cache

    @abstractmethod
    async def setup(self) -> bool:
        """
        Handle async initialization of the provider.

        Called when provider is registered.
        """

    @property
    def type(self) -> ProviderType:
        """Return provider type for this provider."""
        return self._attr_type

    @property
    def name(self) -> str:
        """Return provider Name for this provider."""
        if sum(1 for x in self.mass.music.providers if x.type == self.type) > 1:
            append_str = self.config.path or self.config.username
            return f"{self._attr_name} ({append_str})"
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

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from the provider."""
        if MediaType.ARTIST in self.supported_mediatypes:
            raise NotImplementedError

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        if MediaType.ALBUM in self.supported_mediatypes:
            raise NotImplementedError

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        if MediaType.TRACK in self.supported_mediatypes:
            raise NotImplementedError

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve library/subscribed playlists from the provider."""
        if MediaType.PLAYLIST in self.supported_mediatypes:
            raise NotImplementedError

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
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
    ) -> None:
        """Add track(s) to playlist."""
        if MediaType.PLAYLIST in self.supported_mediatypes:
            raise NotImplementedError

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> None:
        """Remove track(s) from playlist."""
        if MediaType.PLAYLIST in self.supported_mediatypes:
            raise NotImplementedError

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        raise NotImplementedError

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

    async def sync_library(self) -> None:
        """Run library sync for this provider."""
        # this reference implementation can be overridden with provider specific approach
        # this logic is aimed at streaming/online providers,
        # which all have more or less the same structure.
        # filesystem implementation(s) just override this.
        async with self.mass.database.get_db() as db:
            for media_type in self.supported_mediatypes:
                self.logger.debug("Start sync of %s items.", media_type.value)
                controller = self.mass.music.get_controller(media_type)

                # create a set of all previous and current db id's
                # note we only store the items in the prev_ids list that are
                # unique to this provider to avoid getting into a mess where
                # for example an item still exists on disk (in case of file provider)
                # and no longer favorite on streaming provider.
                # Bottomline this means that we don't do a full 2 way sync if multiple
                # providers are attached to the same media item.
                prev_ids = set()
                for db_item in await controller.library():
                    prov_types = {x.prov_type for x in db_item.provider_ids}
                    if len(prov_types) > 1:
                        continue
                    for prov_id in db_item.provider_ids:
                        if prov_id.prov_id == self.id:
                            prev_ids.add(db_item.item_id)
                cur_ids = set()
                async for prov_item in self._get_library_gen(media_type)():
                    prov_item: MediaItemType = prov_item

                    db_item: MediaItemType = await controller.get_db_item_by_prov_id(
                        provider_item_id=prov_item.item_id,
                        provider=prov_item.provider,
                        db=db,
                    )
                    if not db_item:
                        # dump the item in the db, rich metadata is lazy loaded later
                        db_item = await controller.add_db_item(prov_item, db=db)
                    elif (
                        db_item.metadata.checksum and prov_item.metadata.checksum
                    ) and db_item.metadata.checksum != prov_item.metadata.checksum:
                        # item checksum changed
                        db_item = await controller.update_db_item(
                            db_item.item_id, prov_item, db=db
                        )
                    cur_ids.add(db_item.item_id)
                    if not db_item.in_library:
                        await controller.set_db_library(db_item.item_id, True, db=db)

                # process deletions
                for item_id in prev_ids:
                    if item_id not in cur_ids:
                        # only mark the item as not in library and leave the metadata in db
                        await controller.set_db_library(item_id, False, db=db)

    # DO NOT OVERRIDE BELOW

    @property
    def id(self) -> str:
        """Return unique provider id to distinguish multiple instances of the same provider."""
        return self.config.id

    def to_dict(self) -> Dict[str, Any]:
        """Return (serializable) dict representation of MusicProvider."""
        return {
            "type": self.type.value,
            "name": self.name,
            "id": self.id,
            "supported_mediatypes": [x.value for x in self.supported_mediatypes],
        }

    def _get_library_gen(self, media_type: MediaType) -> AsyncGenerator[MediaItemType]:
        """Return library generator for given media_type."""
        if media_type == MediaType.ARTIST:
            return self.get_library_artists
        if media_type == MediaType.ALBUM:
            return self.get_library_albums
        if media_type == MediaType.TRACK:
            return self.get_library_tracks
        if media_type == MediaType.PLAYLIST:
            return self.get_library_playlists
        if media_type == MediaType.RADIO:
            return self.get_library_radios
