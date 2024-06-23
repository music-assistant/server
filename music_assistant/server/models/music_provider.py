"""Model/base for a Music Provider implementation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant.common.models.enums import MediaType, ProviderFeature
from music_assistant.common.models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    BrowseFolder,
    MediaItemType,
    Playlist,
    Radio,
    SearchResults,
    Track,
)
from music_assistant.common.models.streamdetails import StreamDetails

from .provider import Provider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# ruff: noqa: ARG001, ARG002


class MusicProvider(Provider):
    """Base representation of a Music Provider (controller).

    Music Provider implementations should inherit from this base model.
    """

    @property
    def is_streaming_provider(self) -> bool:
        """
        Return True if the provider is a streaming provider.

        This literally means that the catalog is not the same as the library contents.
        For local based providers (files, plex), the catalog is the same as the library content.
        It also means that data is if this provider is NOT a streaming provider,
        data cross instances is unique, the catalog and library differs per instance.

        Setting this to True will only query one instance of the provider for search and lookups.
        Setting this to False will query all instances of this provider for search and lookups.
        """
        return True

    @property
    def lookup_key(self) -> str:
        """Return domain if (multi-instance) streaming_provider or instance_id otherwise."""
        if self.is_streaming_provider or not self.manifest.multi_instance:
            return self.domain
        return self.instance_id

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

    async def get_album_tracks(
        self,
        prov_album_id: str,  # type: ignore[return]
    ) -> list[Track]:
        """Get album tracks for given album id."""
        if ProviderFeature.LIBRARY_ALBUMS in self.supported_features:
            raise NotImplementedError

    async def get_playlist_tracks(
        self, prov_playlist_id: str, offset: int, limit: int
    ) -> list[Track]:
        """Get all playlist tracks for given playlist id."""
        if ProviderFeature.LIBRARY_PLAYLISTS in self.supported_features:
            raise NotImplementedError

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to provider's library. Return true on success."""
        if (
            item.media_type == MediaType.ARTIST
            and ProviderFeature.LIBRARY_ARTISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            item.media_type == MediaType.ALBUM
            and ProviderFeature.LIBRARY_ALBUMS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            item.media_type == MediaType.TRACK
            and ProviderFeature.LIBRARY_TRACKS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            item.media_type == MediaType.PLAYLIST
            and ProviderFeature.LIBRARY_PLAYLISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            item.media_type == MediaType.RADIO
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

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        raise NotImplementedError

    async def get_audio_stream(  # type: ignore[return]
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """
        Return the (custom) audio stream for the provider item.

        Will only be called when the stream_type is set to CUSTOM.
        """
        raise NotImplementedError

    async def on_streamed(self, streamdetails: StreamDetails, seconds_streamed: int) -> None:
        """Handle callback when an item completed streaming."""

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Resolve an image from an image path.

        This either returns (a generator to get) raw bytes of the image or
        a string with an http(s) URL or local path that is accessible from the server.
        """
        return path

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

    async def browse(self, path: str, offset: int, limit: int) -> list[MediaItemType]:
        """Browse this provider's items.

        :param path: The path to browse, (e.g. provider_id://artists).
        """
        if ProviderFeature.BROWSE not in self.supported_features:
            # we may NOT use the default implementation if the provider does not support browse
            raise NotImplementedError

        subpath = path.split("://", 1)[1]
        # this reference implementation can be overridden with a provider specific approach
        if subpath == "artists":
            return await self.mass.music.artists.library_items(
                limit=limit, offset=offset, provider=self.instance_id
            )
        if subpath == "albums":
            return await self.mass.music.albums.library_items(
                limit=limit, offset=offset, provider=self.instance_id
            )
        if subpath == "tracks":
            return await self.mass.music.tracks.library_items(
                limit=limit, offset=offset, provider=self.instance_id
            )
        if subpath == "radios":
            return await self.mass.music.radio.library_items(
                limit=limit, offset=offset, provider=self.instance_id
            )
        if subpath == "playlists":
            return await self.mass.music.playlists.library_items(
                limit=limit, offset=offset, provider=self.instance_id
            )
        if subpath:
            # unknown path
            msg = "Invalid subpath"
            raise KeyError(msg)

        # no subpath: return main listing
        items: list[MediaItemType] = []
        if ProviderFeature.LIBRARY_ARTISTS in self.supported_features:
            items.append(
                BrowseFolder(
                    item_id="artists",
                    provider=self.domain,
                    path=path + "artists",
                    name="",
                    label="artists",
                )
            )
        if ProviderFeature.LIBRARY_ALBUMS in self.supported_features:
            items.append(
                BrowseFolder(
                    item_id="albums",
                    provider=self.domain,
                    path=path + "albums",
                    name="",
                    label="albums",
                )
            )
        if ProviderFeature.LIBRARY_TRACKS in self.supported_features:
            items.append(
                BrowseFolder(
                    item_id="tracks",
                    provider=self.domain,
                    path=path + "tracks",
                    name="",
                    label="tracks",
                )
            )
        if ProviderFeature.LIBRARY_PLAYLISTS in self.supported_features:
            items.append(
                BrowseFolder(
                    item_id="playlists",
                    provider=self.domain,
                    path=path + "playlists",
                    name="",
                    label="playlists",
                )
            )
        if ProviderFeature.LIBRARY_RADIOS in self.supported_features:
            items.append(
                BrowseFolder(
                    item_id="radios",
                    provider=self.domain,
                    path=path + "radios",
                    name="",
                    label="radios",
                )
            )
        return items

    async def recommendations(self) -> list[MediaItemType]:
        """Get this provider's recommendations.

        Returns a actual and personalised list of Media items with recommendations
        form this provider for the user/account. It may return nested levels with
        BrowseFolder items.
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
                library_item = await controller.get_library_item_by_prov_mappings(
                    prov_item.provider_mappings,
                )
                try:
                    if not library_item and not prov_item.available:
                        # skip unavailable tracks
                        self.logger.debug(
                            "Skipping sync of item %s because it is unavailable", prov_item.uri
                        )
                        continue
                    if not library_item:
                        # create full db item
                        # note that we skip the metadata lookup purely to speed up the sync
                        # the additional metadata is then lazy retrieved afterwards
                        if self.is_streaming_provider:
                            prov_item.favorite = True
                        library_item = await controller.add_item_to_library(
                            prov_item, metadata_lookup=False
                        )
                    elif library_item.metadata.cache_checksum != prov_item.metadata.cache_checksum:
                        # existing dbitem checksum changed
                        library_item = await controller.update_item_in_library(
                            library_item.item_id, prov_item
                        )
                    cur_db_ids.add(library_item.item_id)
                    await asyncio.sleep(0)  # yield to eventloop
                except MusicAssistantError as err:
                    self.logger.warning(
                        "Skipping sync of item %s - error details: %s", prov_item.uri, str(err)
                    )

            # process deletions (= no longer in library)
            cache_key = f"library_items.{media_type}.{self.instance_id}"
            prev_library_items: list[int] | None
            if prev_library_items := await self.mass.cache.get(cache_key):
                for db_id in prev_library_items:
                    if db_id not in cur_db_ids:
                        try:
                            item = await controller.get_library_item(db_id)
                        except MediaNotFoundError:
                            # edge case: the item is already removed
                            continue
                        remaining_providers = {
                            x.provider_domain
                            for x in item.provider_mappings
                            if x.provider_domain != self.domain
                        }
                        if not remaining_providers and media_type != MediaType.ARTIST:
                            # this item is removed from the provider's library
                            # and we have no other providers attached to it
                            # it is safe to remove it from the MA library too
                            # note we skip artists here to prevent a recursive removal
                            # of all albums and tracks underneath this artist
                            await controller.remove_item_from_library(db_id)
                        else:
                            # otherwise: just unmark favorite
                            await controller.set_favorite(db_id, False)
                await asyncio.sleep(0)  # yield to eventloop
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
