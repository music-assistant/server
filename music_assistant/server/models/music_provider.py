"""Model/base for a Music Provider implementation."""
from __future__ import annotations

from collections.abc import AsyncGenerator

from music_assistant.common.models.enums import MediaType, ProviderFeature
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    BrowseFolder,
    MediaItemType,
    Playlist,
    Radio,
    SearchResults,
    StreamDetails,
    Track,
)

from .provider import Provider

# ruff: noqa: ARG001, ARG002


class MusicProvider(Provider):
    """Base representation of a Music Provider (controller).

    Music Provider implementations should inherit from this base model.
    """

    @property
    def is_unique(self) -> bool:
        """
        Return True if the (non user related) data in this provider instance is unique.

        For example on a global streaming provider (like Spotify),
        the data on all instances is the same.
        For a file provider each instance has other items.
        Setting this to False will only query one instance of the provider for search and lookups.
        Setting this to True will query all instances of this provider for search and lookups.
        """
        return False

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType] | None = None,
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: Number of items to return in the search (per type).
        """
        if ProviderFeature.SEARCH in self.supported_features:
            raise NotImplementedError
        return SearchResults()

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from the provider."""
        if ProviderFeature.LIBRARY_ARTISTS in self.supported_features:
            raise NotImplementedError
        yield  # type: ignore

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        if ProviderFeature.LIBRARY_ALBUMS in self.supported_features:
            raise NotImplementedError
        yield  # type: ignore

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        if ProviderFeature.LIBRARY_TRACKS in self.supported_features:
            raise NotImplementedError
        yield  # type: ignore

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve library/subscribed playlists from the provider."""
        if ProviderFeature.LIBRARY_PLAYLISTS in self.supported_features:
            raise NotImplementedError
        yield  # type: ignore

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        if ProviderFeature.LIBRARY_RADIOS in self.supported_features:
            raise NotImplementedError
        yield  # type: ignore

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        raise NotImplementedError

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        if ProviderFeature.ARTIST_ALBUMS in self.supported_features:
            raise NotImplementedError
        return []

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of most popular tracks for the given artist."""
        if ProviderFeature.ARTIST_TOPTRACKS in self.supported_features:
            raise NotImplementedError
        return []

    async def get_album(self, prov_album_id: str) -> Album:  # type: ignore[return]
        """Get full album details by id."""
        if ProviderFeature.LIBRARY_ALBUMS in self.supported_features:
            raise NotImplementedError

    async def get_track(self, prov_track_id: str) -> Track:  # type: ignore[return]
        """Get full track details by id."""
        if ProviderFeature.LIBRARY_TRACKS in self.supported_features:
            raise NotImplementedError

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:  # type: ignore[return]
        """Get full playlist details by id."""
        if ProviderFeature.LIBRARY_PLAYLISTS in self.supported_features:
            raise NotImplementedError

    async def get_radio(self, prov_radio_id: str) -> Radio:  # type: ignore[return]
        """Get full radio details by id."""
        if ProviderFeature.LIBRARY_RADIOS in self.supported_features:
            raise NotImplementedError

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:  # type: ignore[return]
        """Get album tracks for given album id."""
        if ProviderFeature.LIBRARY_ALBUMS in self.supported_features:
            raise NotImplementedError

    async def get_playlist_tracks(  # type: ignore[return]
        self, prov_playlist_id: str
    ) -> AsyncGenerator[Track, None]:
        """Get all playlist tracks for given playlist id."""
        if ProviderFeature.LIBRARY_PLAYLISTS in self.supported_features:
            raise NotImplementedError
        yield  # type: ignore

    async def library_add(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Add item to provider's library. Return true on success."""
        if (
            media_type == MediaType.ARTIST
            and ProviderFeature.LIBRARY_ARTISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.ALBUM
            and ProviderFeature.LIBRARY_ALBUMS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.TRACK
            and ProviderFeature.LIBRARY_TRACKS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.PLAYLIST
            and ProviderFeature.LIBRARY_PLAYLISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.RADIO
            and ProviderFeature.LIBRARY_RADIOS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        self.logger.info(
            "Provider %s does not support library edit, "
            "the action will only be performed in the local database.",
            self.name,
        )
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on success."""
        if (
            media_type == MediaType.ARTIST
            and ProviderFeature.LIBRARY_ARTISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.ALBUM
            and ProviderFeature.LIBRARY_ALBUMS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.TRACK
            and ProviderFeature.LIBRARY_TRACKS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.PLAYLIST
            and ProviderFeature.LIBRARY_PLAYLISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.RADIO
            and ProviderFeature.LIBRARY_RADIOS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        self.logger.info(
            "Provider %s does not support library edit, "
            "the action will only be performed in the local database.",
            self.name,
        )
        return True

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        if ProviderFeature.PLAYLIST_TRACKS_EDIT in self.supported_features:
            raise NotImplementedError

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        if ProviderFeature.PLAYLIST_TRACKS_EDIT in self.supported_features:
            raise NotImplementedError

    async def create_playlist(self, name: str) -> Playlist:  # type: ignore[return]
        """Create a new playlist on provider with given name."""
        if ProviderFeature.PLAYLIST_CREATE in self.supported_features:
            raise NotImplementedError

    async def get_similar_tracks(  # type: ignore[return]
        self, prov_track_id: str, limit: int = 25
    ) -> list[Track]:
        """Retrieve a dynamic list of similar tracks based on the provided track."""
        if ProviderFeature.SIMILAR_TRACKS in self.supported_features:
            raise NotImplementedError

    async def get_stream_details(self, item_id: str) -> StreamDetails | None:
        """Get streamdetails for a track/radio."""
        raise NotImplementedError

    async def get_audio_stream(  # type: ignore[return]
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        if streamdetails.direct is None:
            raise NotImplementedError

    async def resolve_image(self, path: str) -> str | bytes | AsyncGenerator[bytes, None]:
        """
        Resolve an image from an image path.

        This either returns (a generator to get) raw bytes of the image or
        a string with an http(s) URL or local path that is accessible from the server.
        """
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
        """Browse this provider's items.

        :param path: The path to browse, (e.g. provid://artists).
        """
        if ProviderFeature.BROWSE not in self.supported_features:
            # we may NOT use the default implementation if the provider does not support browse
            raise NotImplementedError

        _, subpath = path.split("://")

        # this reference implementation can be overridden with a provider specific approach
        if not subpath:
            # return main listing
            root_items: list[BrowseFolder] = []
            if ProviderFeature.LIBRARY_ARTISTS in self.supported_features:
                root_items.append(
                    BrowseFolder(
                        item_id="artists",
                        provider=self.domain,
                        path=path + "artists",
                        name="",
                        label="artists",
                    )
                )
            if ProviderFeature.LIBRARY_ALBUMS in self.supported_features:
                root_items.append(
                    BrowseFolder(
                        item_id="albums",
                        provider=self.domain,
                        path=path + "albums",
                        name="",
                        label="albums",
                    )
                )
            if ProviderFeature.LIBRARY_TRACKS in self.supported_features:
                root_items.append(
                    BrowseFolder(
                        item_id="tracks",
                        provider=self.domain,
                        path=path + "tracks",
                        name="",
                        label="tracks",
                    )
                )
            if ProviderFeature.LIBRARY_PLAYLISTS in self.supported_features:
                root_items.append(
                    BrowseFolder(
                        item_id="playlists",
                        provider=self.domain,
                        path=path + "playlists",
                        name="",
                        label="playlists",
                    )
                )
            if ProviderFeature.LIBRARY_RADIOS in self.supported_features:
                root_items.append(
                    BrowseFolder(
                        item_id="radios",
                        provider=self.domain,
                        path=path + "radios",
                        name="",
                        label="radios",
                    )
                )
            return BrowseFolder(
                item_id="root",
                provider=self.domain,
                path=path,
                name=self.name,
                items=root_items,
            )
        # sublevel
        if subpath == "artists":
            return BrowseFolder(
                item_id="artists",
                provider=self.domain,
                path=path,
                name="",
                label="artists",
                items=[x async for x in self.get_library_artists()],
            )
        if subpath == "albums":
            return BrowseFolder(
                item_id="albums",
                provider=self.domain,
                path=path,
                name="",
                label="albums",
                items=[x async for x in self.get_library_albums()],
            )
        if subpath == "tracks":
            return BrowseFolder(
                item_id="tracks",
                provider=self.domain,
                path=path,
                name="",
                label="tracks",
                items=[x async for x in self.get_library_tracks()],
            )
        if subpath == "radios":
            return BrowseFolder(
                item_id="radios",
                provider=self.domain,
                path=path,
                name="",
                label="radios",
                items=[x async for x in self.get_library_radios()],
            )
        if subpath == "playlists":
            return BrowseFolder(
                item_id="playlists",
                provider=self.domain,
                path=path,
                name="",
                label="playlists",
                items=[x async for x in self.get_library_playlists()],
            )
        raise KeyError("Invalid subpath")

    async def recommendations(self) -> list[BrowseFolder]:
        """Get this provider's recommendations.

        Returns a list of BrowseFolder items with (max 25) mediaitems in the items attribute.
        """
        if ProviderFeature.RECOMMENDATIONS in self.supported_features:
            raise NotImplementedError
        return []

    async def sync_library(self, media_types: tuple[MediaType, ...]) -> None:
        """Run library sync for this provider."""
        # this reference implementation can be overridden
        # with a provider specific approach if needed
        for media_type in media_types:
            if not self.library_supported(media_type):
                continue
            self.logger.debug("Start sync of %s items.", media_type.value)
            controller = self.mass.music.get_controller(media_type)
            cur_db_ids = set()
            async for prov_item in self._get_library_gen(media_type):
                db_item = await controller.get_db_item_by_prov_mappings(
                    prov_item.provider_mappings,
                )
                if not db_item:
                    # create full db item
                    prov_item.in_library = True
                    db_item = await controller.add(prov_item, skip_metadata_lookup=True)
                elif (
                    db_item.metadata.checksum and prov_item.metadata.checksum
                ) and db_item.metadata.checksum != prov_item.metadata.checksum:
                    # existing dbitem checksum changed
                    db_item = await controller.update(db_item.item_id, prov_item)
                cur_db_ids.add(db_item.item_id)
                if not db_item.in_library:
                    await controller.set_db_library(db_item.item_id, True)

            # process deletions (= no longer in library)
            cache_key = f"db_items.{media_type}.{self.instance_id}"
            prev_db_items: list[int] | None
            if prev_db_items := await self.mass.cache.get(cache_key):
                for db_id in prev_db_items:
                    if db_id not in cur_db_ids:
                        # only mark the item as not in library and leave the metadata in db
                        await controller.set_db_library(db_id, False)
            await self.mass.cache.set(cache_key, list(cur_db_ids))

    # DO NOT OVERRIDE BELOW

    def library_supported(self, media_type: MediaType) -> bool:
        """Return if Library is supported for given MediaType on this provider."""
        if media_type == MediaType.ARTIST:
            return ProviderFeature.LIBRARY_ARTISTS in self.supported_features
        if media_type == MediaType.ALBUM:
            return ProviderFeature.LIBRARY_ALBUMS in self.supported_features
        if media_type == MediaType.TRACK:
            return ProviderFeature.LIBRARY_TRACKS in self.supported_features
        if media_type == MediaType.PLAYLIST:
            return ProviderFeature.LIBRARY_PLAYLISTS in self.supported_features
        if media_type == MediaType.RADIO:
            return ProviderFeature.LIBRARY_RADIOS in self.supported_features
        return False

    def library_edit_supported(self, media_type: MediaType) -> bool:
        """Return if Library add/remove is supported for given MediaType on this provider."""
        if media_type == MediaType.ARTIST:
            return ProviderFeature.LIBRARY_ARTISTS_EDIT in self.supported_features
        if media_type == MediaType.ALBUM:
            return ProviderFeature.LIBRARY_ALBUMS_EDIT in self.supported_features
        if media_type == MediaType.TRACK:
            return ProviderFeature.LIBRARY_TRACKS_EDIT in self.supported_features
        if media_type == MediaType.PLAYLIST:
            return ProviderFeature.LIBRARY_PLAYLISTS_EDIT in self.supported_features
        if media_type == MediaType.RADIO:
            return ProviderFeature.LIBRARY_RADIOS_EDIT in self.supported_features
        return False

    def _get_library_gen(self, media_type: MediaType) -> AsyncGenerator[MediaItemType, None]:
        """Return library generator for given media_type."""
        if media_type == MediaType.ARTIST:
            return self.get_library_artists()
        if media_type == MediaType.ALBUM:
            return self.get_library_albums()
        if media_type == MediaType.TRACK:
            return self.get_library_tracks()
        if media_type == MediaType.PLAYLIST:
            return self.get_library_playlists()
        if media_type == MediaType.RADIO:
            return self.get_library_radios()
        raise NotImplementedError
