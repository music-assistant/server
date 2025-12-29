"""Model/base for a Music Provider implementation."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final, cast

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Playlist,
    Podcast,
    PodcastEpisode,
    Radio,
    RecommendationFolder,
    SearchResults,
    Track,
)

from music_assistant.constants import (
    CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS,
    CONF_ENTRY_LIBRARY_SYNC_BACK,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLIST_TRACKS,
)

from .provider import Provider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.streamdetails import StreamDetails

CACHE_CATEGORY_PREV_LIBRARY_IDS: Final[int] = 1


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

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""

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
        yield  # type: ignore[misc]
        raise NotImplementedError

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        yield  # type: ignore[misc]
        raise NotImplementedError

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        yield  # type: ignore[misc]
        raise NotImplementedError

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve library/subscribed playlists from the provider."""
        yield  # type: ignore[misc]
        raise NotImplementedError

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        yield  # type: ignore[misc]
        raise NotImplementedError

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook, None]:
        """Retrieve library/subscribed audiobooks from the provider."""
        yield  # type: ignore[misc]
        raise NotImplementedError

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Retrieve library/subscribed podcasts from the provider."""
        yield  # type: ignore[misc]
        raise NotImplementedError

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        raise NotImplementedError

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist.

        Only called if provider supports ProviderFeature.ARTIST_ALBUMS.
        """
        raise NotImplementedError

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of most popular tracks for the given artist.

        Only called if provider supports ProviderFeature.ARTIST_TOPTRACKS.
        """
        raise NotImplementedError

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id.

        Only called if provider supports ProviderFeature.LIBRARY_ALBUMS.
        """
        raise NotImplementedError

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id.

        Only called if provider supports ProviderFeature.LIBRARY_TRACKS.
        """
        raise NotImplementedError

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id.

        Only called if provider supports ProviderFeature.LIBRARY_PLAYLISTS.
        """
        raise NotImplementedError

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id.

        Only called if provider supports ProviderFeature.LIBRARY_RADIOS.
        """
        raise NotImplementedError

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id.

        Only called if provider supports ProviderFeature.LIBRARY_AUDIOBOOKS.
        """
        raise NotImplementedError

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id.

        Only called if provider supports ProviderFeature.LIBRARY_PODCASTS.
        """
        raise NotImplementedError

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get (full) podcast episode details by id.

        Only called if provider supports ProviderFeature.LIBRARY_PODCASTS.
        """
        raise NotImplementedError

    async def get_album_tracks(
        self,
        prov_album_id: str,
    ) -> list[Track]:
        """Get album tracks for given album id.

        Only called if provider supports ProviderFeature.LIBRARY_ALBUMS.
        """
        raise NotImplementedError

    async def get_playlist_tracks(
        self,
        prov_playlist_id: str,
        page: int = 0,
    ) -> list[Track]:
        """Get all playlist tracks for given playlist id.

        Only called if provider supports ProviderFeature.LIBRARY_PLAYLISTS.
        """
        raise NotImplementedError

    async def get_podcast_episodes(
        self,
        prov_podcast_id: str,
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get all PodcastEpisodes for given podcast id.

        Only called if provider supports ProviderFeature.LIBRARY_PODCASTS.
        """
        yield  # type: ignore[misc]
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
        if (
            item.media_type == MediaType.AUDIOBOOK
            and ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            item.media_type == MediaType.PODCAST
            and ProviderFeature.LIBRARY_PODCASTS_EDIT in self.supported_features
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
        if (
            media_type == MediaType.AUDIOBOOK
            and ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.PODCAST
            and ProviderFeature.LIBRARY_PODCASTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        self.logger.info(
            "Provider %s does not support library edit, "
            "the action will only be performed in the local database.",
            self.name,
        )
        return True

    async def set_favorite(self, prov_item_id: str, media_type: MediaType, favorite: bool) -> None:
        """
        Set favorite status for item in provider's library.

        Only called if provider supports ProviderFeature.FAVORITE_*_EDIT.

        Note that this should only be implemented by a provider implementation if
        the provider differentiates between 'in library' and 'favorited' items.
        """
        if (
            media_type == MediaType.ARTIST
            and ProviderFeature.FAVORITE_ARTISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.ALBUM
            and ProviderFeature.FAVORITE_ALBUMS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.TRACK
            and ProviderFeature.FAVORITE_TRACKS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.PLAYLIST
            and ProviderFeature.FAVORITE_PLAYLISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.RADIO
            and ProviderFeature.FAVORITE_RADIOS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.AUDIOBOOK
            and ProviderFeature.FAVORITE_AUDIOBOOKS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.PODCAST
            and ProviderFeature.FAVORITE_PODCASTS_EDIT in self.supported_features
        ):
            raise NotImplementedError

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist.

        Only called if provider supports ProviderFeature.PLAYLIST_TRACKS_EDIT.
        """
        raise NotImplementedError

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist.

        Only called if provider supports ProviderFeature.PLAYLIST_TRACKS_EDIT.
        """
        raise NotImplementedError

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name.

        Only called if provider supports ProviderFeature.PLAYLIST_CREATE.
        """
        raise NotImplementedError

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of similar tracks based on the provided track.

        Only called if provider supports ProviderFeature.SIMILAR_TRACKS.
        """
        raise NotImplementedError

    async def get_resume_position(self, item_id: str, media_type: MediaType) -> tuple[bool, int]:
        """
        Get progress (resume point) details for the given Audiobook or Podcast episode.

        This is a separate call from the regular get_item call to ensure the resume position
        is always up-to-date and because a lot providers have this info present on a dedicated
        endpoint.

        Will be called right before playback starts to ensure the resume position is correct.

        Returns a boolean with the fully_played status
        and an integer with the resume position in ms.
        """
        raise NotImplementedError

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track/radio/chapter/episode."""
        raise NotImplementedError

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """
        Return the (custom) audio stream for the provider item.

        Will only be called when the stream_type is set to CUSTOM.
        """
        yield b""
        raise NotImplementedError

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

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """
        Handle callback when a (playable) media item has been played.

        This is called by the Queue controller when;
            - a track has been fully played
            - a track has been stopped (or skipped) after being played
            - every 30s when a track is playing

        Fully played is True when the track has been played to the end.

        Position is the last known position of the track in seconds, to sync resume state.
        When fully_played is set to false and position is 0,
        the user marked the item as unplayed in the UI.

        media_item is the full media item details of the played/playing track.

        is_playing is True when the track is currently playing.
        """

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
        if media_type == MediaType.AUDIOBOOK:
            return await self.get_audiobook(prov_item_id)
        if media_type == MediaType.PODCAST:
            return await self.get_podcast(prov_item_id)
        if media_type == MediaType.PODCAST_EPISODE:
            return await self.get_podcast_episode(prov_item_id)
        return await self.get_track(prov_item_id)

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:  # noqa: PLR0911
        """Browse this provider's items.

        :param path: The path to browse, (e.g. provider_id://artists).
        """
        if ProviderFeature.BROWSE not in self.supported_features:
            # we may NOT use the default implementation if the provider does not support browse
            raise NotImplementedError

        path_parts = path.split("://")[1].split("/")
        subpath = path_parts[0] if len(path_parts) > 0 else None
        sub_subpath = path_parts[1] if len(path_parts) > 1 else None
        # this reference implementation can be overridden with a provider specific approach
        if subpath == "artists":
            if artists := await self.mass.music.artists.library_items(
                provider=self.instance_id,
            ):
                return artists
            # library items not (yet) synced, fallback to direct retrieval
            return [x async for x in self.get_library_artists()]
        if subpath == "albums":
            if albums := await self.mass.music.albums.library_items(
                provider=self.instance_id,
            ):
                return albums
            # library items not (yet) synced, fallback to direct retrieval
            return [x async for x in self.get_library_albums()]
        if subpath == "tracks":
            if tracks := await self.mass.music.tracks.library_items(
                provider=self.instance_id,
            ):
                return tracks
            # library items not (yet) synced, fallback to direct retrieval
            return [x async for x in self.get_library_tracks()]
        if subpath == "radios":
            if radios := await self.mass.music.radio.library_items(
                provider=self.instance_id,
            ):
                return radios
            # library items not (yet) synced, fallback to direct retrieval
            return [x async for x in self.get_library_radios()]
        if subpath == "playlists":
            if playlists := await self.mass.music.playlists.library_items(
                provider=self.instance_id,
            ):
                return playlists
            # library items not (yet) synced, fallback to direct retrieval
            return [x async for x in self.get_library_playlists()]
        if subpath == "audiobooks":
            if audiobooks := await self.mass.music.audiobooks.library_items(
                provider=self.instance_id,
            ):
                return audiobooks
            # library items not (yet) synced, fallback to direct retrieval
            return [x async for x in self.get_library_audiobooks()]
        if subpath == "podcasts":
            if podcasts := await self.mass.music.podcasts.library_items(
                provider=self.instance_id,
            ):
                return podcasts
            # library items not (yet) synced, fallback to direct retrieval
            return [x async for x in self.get_library_podcasts()]
        if subpath == "recommendations" and sub_subpath:
            # recommendations contents listing
            recommendations = await self.recommendations()
            for rec in recommendations:
                if rec.item_id == sub_subpath:
                    return rec.items
        if subpath == "recommendations":
            # Main recommendations listing
            result: list[BrowseFolder] = []
            recommendations = await self.recommendations()
            for rec in recommendations:
                result.append(
                    BrowseFolder(
                        item_id=rec.item_id,
                        provider=self.instance_id,
                        name=rec.name,
                        is_playable=rec.is_playable,
                        image=rec.image,
                        path=f"{path}/{rec.item_id}",
                    )
                )
            return result

        if subpath:
            # unknown path
            msg = "Invalid subpath"
            raise KeyError(msg)

        # no subpath: return main listing
        folders: list[BrowseFolder] = []
        if ProviderFeature.LIBRARY_ARTISTS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="artists",
                    provider=self.instance_id,
                    path=path + "artists",
                    name="",
                    translation_key="artists",
                    is_playable=True,
                )
            )
        if ProviderFeature.LIBRARY_ALBUMS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="albums",
                    provider=self.instance_id,
                    path=path + "albums",
                    name="",
                    translation_key="albums",
                    is_playable=True,
                )
            )
        if ProviderFeature.LIBRARY_TRACKS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="tracks",
                    provider=self.domain,
                    path=path + "tracks",
                    name="",
                    translation_key="tracks",
                    is_playable=True,
                )
            )
        if ProviderFeature.LIBRARY_PLAYLISTS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="playlists",
                    provider=self.instance_id,
                    path=path + "playlists",
                    name="",
                    translation_key="playlists",
                    is_playable=True,
                )
            )
        if ProviderFeature.LIBRARY_RADIOS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="radios",
                    provider=self.instance_id,
                    path=path + "radios",
                    name="",
                    translation_key="radios",
                )
            )
        if ProviderFeature.LIBRARY_AUDIOBOOKS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="audiobooks",
                    provider=self.instance_id,
                    path=path + "audiobooks",
                    name="",
                    translation_key="audiobooks",
                )
            )
        if ProviderFeature.LIBRARY_PODCASTS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="podcasts",
                    provider=self.instance_id,
                    path=path + "podcasts",
                    name="",
                    translation_key="podcasts",
                )
            )
        if ProviderFeature.RECOMMENDATIONS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="recommendations",
                    provider=self.instance_id,
                    path=path + "recommendations",
                    name="",
                    translation_key="recommendations",
                )
            )
        if len(folders) == 1:
            # only one level, return the items directly
            return await self.browse(folders[0].path)
        return folders

    async def recommendations(self) -> list[RecommendationFolder]:
        """
        Get this provider's recommendations.

        Returns an actual (and often personalised) list of recommendations
        from this provider for the user/account.
        """
        if ProviderFeature.RECOMMENDATIONS in self.supported_features:
            raise NotImplementedError
        return []

    async def sync_library(self, media_type: MediaType) -> None:
        """Run library sync for this provider."""
        # this reference implementation may be overridden
        # with a provider specific approach if needed

        if not self.library_supported(media_type):
            raise UnsupportedFeaturedException("Library sync not supported for this media type")

        if media_type == MediaType.ARTIST:
            cur_db_ids = await self._sync_library_artists()
        elif media_type == MediaType.ALBUM:
            cur_db_ids = await self._sync_library_albums()
        elif media_type == MediaType.TRACK:
            cur_db_ids = await self._sync_library_tracks()
        elif media_type == MediaType.PLAYLIST:
            cur_db_ids = await self._sync_library_playlists()
        elif media_type == MediaType.PODCAST:
            cur_db_ids = await self._sync_library_podcasts()
        elif media_type == MediaType.RADIO:
            cur_db_ids = await self._sync_library_radios()
        elif media_type == MediaType.AUDIOBOOK:
            cur_db_ids = await self._sync_library_audiobooks()
        else:
            # this should not happen but catch it anyways
            raise UnsupportedFeaturedException(f"Unexpected media type to sync: {media_type}")

        # process deletions (= no longer in library)
        prev_library_items: list[int] | None
        controller = self.mass.music.get_controller(media_type)
        if prev_library_items := await self.mass.cache.get(
            key=media_type.value,
            provider=self.instance_id,
            category=CACHE_CATEGORY_PREV_LIBRARY_IDS,
        ):
            for db_id in prev_library_items:
                if db_id not in cur_db_ids:
                    try:
                        library_item = await controller.get_library_item(db_id)
                    except MediaNotFoundError:
                        # edge case: the item is (already) removed from MA library as well
                        continue
                    # check if we have other provider-mappings (marked as in-library)
                    remaining_providers_in_library = {
                        x.provider_instance
                        for x in library_item.provider_mappings
                        if x.provider_instance != self.instance_id and x.in_library
                    }
                    if not remaining_providers_in_library and library_item.favorite:
                        # unmark as favorite since no providers have it in library anymore
                        await controller.set_favorite(db_id, False)
                    # unmark this provider mapping as in_library = False
                    # we keep it in the library database so we can keep the metadata for future use
                    for prov_map in library_item.provider_mappings:
                        if prov_map.provider_instance == self.instance_id:
                            prov_map.in_library = False
                    await controller.set_provider_mappings(db_id, library_item.provider_mappings)
                    await asyncio.sleep(0)  # yield to eventloop
        # store current list of id's in cache so we can track changes
        await self.mass.cache.set(
            key=media_type.value,
            data=list(cur_db_ids),
            provider=self.instance_id,
            category=CACHE_CATEGORY_PREV_LIBRARY_IDS,
        )

    async def _sync_library_artists(self) -> set[int]:
        """Sync Library Artists to Music Assistant library."""
        self.logger.debug("Start sync of Artists to Music Assistant library.")
        cur_db_ids: set[int] = set()
        async for prov_item in self.get_library_artists():
            library_item = await self.mass.music.artists.get_library_item_by_prov_mappings(
                prov_item.provider_mappings,
            )
            try:
                if not library_item:
                    # add item to the library
                    for prov_map in prov_item.provider_mappings:
                        prov_map.in_library = True
                    library_item = await self.mass.music.artists.add_item_to_library(prov_item)
                elif not self._check_provider_mappings(library_item, prov_item, True):
                    # existing library item but provider mapping doesn't match
                    library_item = await self.mass.music.artists.update_item_in_library(
                        library_item.item_id, prov_item
                    )
                if not library_item.favorite and prov_item.favorite:
                    # existing library item not favorite but should be
                    await self.mass.music.artists.set_favorite(library_item.item_id, True)
                cur_db_ids.add(int(library_item.item_id))
                await asyncio.sleep(0)  # yield to eventloop
            except MusicAssistantError as err:
                self.logger.warning(
                    "Skipping sync of artist %s - error details: %s",
                    prov_item.uri,
                    str(err),
                )
        return cur_db_ids

    async def _sync_library_albums(self) -> set[int]:
        """Sync Library Albums to Music Assistant library."""
        self.logger.debug("Start sync of Albums to Music Assistant library.")
        cur_db_ids: set[int] = set()
        conf_sync_album_tracks = self.config.get_value(
            CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS.key,
            CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS.default_value,
        )
        sync_album_tracks = bool(conf_sync_album_tracks)
        async for prov_item in self.get_library_albums():
            library_item = await self.mass.music.albums.get_library_item_by_prov_mappings(
                prov_item.provider_mappings,
            )
            try:
                if not library_item:
                    # add item to the library
                    for prov_map in prov_item.provider_mappings:
                        prov_map.in_library = True
                    library_item = await self.mass.music.albums.add_item_to_library(prov_item)
                elif not self._check_provider_mappings(library_item, prov_item, True):
                    # existing library item but provider mapping doesn't match
                    library_item = await self.mass.music.albums.update_item_in_library(
                        library_item.item_id, prov_item
                    )
                if not library_item.favorite and prov_item.favorite:
                    # existing library item not favorite but should be
                    await self.mass.music.albums.set_favorite(library_item.item_id, True)
                cur_db_ids.add(int(library_item.item_id))
                await asyncio.sleep(0)  # yield to eventloop
                # optionally add album tracks to library
                if sync_album_tracks:
                    await self._sync_album_tracks(prov_item)
            except MusicAssistantError as err:
                self.logger.warning(
                    "Skipping sync of album %s - error details: %s",
                    prov_item.uri,
                    str(err),
                )
        return cur_db_ids

    async def _sync_album_tracks(self, provider_album: Album) -> None:
        """Sync Album Tracks to Music Assistant library."""
        self.logger.debug(
            "Start sync of Album Tracks to Music Assistant library for album %s.",
            provider_album.name,
        )
        for prov_track in await self.get_album_tracks(provider_album.item_id):
            library_track = await self.mass.music.tracks.get_library_item_by_prov_mappings(
                prov_track.provider_mappings,
            )
            try:
                if not library_track:
                    # add item to the library
                    for prov_map in prov_track.provider_mappings:
                        prov_map.in_library = True
                    library_track = await self.mass.music.tracks.add_item_to_library(prov_track)
                elif not self._check_provider_mappings(library_track, prov_track, True):
                    # existing library track but provider mapping doesn't match
                    library_track = await self.mass.music.tracks.update_item_in_library(
                        library_track.item_id, prov_track
                    )
                await asyncio.sleep(0)  # yield to eventloop
            except MusicAssistantError as err:
                self.logger.warning(
                    "Skipping sync of album track %s - error details: %s",
                    prov_track.uri,
                    str(err),
                )

    async def _sync_library_audiobooks(self) -> set[int]:
        """Sync Library Audiobooks to Music Assistant library."""
        self.logger.debug("Start sync of Audiobooks to Music Assistant library.")
        cur_db_ids: set[int] = set()
        async for prov_item in self.get_library_audiobooks():
            library_item = await self.mass.music.audiobooks.get_library_item_by_prov_mappings(
                prov_item.provider_mappings,
            )
            try:
                if not library_item:
                    # add item to the library
                    for prov_map in prov_item.provider_mappings:
                        prov_map.in_library = True
                    library_item = await self.mass.music.audiobooks.add_item_to_library(prov_item)
                elif not self._check_provider_mappings(library_item, prov_item, True):
                    # existing library item but provider mapping doesn't match
                    library_item = await self.mass.music.audiobooks.update_item_in_library(
                        library_item.item_id, prov_item
                    )
                if not library_item.favorite and prov_item.favorite:
                    # existing library item not favorite but should be
                    await self.mass.music.audiobooks.set_favorite(library_item.item_id, True)

                # check if resume_position_ms or fully_played changed
                if (
                    prov_item.resume_position_ms is not None
                    and prov_item.fully_played is not None
                    and (
                        library_item.resume_position_ms != prov_item.resume_position_ms
                        or library_item.fully_played != prov_item.fully_played
                    )
                ):
                    library_item = await self.mass.music.audiobooks.update_item_in_library(
                        library_item.item_id, prov_item
                    )

                cur_db_ids.add(int(library_item.item_id))
                await asyncio.sleep(0)  # yield to eventloop
            except MusicAssistantError as err:
                self.logger.warning(
                    "Skipping sync of audiobook %s - error details: %s",
                    prov_item.uri,
                    str(err),
                )
        return cur_db_ids

    async def _sync_library_playlists(self) -> set[int]:
        """Sync Library Playlists to Music Assistant library."""
        self.logger.debug("Start sync of Playlists to Music Assistant library.")
        conf_sync_playlist_tracks = self.config.get_value(
            CONF_ENTRY_LIBRARY_SYNC_PLAYLIST_TRACKS.key,
            CONF_ENTRY_LIBRARY_SYNC_PLAYLIST_TRACKS.default_value,
        )
        conf_sync_playlist_tracks = cast("list[str]", conf_sync_playlist_tracks)
        cur_db_ids: set[int] = set()
        async for prov_item in self.get_library_playlists():
            library_item = await self.mass.music.playlists.get_library_item_by_prov_mappings(
                prov_item.provider_mappings,
            )
            try:
                if not library_item:
                    # add item to the library
                    for prov_map in prov_item.provider_mappings:
                        prov_map.in_library = True
                    library_item = await self.mass.music.playlists.add_item_to_library(prov_item)
                elif not self._check_provider_mappings(library_item, prov_item, True):
                    # existing library item but provider mapping doesn't match
                    library_item = await self.mass.music.playlists.update_item_in_library(
                        library_item.item_id, prov_item
                    )
                if not library_item.favorite and prov_item.favorite:
                    # existing library item not favorite but should be
                    await self.mass.music.playlists.set_favorite(library_item.item_id, True)

                cur_db_ids.add(int(library_item.item_id))
                await asyncio.sleep(0)  # yield to eventloop
                # optionally sync playlist tracks
                if (
                    prov_item.name in conf_sync_playlist_tracks
                    or prov_item.uri in conf_sync_playlist_tracks
                ):
                    await self._sync_playlist_tracks(prov_item)
            except MusicAssistantError as err:
                self.logger.warning(
                    "Skipping sync of playlist %s - error details: %s",
                    prov_item.uri,
                    str(err),
                )
        return cur_db_ids

    async def _sync_playlist_tracks(self, provider_playlist: Playlist) -> None:
        """Sync Playlist Tracks to Music Assistant library."""
        self.logger.debug(
            "Start sync of Playlist Tracks to Music Assistant library for playlist %s.",
            provider_playlist.name,
        )
        async for prov_track in self.iter_playlist_tracks(provider_playlist.item_id):
            library_track = await self.mass.music.tracks.get_library_item_by_prov_mappings(
                prov_track.provider_mappings,
            )
            try:
                if not library_track:
                    # add item to the library
                    for prov_map in prov_track.provider_mappings:
                        prov_map.in_library = True
                    library_track = await self.mass.music.tracks.add_item_to_library(prov_track)
                elif not self._check_provider_mappings(library_track, prov_track, True):
                    # existing library track but provider mapping doesn't match
                    library_track = await self.mass.music.tracks.update_item_in_library(
                        library_track.item_id, prov_track
                    )
                await asyncio.sleep(0)  # yield to eventloop
            except MusicAssistantError as err:
                self.logger.warning(
                    "Skipping sync of album track %s - error details: %s",
                    prov_track.uri,
                    str(err),
                )

    async def _sync_library_tracks(self) -> set[int]:
        """Sync Library Tracks to Music Assistant library."""
        self.logger.debug("Start sync of Tracks to Music Assistant library.")
        cur_db_ids: set[int] = set()
        async for prov_item in self.get_library_tracks():
            library_item = await self.mass.music.tracks.get_library_item_by_prov_mappings(
                prov_item.provider_mappings,
            )
            try:
                if not library_item and not prov_item.available:
                    # skip unavailable tracks
                    # TODO: do we want to search for substitutes at this point ?
                    self.logger.debug(
                        "Skipping sync of track %s because it is unavailable",
                        prov_item.uri,
                    )
                    continue
                if not library_item:
                    # add item to the library
                    for prov_map in prov_item.provider_mappings:
                        prov_map.in_library = True
                    library_item = await self.mass.music.tracks.add_item_to_library(prov_item)
                elif not self._check_provider_mappings(library_item, prov_item, True):
                    # existing library item but provider mapping doesn't match
                    library_item = await self.mass.music.tracks.update_item_in_library(
                        library_item.item_id, prov_item
                    )
                if not library_item.favorite and prov_item.favorite:
                    # existing library item not favorite but should be
                    await self.mass.music.tracks.set_favorite(library_item.item_id, True)

                cur_db_ids.add(int(library_item.item_id))
                await asyncio.sleep(0)  # yield to eventloop
            except MusicAssistantError as err:
                self.logger.warning(
                    "Skipping sync of track %s - error details: %s",
                    prov_item.uri,
                    str(err),
                )
        return cur_db_ids

    async def _sync_library_podcasts(self) -> set[int]:
        """Sync Library Podcasts to Music Assistant library."""
        self.logger.debug("Start sync of Podcasts to Music Assistant library.")
        cur_db_ids: set[int] = set()
        async for prov_item in self.get_library_podcasts():
            library_item = await self.mass.music.podcasts.get_library_item_by_prov_mappings(
                prov_item.provider_mappings,
            )
            try:
                if not library_item:
                    # add item to the library
                    for prov_map in prov_item.provider_mappings:
                        prov_map.in_library = True
                    library_item = await self.mass.music.podcasts.add_item_to_library(prov_item)
                elif not self._check_provider_mappings(library_item, prov_item, True):
                    # existing library item but provider mapping doesn't match
                    library_item = await self.mass.music.podcasts.update_item_in_library(
                        library_item.item_id, prov_item
                    )
                if not library_item.favorite and prov_item.favorite:
                    # existing library item not favorite but should be
                    await self.mass.music.podcasts.set_favorite(library_item.item_id, True)

                cur_db_ids.add(int(library_item.item_id))
                await asyncio.sleep(0)  # yield to eventloop

                # precache podcast episodes
                async for _ in self.mass.music.podcasts.episodes(
                    library_item.item_id, library_item.provider
                ):
                    await asyncio.sleep(0)  # yield to eventloop
            except MusicAssistantError as err:
                self.logger.warning(
                    "Skipping sync of podcast %s - error details: %s",
                    prov_item.uri,
                    str(err),
                )
        return cur_db_ids

    async def _sync_library_radios(self) -> set[int]:
        """Sync Library Radios to Music Assistant library."""
        self.logger.debug("Start sync of Radios to Music Assistant library.")
        cur_db_ids: set[int] = set()
        async for prov_item in self.get_library_radios():
            library_item = await self.mass.music.radio.get_library_item_by_prov_mappings(
                prov_item.provider_mappings,
            )
            try:
                if not library_item:
                    # add item to the library
                    for prov_map in prov_item.provider_mappings:
                        prov_map.in_library = True
                    library_item = await self.mass.music.radio.add_item_to_library(prov_item)
                elif not self._check_provider_mappings(library_item, prov_item, True):
                    # existing library item but provider mapping doesn't match
                    library_item = await self.mass.music.radio.update_item_in_library(
                        library_item.item_id, prov_item
                    )
                if not library_item.favorite and prov_item.favorite:
                    # existing library item not favorite but should be
                    await self.mass.music.radio.set_favorite(library_item.item_id, True)

                cur_db_ids.add(int(library_item.item_id))
                await asyncio.sleep(0)  # yield to eventloop

            except MusicAssistantError as err:
                self.logger.warning(
                    "Skipping sync of Radio %s - error details: %s",
                    prov_item.uri,
                    str(err),
                )
        return cur_db_ids

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
        if media_type == MediaType.AUDIOBOOK:
            return ProviderFeature.LIBRARY_AUDIOBOOKS in self.supported_features
        if media_type == MediaType.PODCAST:
            return ProviderFeature.LIBRARY_PODCASTS in self.supported_features
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
        if media_type == MediaType.AUDIOBOOK:
            return ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT in self.supported_features
        if media_type == MediaType.PODCAST:
            return ProviderFeature.LIBRARY_PODCASTS_EDIT in self.supported_features
        return False

    def library_sync_back_enabled(self, media_type: MediaType) -> bool:
        """Return if Library sync back is enabled for given MediaType on this provider."""
        conf_value = self.config.get_value(
            CONF_ENTRY_LIBRARY_SYNC_BACK.key, CONF_ENTRY_LIBRARY_SYNC_BACK.default_value
        )
        return bool(conf_value)

    def library_favorites_edit_supported(self, media_type: MediaType) -> bool:
        """Return if favorites add/remove is supported for given MediaType on this provider."""
        if media_type == MediaType.ARTIST:
            return ProviderFeature.FAVORITE_ARTISTS_EDIT in self.supported_features
        if media_type == MediaType.ALBUM:
            return ProviderFeature.FAVORITE_ALBUMS_EDIT in self.supported_features
        if media_type == MediaType.TRACK:
            return ProviderFeature.FAVORITE_TRACKS_EDIT in self.supported_features
        if media_type == MediaType.PLAYLIST:
            return ProviderFeature.FAVORITE_PLAYLISTS_EDIT in self.supported_features
        if media_type == MediaType.RADIO:
            return ProviderFeature.FAVORITE_RADIOS_EDIT in self.supported_features
        if media_type == MediaType.AUDIOBOOK:
            return ProviderFeature.FAVORITE_AUDIOBOOKS_EDIT in self.supported_features
        if media_type == MediaType.PODCAST:
            return ProviderFeature.FAVORITE_PODCASTS_EDIT in self.supported_features
        return False

    async def iter_playlist_tracks(
        self,
        prov_playlist_id: str,
    ) -> AsyncGenerator[Track, None]:
        """Iterate playlist tracks for the given provider playlist id."""
        page = 0
        while True:
            tracks = await self.get_playlist_tracks(
                prov_playlist_id,
                page=page,
            )
            if not tracks:
                break
            for track in tracks:
                yield track
            page += 1

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
        if media_type == MediaType.AUDIOBOOK:
            return self.get_library_audiobooks()
        if media_type == MediaType.PODCAST:
            return self.get_library_podcasts()
        raise NotImplementedError

    def _check_provider_mappings(
        self, library_item: MediaItemType, provider_item: MediaItemType, in_library: bool
    ) -> bool:
        """Check if provider mapping(s) are consistent between library and provider items."""
        for provider_mapping in provider_item.provider_mappings:
            if provider_mapping.item_id != provider_item.item_id:
                # this should never happen, but guard against it
                raise MusicAssistantError("Inconsistent provider mapping item_id found")
            if provider_mapping.provider_instance != self.instance_id:
                # this should never happen, but guard against it
                raise MusicAssistantError("Inconsistent provider mapping instance_id found")
            # check if the provider mapping matches the library item
            provider_mapping.in_library = in_library
            library_mapping = next(
                (
                    x
                    for x in library_item.provider_mappings
                    if x.provider_instance == provider_mapping.provider_instance
                    and x.item_id == provider_mapping.item_id
                ),
                None,
            )
            if not library_mapping:
                return False
            if provider_mapping.in_library != library_mapping.in_library:
                # in-library status doesn't match
                return False
            if provider_mapping.is_unique != library_mapping.is_unique:
                # unique status doesn't match
                return False
            # check if the library item has all provider instances mappings
            is_unique = provider_mapping.is_unique or (not self.is_streaming_provider)
            if not is_unique:
                # for streaming providers we need to make sure all provider instances
                # for this domain are represented in the provider mappings
                prov_instances = self.mass.music.get_provider_instances(
                    domain=provider_mapping.provider_domain,
                    return_unavailable=True,
                )
                if len(prov_instances) > 1:
                    # multiple provider instances for this domain exist
                    # make sure the library item has all provider mappings
                    for prov_instance in prov_instances:
                        if not any(
                            x.provider_instance == prov_instance.instance_id
                            and x.item_id == provider_mapping.item_id
                            for x in library_item.provider_mappings
                        ):
                            # missing provider mapping for another instance
                            # the rest of the core logic will take care of adding it
                            # just return False here to trigger that logic
                            return False

            # final check: availability
            return provider_mapping.available == library_mapping.available
        return False
