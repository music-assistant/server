"""KION Music provider implementation."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Playlist,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .api_client import KionMusicClient
from .constants import (
    BROWSE_INITIAL_TRACKS,
    BROWSE_NAMES_EN,
    BROWSE_NAMES_RU,
    CONF_BASE_URL,
    CONF_TOKEN,
    DEFAULT_BASE_URL,
    DISCOVERY_INITIAL_TRACKS,
    MY_MIX_BATCH_SIZE,
    MY_MIX_MAX_TRACKS,
    MY_MIX_PLAYLIST_ID,
    PLAYLIST_ID_SPLITTER,
    RADIO_TRACK_ID_SEP,
    ROTOR_STATION_MY_MIX,
    TRACK_BATCH_SIZE,
)
from .parsers import parse_album, parse_artist, parse_playlist, parse_track
from .streaming import KionMusicStreamingManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.streamdetails import StreamDetails


def _parse_radio_item_id(item_id: str) -> tuple[str, str | None]:
    """Extract track_id and optional station_id from provider item_id.

    My Mix tracks use item_id format 'track_id@station_id'. Other tracks use
    plain track_id.

    :param item_id: Provider item_id (may contain RADIO_TRACK_ID_SEP).
    :return: (track_id, station_id or None).
    """
    if RADIO_TRACK_ID_SEP in item_id:
        parts = item_id.split(RADIO_TRACK_ID_SEP, 1)
        return (parts[0], parts[1] if len(parts) > 1 else None)
    return (item_id, None)


class KionMusicProvider(MusicProvider):
    """Implementation of a KION Music MusicProvider."""

    _client: KionMusicClient | None = None
    _streaming: KionMusicStreamingManager | None = None
    _my_mix_batch_id: str | None = None
    _my_mix_last_track_id: str | None = None  # last track id for "Load more" (API queue param)
    _my_mix_playlist_next_cursor: str | None = None  # first_track_id for next playlist page
    _my_mix_radio_started_sent: bool = False
    _my_mix_seen_track_ids: set[str]  # Track IDs seen in current My Mix session

    @property
    def client(self) -> KionMusicClient:
        """Return the KION Music client."""
        if self._client is None:
            raise ProviderUnavailableError("Provider not initialized")
        return self._client

    @property
    def streaming(self) -> KionMusicStreamingManager:
        """Return the streaming manager."""
        if self._streaming is None:
            raise ProviderUnavailableError("Provider not initialized")
        return self._streaming

    def _get_browse_names(self) -> dict[str, str]:
        """Get locale-based browse folder names."""
        try:
            locale = (self.mass.metadata.locale or "en_US").lower()
            use_russian = locale.startswith("ru")
        except Exception:
            use_russian = False
        return BROWSE_NAMES_RU if use_russian else BROWSE_NAMES_EN

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        token = self.config.get_value(CONF_TOKEN)
        if not token:
            raise LoginFailed("No KION Music token provided")

        base_url = self.config.get_value(CONF_BASE_URL, DEFAULT_BASE_URL)
        self._client = KionMusicClient(str(token), base_url=str(base_url))
        await self._client.connect()
        # Suppress yandex_music library DEBUG dumps (full API request/response JSON)
        logging.getLogger("yandex_music").setLevel(self.logger.level + 10)
        self._streaming = KionMusicStreamingManager(self)
        # Initialize My Mix duplicate tracking
        self._my_mix_seen_track_ids = set()
        self.logger.info("Successfully connected to KION Music")

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider.

        :param is_removed: Whether the provider is being removed.
        """
        if self._client:
            await self._client.disconnect()
        self._client = None
        self._streaming = None
        await super().unload(is_removed)

    def get_item_mapping(self, media_type: MediaType | str, key: str, name: str) -> ItemMapping:
        """Create a generic item mapping.

        :param media_type: The media type.
        :param key: The item ID.
        :param name: The item name.
        :return: An ItemMapping instance.
        """
        if isinstance(media_type, str):
            media_type = MediaType(media_type)
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.instance_id,
            name=name,
        )

    async def _fetch_my_mix_tracks(
        self,
        *,
        max_tracks: int = MY_MIX_MAX_TRACKS,
        max_batches: int = MY_MIX_BATCH_SIZE,
        initial_queue: str | int | None = None,
        seen_track_ids: set[str] | None = None,
    ) -> tuple[list[Track], str | None, str | None, set[str]]:
        """Fetch My Mix tracks with de-duplication and radio feedback.

        :param max_tracks: Maximum number of tracks to return.
        :param max_batches: Maximum number of API batch calls.
        :param initial_queue: Optional track ID for API pagination.
        :param seen_track_ids: Already-seen track IDs for de-duplication.
        :return: (tracks, last_batch_id, last_first_track_id, updated_seen_ids).
        """
        if seen_track_ids is None:
            seen_track_ids = set()

        tracks: list[Track] = []
        last_batch_id: str | None = None
        last_first_track_id: str | None = None
        queue: str | int | None = initial_queue

        for _ in range(max_batches):
            if len(tracks) >= max_tracks:
                break

            yandex_tracks, batch_id = await self.client.get_my_mix_tracks(queue=queue)
            if batch_id:
                self._my_mix_batch_id = batch_id
                last_batch_id = batch_id
            if not self._my_mix_radio_started_sent and yandex_tracks:
                self._my_mix_radio_started_sent = True
                await self.client.send_rotor_station_feedback(
                    ROTOR_STATION_MY_MIX,
                    "radioStarted",
                    batch_id=batch_id,
                )
            first_track_id_this_batch: str | None = None
            for yt in yandex_tracks:
                if len(tracks) >= max_tracks:
                    break
                try:
                    t = parse_track(self, yt)
                    track_id = (
                        str(yt.id) if hasattr(yt, "id") and yt.id else getattr(yt, "track_id", None)
                    )
                    if track_id:
                        if track_id in seen_track_ids:
                            self.logger.debug("Skipping duplicate My Mix track: %s", track_id)
                            continue
                        seen_track_ids.add(track_id)
                        if first_track_id_this_batch is None:
                            first_track_id_this_batch = track_id
                        t.item_id = f"{track_id}{RADIO_TRACK_ID_SEP}{ROTOR_STATION_MY_MIX}"
                        for pm in t.provider_mappings:
                            if pm.provider_instance == self.instance_id:
                                pm.item_id = t.item_id
                                break
                    tracks.append(t)
                except InvalidDataError as err:
                    self.logger.debug("Error parsing My Mix track: %s", err)
            if first_track_id_this_batch is not None:
                last_first_track_id = first_track_id_this_batch
            if not batch_id or not yandex_tracks or len(tracks) >= max_tracks:
                break
            queue = first_track_id_this_batch

        return (tracks, last_batch_id, last_first_track_id, seen_track_ids)

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse provider items with locale-based folder names and My Mix.

        Root level shows My Mix, artists, albums, liked tracks, playlists. Names
        are in Russian when MA locale is ru_*, otherwise in English. My Mix
        tracks use item_id format track_id@station_id for rotor feedback.

        :param path: The path to browse (e.g. provider_id:// or provider_id://artists).
        """
        if ProviderFeature.BROWSE not in self.supported_features:
            raise NotImplementedError

        path_parts = path.split("://")[1].split("/") if "://" in path else []
        subpath = path_parts[0] if len(path_parts) > 0 else None
        sub_subpath = path_parts[1] if len(path_parts) > 1 else None

        if subpath == MY_MIX_PLAYLIST_ID:
            max_batches = MY_MIX_BATCH_SIZE if sub_subpath != "next" else 1

            if sub_subpath != "next":
                self._my_mix_seen_track_ids = set()

            queue: str | int | None = None
            if sub_subpath == "next":
                queue = self._my_mix_last_track_id
            elif sub_subpath:
                queue = sub_subpath

            (
                fetched,
                last_batch_id,
                last_first_track_id,
                self._my_mix_seen_track_ids,
            ) = await self._fetch_my_mix_tracks(
                max_batches=max_batches,
                initial_queue=queue,
                seen_track_ids=self._my_mix_seen_track_ids,
            )
            if last_first_track_id is not None:
                self._my_mix_last_track_id = last_first_track_id

            all_tracks: list[Track | BrowseFolder] = list(fetched)

            # Apply initial tracks limit if not in "load more" mode
            if sub_subpath != "next":
                if len(all_tracks) > BROWSE_INITIAL_TRACKS:
                    all_tracks = all_tracks[:BROWSE_INITIAL_TRACKS]

            # Only show "Load more" if we haven't reached the limit and there's more data
            if last_batch_id and len(fetched) < MY_MIX_MAX_TRACKS:
                names = self._get_browse_names()
                next_name = "Ещё" if names == BROWSE_NAMES_RU else "Load more"
                all_tracks.append(
                    BrowseFolder(
                        item_id="next",
                        provider=self.instance_id,
                        path=f"{path.rstrip('/')}/next",
                        name=next_name,
                        is_playable=False,
                    )
                )
            return all_tracks

        if subpath:
            return await super().browse(path)

        names = self._get_browse_names()

        folders: list[BrowseFolder] = []
        base = path if path.endswith("//") else path.rstrip("/") + "/"
        folders.append(
            BrowseFolder(
                item_id=MY_MIX_PLAYLIST_ID,
                provider=self.instance_id,
                path=f"{base}{MY_MIX_PLAYLIST_ID}",
                name=names[MY_MIX_PLAYLIST_ID],
                is_playable=True,
            )
        )
        if ProviderFeature.LIBRARY_ARTISTS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="artists",
                    provider=self.instance_id,
                    path=f"{base}artists",
                    name=names["artists"],
                    is_playable=True,
                )
            )
        if ProviderFeature.LIBRARY_ALBUMS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="albums",
                    provider=self.instance_id,
                    path=f"{base}albums",
                    name=names["albums"],
                    is_playable=True,
                )
            )
        if ProviderFeature.LIBRARY_TRACKS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="tracks",
                    provider=self.instance_id,
                    path=f"{base}tracks",
                    name=names["tracks"],
                    is_playable=True,
                )
            )
        if ProviderFeature.LIBRARY_PLAYLISTS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="playlists",
                    provider=self.instance_id,
                    path=f"{base}playlists",
                    name=names["playlists"],
                    is_playable=True,
                )
            )
        if len(folders) == 1:
            return await self.browse(folders[0].path)
        return folders

    # Search

    @use_cache(3600 * 24 * 14)
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Perform search on KION Music.

        :param search_query: The search query.
        :param media_types: List of media types to search for.
        :param limit: Maximum number of results per type.
        :return: SearchResults with found items.
        """
        result = SearchResults()

        # Determine search type based on requested media types
        # Map MediaType to KION API search type
        type_mapping = {
            MediaType.TRACK: "track",
            MediaType.ALBUM: "album",
            MediaType.ARTIST: "artist",
            MediaType.PLAYLIST: "playlist",
        }
        requested_types = [type_mapping[mt] for mt in media_types if mt in type_mapping]

        # Use specific type if only one requested, otherwise search all
        search_type = requested_types[0] if len(requested_types) == 1 else "all"

        search_result = await self.client.search(search_query, search_type=search_type, limit=limit)
        if not search_result:
            return result

        # Parse tracks
        if MediaType.TRACK in media_types and search_result.tracks:
            for track in search_result.tracks.results[:limit]:
                try:
                    result.tracks = [*result.tracks, parse_track(self, track)]
                except InvalidDataError as err:
                    self.logger.debug("Error parsing track: %s", err)

        # Parse albums
        if MediaType.ALBUM in media_types and search_result.albums:
            for album in search_result.albums.results[:limit]:
                try:
                    result.albums = [*result.albums, parse_album(self, album)]
                except InvalidDataError as err:
                    self.logger.debug("Error parsing album: %s", err)

        # Parse artists
        if MediaType.ARTIST in media_types and search_result.artists:
            for artist in search_result.artists.results[:limit]:
                try:
                    result.artists = [*result.artists, parse_artist(self, artist)]
                except InvalidDataError as err:
                    self.logger.debug("Error parsing artist: %s", err)

        # Parse playlists
        if MediaType.PLAYLIST in media_types and search_result.playlists:
            for playlist in search_result.playlists.results[:limit]:
                try:
                    result.playlists = [*result.playlists, parse_playlist(self, playlist)]
                except InvalidDataError as err:
                    self.logger.debug("Error parsing playlist: %s", err)

        return result

    # Get single items

    @use_cache(3600 * 24 * 30)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details by ID.

        :param prov_artist_id: The provider artist ID.
        :return: Artist object.
        :raises MediaNotFoundError: If artist not found.
        """
        artist = await self.client.get_artist(prov_artist_id)
        if not artist:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
        return parse_artist(self, artist)

    @use_cache(3600 * 24 * 30)
    async def get_album(self, prov_album_id: str) -> Album:
        """Get album details by ID.

        :param prov_album_id: The provider album ID.
        :return: Album object.
        :raises MediaNotFoundError: If album not found.
        """
        album = await self.client.get_album(prov_album_id)
        if not album:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        return parse_album(self, album)

    async def get_track(self, prov_track_id: str) -> Track:
        """Get track details by ID.

        Supports composite item_id (track_id@station_id) for My Mix tracks;
        only the track_id part is used for the API. Normalizes the ID before
        caching so that "12345" and "12345@user:onyourwave" share one cache entry.

        :param prov_track_id: The provider track ID (or track_id@station_id).
        :return: Track object.
        :raises MediaNotFoundError: If track not found.
        """
        track_id, _ = _parse_radio_item_id(prov_track_id)
        return await self._get_track_cached(track_id)

    @use_cache(3600 * 24 * 30)
    async def _get_track_cached(self, track_id: str) -> Track:
        """Fetch and cache track details by normalized track ID.

        :param track_id: Plain track ID (no station suffix).
        :return: Track object.
        :raises MediaNotFoundError: If track not found.
        """
        yandex_track = await self.client.get_track(track_id)
        if not yandex_track:
            raise MediaNotFoundError(f"Track {track_id} not found")
        return parse_track(self, yandex_track)

    @use_cache(3600 * 24 * 30)
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details by ID.

        Supports virtual playlist MY_MIX_PLAYLIST_ID (My Mix). Real playlists
        use format "owner_id:kind".

        :param prov_playlist_id: The provider playlist ID (format: "owner_id:kind" or my_mix).
        :return: Playlist object.
        :raises MediaNotFoundError: If playlist not found.
        """
        if prov_playlist_id == MY_MIX_PLAYLIST_ID:
            names = self._get_browse_names()
            return Playlist(
                item_id=MY_MIX_PLAYLIST_ID,
                provider=self.instance_id,
                name=names[MY_MIX_PLAYLIST_ID],
                owner="KION Music",
                provider_mappings={
                    ProviderMapping(
                        item_id=MY_MIX_PLAYLIST_ID,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        is_unique=True,
                    )
                },
                is_editable=False,
            )

        # Parse the playlist ID (format: owner_id:kind)
        if PLAYLIST_ID_SPLITTER in prov_playlist_id:
            owner_id, kind = prov_playlist_id.split(PLAYLIST_ID_SPLITTER, 1)
        else:
            owner_id = str(self.client.user_id)
            kind = prov_playlist_id

        playlist = await self.client.get_playlist(owner_id, kind)
        if not playlist:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found")
        return parse_playlist(self, playlist)

    async def _get_my_mix_playlist_tracks(self, page: int) -> list[Track]:
        """Get My Mix tracks for virtual playlist (uncached; uses cursor for page > 0).

        :param page: Page number (0 = first batch, 1+ = next batches via queue cursor).
        :return: List of Track objects for this page.
        """
        if page == 0:
            self._my_mix_seen_track_ids = set()

        queue: str | int | None = None
        if page > 0:
            queue = self._my_mix_playlist_next_cursor
            if not queue:
                return []

        if len(self._my_mix_seen_track_ids) >= MY_MIX_MAX_TRACKS:
            return []

        (
            tracks,
            _,
            last_first_track_id,
            self._my_mix_seen_track_ids,
        ) = await self._fetch_my_mix_tracks(
            max_batches=1,
            initial_queue=queue,
            seen_track_ids=self._my_mix_seen_track_ids,
        )
        if last_first_track_id is not None:
            self._my_mix_playlist_next_cursor = last_first_track_id
        return tracks

    # Get related items

    @use_cache(3600 * 24 * 30)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks.

        :param prov_album_id: The provider album ID.
        :return: List of Track objects.
        """
        album = await self.client.get_album_with_tracks(prov_album_id)
        if not album or not album.volumes:
            return []

        tracks = []
        for volume_index, volume in enumerate(album.volumes):
            for track_index, track in enumerate(volume):
                try:
                    parsed_track = parse_track(self, track)
                    parsed_track.disc_number = volume_index + 1
                    parsed_track.track_number = track_index + 1
                    tracks.append(parsed_track)
                except InvalidDataError as err:
                    self.logger.debug("Error parsing album track: %s", err)
        return tracks

    @use_cache(3600 * 3)
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Get similar tracks using rotor station for this track.

        Uses rotor station track:{id} so MA radio mode gets recommendations.

        :param prov_track_id: Provider track ID (plain or track_id@station_id).
        :param limit: Maximum number of tracks to return.
        :return: List of similar Track objects.
        """
        track_id, _ = _parse_radio_item_id(prov_track_id)
        station_id = f"track:{track_id}"
        yandex_tracks, _ = await self.client.get_rotor_station_tracks(station_id, queue=None)
        tracks = []
        for yt in yandex_tracks[:limit]:
            try:
                tracks.append(parse_track(self, yt))
            except InvalidDataError as err:
                self.logger.debug("Error parsing similar track: %s", err)
        return tracks

    @use_cache(600)  # Cache for 10 minutes
    async def recommendations(self) -> list[RecommendationFolder]:
        """Get recommendations; includes My Mix (Мой Микс) as first folder.

        Fetches fresh tracks on each call for discovery experience.

        :return: List of recommendation folders (My Mix with tracks).
        """
        items, _, _, _ = await self._fetch_my_mix_tracks(
            max_tracks=DISCOVERY_INITIAL_TRACKS,
        )

        names = self._get_browse_names()
        return [
            RecommendationFolder(
                item_id=MY_MIX_PLAYLIST_ID,
                provider=self.instance_id,
                name=names[MY_MIX_PLAYLIST_ID],
                items=UniqueList(items),
                icon="mdi-waveform",
            )
        ]

    @use_cache(3600 * 3)
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks.

        :param prov_playlist_id: The provider playlist ID (format: "owner_id:kind" or my_mix).
        :param page: Page number for pagination.
        :return: List of Track objects.
        """
        if prov_playlist_id == MY_MIX_PLAYLIST_ID:
            return await self._get_my_mix_playlist_tracks(page)

        # KION Music API returns all playlist tracks in one call (no server-side pagination).
        # Return empty list for page > 0 so the controller pagination loop terminates.
        if page > 0:
            return []

        # Parse the playlist ID (format: owner_id:kind)
        if PLAYLIST_ID_SPLITTER in prov_playlist_id:
            owner_id, kind = prov_playlist_id.split(PLAYLIST_ID_SPLITTER, 1)
        else:
            owner_id = str(self.client.user_id)
            kind = prov_playlist_id

        playlist = await self.client.get_playlist(owner_id, kind)
        if not playlist:
            return []

        # API sometimes returns playlist without tracks; fetch them explicitly if needed
        tracks_list = playlist.tracks or []
        track_count = getattr(playlist, "track_count", None) or 0
        if not tracks_list and track_count > 0:
            self.logger.debug(
                "Playlist %s/%s: track_count=%s but no tracks in response, "
                "calling fetch_tracks_async",
                owner_id,
                kind,
                track_count,
            )
            try:
                tracks_list = await playlist.fetch_tracks_async()
            except Exception as err:
                self.logger.warning("fetch_tracks_async failed for %s/%s: %s", owner_id, kind, err)
            if not tracks_list:
                raise ResourceTemporarilyUnavailable(
                    "Playlist tracks not available; try again later"
                )

        if not tracks_list:
            return []

        # API returns TrackShort objects, we need to fetch full track info
        track_ids = [
            str(track.track_id) if hasattr(track, "track_id") else str(track.id)
            for track in tracks_list
            if track
        ]
        if not track_ids:
            return []

        # Fetch full track details in batches to avoid timeouts
        full_tracks = []
        for i in range(0, len(track_ids), TRACK_BATCH_SIZE):
            batch = track_ids[i : i + TRACK_BATCH_SIZE]
            batch_result = await self.client.get_tracks(batch)
            if not batch_result:
                self.logger.warning(
                    "Received empty result for playlist %s tracks batch %s-%s",
                    prov_playlist_id,
                    i,
                    i + len(batch) - 1,
                )
                raise ResourceTemporarilyUnavailable(
                    "Playlist tracks not fully available; try again later"
                )
            full_tracks.extend(batch_result)

        if track_ids and not full_tracks:
            raise ResourceTemporarilyUnavailable("Failed to load track details; try again later")

        tracks = []
        for track in full_tracks:
            try:
                tracks.append(parse_track(self, track))
            except InvalidDataError as err:
                self.logger.debug("Error parsing playlist track: %s", err)
        return tracks

    @use_cache(3600 * 24 * 7)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get artist's albums.

        :param prov_artist_id: The provider artist ID.
        :return: List of Album objects.
        """
        albums = await self.client.get_artist_albums(prov_artist_id)
        result = []
        for album in albums:
            try:
                result.append(parse_album(self, album))
            except InvalidDataError as err:
                self.logger.debug("Error parsing artist album: %s", err)
        return result

    @use_cache(3600 * 24 * 7)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get artist's top tracks.

        :param prov_artist_id: The provider artist ID.
        :return: List of Track objects.
        """
        tracks = await self.client.get_artist_tracks(prov_artist_id)
        result = []
        for track in tracks:
            try:
                result.append(parse_track(self, track))
            except InvalidDataError as err:
                self.logger.debug("Error parsing artist track: %s", err)
        return result

    # Library methods

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from KION Music."""
        artists = await self.client.get_liked_artists()
        for artist in artists:
            try:
                yield parse_artist(self, artist)
            except InvalidDataError as err:
                self.logger.debug("Error parsing library artist: %s", err)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from KION Music."""
        albums = await self.client.get_liked_albums(batch_size=TRACK_BATCH_SIZE)
        for album in albums:
            try:
                yield parse_album(self, album)
            except InvalidDataError as err:
                self.logger.debug("Error parsing library album: %s", err)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from KION Music."""
        track_shorts = await self.client.get_liked_tracks()
        if not track_shorts:
            return

        # Fetch full track details in batches
        track_ids = [str(ts.track_id) for ts in track_shorts if ts.track_id]
        for i in range(0, len(track_ids), TRACK_BATCH_SIZE):
            batch_ids = track_ids[i : i + TRACK_BATCH_SIZE]
            full_tracks = await self.client.get_tracks(batch_ids)
            for track in full_tracks:
                try:
                    yield parse_track(self, track)
                except InvalidDataError as err:
                    self.logger.debug("Error parsing library track: %s", err)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve library playlists from KION Music.

        Includes the virtual My Mix playlist first, then user playlists.
        """
        yield await self.get_playlist(MY_MIX_PLAYLIST_ID)
        playlists = await self.client.get_user_playlists()
        for playlist in playlists:
            try:
                yield parse_playlist(self, playlist)
            except InvalidDataError as err:
                self.logger.debug("Error parsing library playlist: %s", err)

    # Library edit methods

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to library.

        :param item: The media item to add.
        :return: True if successful.
        """
        prov_item_id = self._get_provider_item_id(item)
        if not prov_item_id:
            return False
        track_id, _ = _parse_radio_item_id(prov_item_id)

        if item.media_type == MediaType.TRACK:
            return await self.client.like_track(track_id)
        if item.media_type == MediaType.ALBUM:
            return await self.client.like_album(prov_item_id)
        if item.media_type == MediaType.ARTIST:
            return await self.client.like_artist(prov_item_id)
        return False

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from library.

        :param prov_item_id: The provider item ID (may be track_id@station_id for tracks).
        :param media_type: The media type.
        :return: True if successful.
        """
        track_id, _ = _parse_radio_item_id(prov_item_id)
        if media_type == MediaType.TRACK:
            return await self.client.unlike_track(track_id)
        if media_type == MediaType.ALBUM:
            return await self.client.unlike_album(prov_item_id)
        if media_type == MediaType.ARTIST:
            return await self.client.unlike_artist(prov_item_id)
        return False

    def _get_provider_item_id(self, item: MediaItemType) -> str | None:
        """Get provider item ID from media item."""
        for mapping in item.provider_mappings:
            if mapping.provider_instance == self.instance_id:
                return mapping.item_id
        return item.item_id if item.provider == self.instance_id else None

    # Streaming

    async def get_stream_details(
        self, item_id: str, media_type: MediaType = MediaType.TRACK
    ) -> StreamDetails:
        """Get stream details for a track.

        :param item_id: The track ID (or track_id@station_id for My Mix).
        :param media_type: The media type (should be TRACK).
        :return: StreamDetails for the track.
        """
        return await self.streaming.get_stream_details(item_id)

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """Report playback for rotor feedback when the track is from My Mix.

        Sends trackStarted when the track is currently playing (is_playing=True).
        trackFinished/skip are sent from on_streamed to use accurate seconds_streamed.
        """
        if media_type != MediaType.TRACK:
            return
        track_id, station_id = _parse_radio_item_id(prov_item_id)
        if not station_id:
            return
        if is_playing:
            await self.client.send_rotor_station_feedback(
                station_id,
                "trackStarted",
                track_id=track_id,
                batch_id=self._my_mix_batch_id,
            )

    async def on_streamed(self, streamdetails: StreamDetails) -> None:
        """Report stream completion for My Mix rotor feedback.

        Sends trackFinished or skip with actual seconds_streamed so the service
        can improve recommendations.
        """
        track_id, station_id = _parse_radio_item_id(streamdetails.item_id)
        if not station_id:
            return
        seconds = int(streamdetails.seconds_streamed or 0)
        duration = streamdetails.duration or 0
        feedback_type = "trackFinished" if duration and seconds >= max(0, duration - 10) else "skip"
        await self.client.send_rotor_station_feedback(
            station_id,
            feedback_type,
            track_id=track_id,
            total_played_seconds=seconds,
            batch_id=self._my_mix_batch_id,
        )
