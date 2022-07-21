"""Model for a Music Providers."""
from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, Any, AsyncGenerator, Dict, List, Optional, Tuple

from music_assistant.models.config import MusicProviderConfig
from music_assistant.models.enums import MediaType, MusicProviderFeature, ProviderType
from music_assistant.models.media_items import (
    Album,
    Artist,
    BrowseFolder,
    MediaItemType,
    Playlist,
    Radio,
    StreamDetails,
    Track,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


class MusicProvider:
    """Model for a Music Provider."""

    _attr_name: str = None
    _attr_type: ProviderType = None
    _attr_available: bool = True

    def __init__(self, mass: MusicAssistant, config: MusicProviderConfig) -> None:
        """Initialize MusicProvider."""
        self.mass = mass
        self.config = config
        self.logger = mass.logger
        self.cache = mass.cache

    @property
    def supported_features(self) -> Tuple[MusicProviderFeature]:
        """Return the features supported by this MusicProvider."""
        return tuple()

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

    async def search(
        self, search_query: str, media_types=Optional[List[MediaType]], limit: int = 5
    ) -> List[MediaItemType]:
        """
        Perform search on musicprovider.

            :param search_query: Search query.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: Number of items to return in the search (per type).
        """
        if MusicProviderFeature.SEARCH in self.supported_features:
            raise NotImplementedError

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from the provider."""
        if MusicProviderFeature.LIBRARY_ARTISTS in self.supported_features:
            raise NotImplementedError

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        if MusicProviderFeature.LIBRARY_ALBUMS in self.supported_features:
            raise NotImplementedError

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        if MusicProviderFeature.LIBRARY_TRACKS in self.supported_features:
            raise NotImplementedError

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve library/subscribed playlists from the provider."""
        if MusicProviderFeature.LIBRARY_PLAYLISTS in self.supported_features:
            raise NotImplementedError

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        if MusicProviderFeature.LIBRARY_RADIOS in self.supported_features:
            raise NotImplementedError

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        raise NotImplementedError

    async def get_artist_albums(self, prov_artist_id: str) -> List[Album]:
        """Get a list of all albums for the given artist."""
        if MusicProviderFeature.ARTIST_ALBUMS in self.supported_features:
            raise NotImplementedError
        return []

    async def get_artist_toptracks(self, prov_artist_id: str) -> List[Track]:
        """Get a list of most popular tracks for the given artist."""
        if MusicProviderFeature.ARTIST_TOPTRACKS in self.supported_features:
            raise NotImplementedError
        return []

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        raise NotImplementedError

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        raise NotImplementedError

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        raise NotImplementedError

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        raise NotImplementedError

    async def get_album_tracks(self, prov_album_id: str) -> List[Track]:
        """Get album tracks for given album id."""
        raise NotImplementedError

    async def get_playlist_tracks(self, prov_playlist_id: str) -> List[Track]:
        """Get all playlist tracks for given playlist id."""
        raise NotImplementedError

    async def library_add(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Add item to provider's library. Return true on succes."""
        if (
            media_type == MediaType.ARTIST
            and MusicProviderFeature.LIBRARY_ARTISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.ALBUM
            and MusicProviderFeature.LIBRARY_ALBUMS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.TRACK
            and MusicProviderFeature.LIBRARY_TRACKS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.PLAYLIST
            and MusicProviderFeature.LIBRARY_PLAYLISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.RADIO
            and MusicProviderFeature.LIBRARY_RADIOS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        self.logger.info(
            "Provider %s does not support library edit, "
            "the action will only be performed in the local database.",
            self.type.value,
        )

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on succes."""
        if (
            media_type == MediaType.ARTIST
            and MusicProviderFeature.LIBRARY_ARTISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.ALBUM
            and MusicProviderFeature.LIBRARY_ALBUMS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.TRACK
            and MusicProviderFeature.LIBRARY_TRACKS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.PLAYLIST
            and MusicProviderFeature.LIBRARY_PLAYLISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.RADIO
            and MusicProviderFeature.LIBRARY_RADIOS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        self.logger.info(
            "Provider %s does not support library edit, "
            "the action will only be performed in the local database.",
            self.type.value,
        )

    async def add_playlist_tracks(
        self, prov_playlist_id: str, prov_track_ids: List[str]
    ) -> None:
        """Add track(s) to playlist."""
        if MusicProviderFeature.PLAYLIST_TRACKS_EDIT in self.supported_features:
            raise NotImplementedError

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: Tuple[int]
    ) -> None:
        """Remove track(s) from playlist."""
        if MusicProviderFeature.PLAYLIST_TRACKS_EDIT in self.supported_features:
            raise NotImplementedError

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        raise NotImplementedError

    async def get_stream_details(self, item_id: str) -> StreamDetails | None:
        """Get streamdetails for a track/radio."""
        raise NotImplementedError

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        raise NotImplementedError

    async def get_item(self, media_type: MediaType, prov_item_id: str) -> MediaItemType:
        """Get single MediaItem from provider."""
        if media_type == MediaType.ARTIST:
            return await self.get_artist(prov_item_id)
        if media_type == MediaType.ALBUM:
            return await self.get_album(prov_item_id)
        if media_type == MediaType.PLAYLIST:
            return await self.get_playlist(prov_item_id)
        if media_type == MediaType.RADIO:
            return await self.get_radio(prov_item_id)
        return await self.get_track(prov_item_id)

    async def browse(self, path: str) -> BrowseFolder:
        """
        Browse this provider's items.

            :param path: The path to browse, (e.g. provid://artists).
        """
        if MusicProviderFeature.BROWSE not in self.supported_features:
            # we may NOT use the default implementation if the provider does not support browse
            raise NotImplementedError

        _, subpath = path.split("://")

        # this reference implementation can be overridden with provider specific approach
        if not subpath:
            # return main listing
            root_items = []
            if MusicProviderFeature.LIBRARY_ARTISTS in self.supported_features:
                root_items.append(
                    BrowseFolder(
                        item_id="artists",
                        provider=self.type,
                        path=path + "artists",
                        name="",
                        label="artists",
                    )
                )
            if MusicProviderFeature.LIBRARY_ALBUMS in self.supported_features:
                root_items.append(
                    BrowseFolder(
                        item_id="albums",
                        provider=self.type,
                        path=path + "albums",
                        name="",
                        label="albums",
                    )
                )
            if MusicProviderFeature.LIBRARY_TRACKS in self.supported_features:
                root_items.append(
                    BrowseFolder(
                        item_id="tracks",
                        provider=self.type,
                        path=path + "tracks",
                        name="",
                        label="tracks",
                    )
                )
            if MusicProviderFeature.LIBRARY_PLAYLISTS in self.supported_features:
                root_items.append(
                    BrowseFolder(
                        item_id="playlists",
                        provider=self.type,
                        path=path + "playlists",
                        name="",
                        label="playlists",
                    )
                )
            if MusicProviderFeature.LIBRARY_RADIOS in self.supported_features:
                root_items.append(
                    BrowseFolder(
                        item_id="radios",
                        provider=self.type,
                        path=path + "radios",
                        name="",
                        label="radios",
                    )
                )
            return BrowseFolder(
                item_id="root",
                provider=self.type,
                path=path,
                name="",
                items=root_items,
            )
        # sublevel
        if subpath == "artists":
            return BrowseFolder(
                item_id="artists",
                provider=self.type,
                path=path,
                name="",
                label="artists",
                items=[x async for x in self.get_library_artists()],
            )
        if subpath == "albums":
            return BrowseFolder(
                item_id="albums",
                provider=self.type,
                path=path,
                name="",
                label="albums",
                items=[x async for x in self.get_library_albums()],
            )
        if subpath == "tracks":
            return BrowseFolder(
                item_id="tracks",
                provider=self.type,
                path=path,
                name="",
                label="tracks",
                items=[x async for x in self.get_library_tracks()],
            )
        if subpath == "radios":
            return BrowseFolder(
                item_id="radios",
                provider=self.type,
                path=path,
                name="",
                label="radios",
                items=[x async for x in self.get_library_radios()],
            )
        if subpath == "playlists":
            return BrowseFolder(
                item_id="playlists",
                provider=self.type,
                path=path,
                name="",
                label="playlists",
                items=[x async for x in self.get_library_playlists()],
            )

    async def recommendations(self) -> List[BrowseFolder]:
        """
        Get this provider's recommendations.

            Returns a list of BrowseFolder items with (max 25) mediaitems in the items attribute.
        """
        if MusicProviderFeature.RECOMMENDATIONS in self.supported_features:
            raise NotImplementedError

    async def sync_library(
        self, media_types: Optional[Tuple[MediaType]] = None
    ) -> None:
        """Run library sync for this provider."""
        # this reference implementation can be overridden with provider specific approach
        # this logic is aimed at streaming/online providers,
        # which all have more or less the same structure.
        # filesystem implementation(s) just override this.
        if media_types is None:
            media_types = (x for x in MediaType)
        for media_type in media_types:
            if not self.library_supported(media_type):
                continue
            self.logger.debug("Start sync of %s items.", media_type.value)
            controller = self.mass.music.get_controller(media_type)
            cur_db_ids = set()
            async for prov_item in self._get_library_gen(media_type)():
                prov_item: MediaItemType = prov_item

                db_item: MediaItemType = await controller.get_db_item_by_prov_id(
                    provider_item_id=prov_item.item_id,
                    provider=prov_item.provider,
                )
                if not db_item:
                    # dump the item in the db, rich metadata is lazy loaded later
                    db_item = await controller.add_db_item(prov_item)

                elif (
                    db_item.metadata.checksum and prov_item.metadata.checksum
                ) and db_item.metadata.checksum != prov_item.metadata.checksum:
                    # item checksum changed
                    db_item = await controller.update_db_item(
                        db_item.item_id, prov_item
                    )
                    # preload album/playlist tracks
                    if prov_item.media_type == (MediaType.ALBUM, MediaType.PLAYLIST):
                        for track in controller.tracks(
                            prov_item.item_id, prov_item.provider
                        ):
                            await self.mass.music.tracks.add_db_item(track)
                cur_db_ids.add(db_item.item_id)
                if not db_item.in_library:
                    await controller.set_db_library(db_item.item_id, True)

            # process deletions (= no longer in library)
            async for db_item in controller.iter_db_items(True):
                if db_item.item_id in cur_db_ids:
                    continue
                for prov_id in db_item.provider_ids:
                    prov_types = {x.prov_type for x in db_item.provider_ids}
                    if len(prov_types) > 1:
                        continue
                    if prov_id.prov_id != self.id:
                        continue
                    # only mark the item as not in library and leave the metadata in db
                    await controller.set_db_library(db_item.item_id, False)

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
            "supported_features": [x.value for x in self.supported_features],
        }

    def library_supported(self, media_type: MediaType) -> bool:
        """Return if Library is supported for given MediaType on this provider."""
        if media_type == MediaType.ARTIST:
            return MusicProviderFeature.LIBRARY_ARTISTS in self.supported_features
        if media_type == MediaType.ALBUM:
            return MusicProviderFeature.LIBRARY_ALBUMS in self.supported_features
        if media_type == MediaType.TRACK:
            return MusicProviderFeature.LIBRARY_TRACKS in self.supported_features
        if media_type == MediaType.PLAYLIST:
            return MusicProviderFeature.LIBRARY_PLAYLISTS in self.supported_features
        if media_type == MediaType.RADIO:
            return MusicProviderFeature.LIBRARY_RADIOS in self.supported_features

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
