"""Zvuk Music provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemMetadata,
    MediaItemType,
    Playlist,
    RecommendationFolder,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .api_client import ZvukMusicClient
from .constants import (
    CONF_QUALITY,
    CONF_TOKEN,
    DEFAULT_LIMIT,
    PLAYLIST_TRACKS_PAGE_SIZE,
    QUALITY_LOSSLESS,
    SYNTHESIS_PLAYLIST_IDS,
)
from .parsers import parse_album, parse_artist, parse_playlist, parse_track

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class ZvukMusicProvider(MusicProvider):
    """Implementation of a Zvuk Music MusicProvider."""

    _client: ZvukMusicClient | None = None

    @property
    def client(self) -> ZvukMusicClient:
        """Return the Zvuk Music client."""
        if self._client is None:
            raise ProviderUnavailableError("Provider not initialized")
        return self._client

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        token = self.config.get_value(CONF_TOKEN)
        if not token:
            raise LoginFailed("No Zvuk Music token provided")

        self._client = ZvukMusicClient(str(token))
        await self._client.connect()
        self.logger.info("Successfully connected to Zvuk Music")

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider.

        :param is_removed: Whether the provider is being removed.
        """
        if self._client:
            await self._client.disconnect()
        self._client = None
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

    # Search

    @use_cache(3600 * 24 * 14)
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Perform search on Zvuk Music.

        :param search_query: The search query.
        :param media_types: List of media types to search for.
        :param limit: Maximum number of results per type.
        :return: SearchResults with found items.
        """
        result = SearchResults()

        search_result = await self.client.search(
            search_query,
            limit=limit,
            search_tracks=MediaType.TRACK in media_types,
            search_artists=MediaType.ARTIST in media_types,
            search_releases=MediaType.ALBUM in media_types,
            search_playlists=MediaType.PLAYLIST in media_types,
        )
        if not search_result:
            return result

        # Parse tracks
        if MediaType.TRACK in media_types and search_result.tracks:
            for track in search_result.tracks.items[:limit]:
                try:
                    result.tracks = [*result.tracks, parse_track(self, track)]
                except InvalidDataError as err:
                    self.logger.debug("Error parsing track: %s", err)

        # Parse albums (Zvuk releases)
        if MediaType.ALBUM in media_types and search_result.releases:
            for release in search_result.releases.items[:limit]:
                try:
                    result.albums = [*result.albums, parse_album(self, release)]
                except InvalidDataError as err:
                    self.logger.debug("Error parsing album: %s", err)

        # Parse artists
        if MediaType.ARTIST in media_types and search_result.artists:
            for artist in search_result.artists.items[:limit]:
                try:
                    result.artists = [*result.artists, parse_artist(self, artist)]
                except InvalidDataError as err:
                    self.logger.debug("Error parsing artist: %s", err)

        # Parse playlists
        if MediaType.PLAYLIST in media_types and search_result.playlists:
            for playlist in search_result.playlists.items[:limit]:
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
        release = await self.client.get_release(prov_album_id)
        if not release:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        return parse_album(self, release)

    @use_cache(3600 * 24 * 30)
    async def get_track(self, prov_track_id: str) -> Track:
        """Get track details by ID.

        :param prov_track_id: The provider track ID.
        :return: Track object.
        :raises MediaNotFoundError: If track not found.
        """
        track = await self.client.get_track(prov_track_id)
        if not track:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")
        return parse_track(self, track)

    @use_cache(3600 * 24 * 30)
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details by ID.

        :param prov_playlist_id: The provider playlist ID.
        :return: Playlist object.
        :raises MediaNotFoundError: If playlist not found.
        """
        playlist = await self.client.get_playlist(prov_playlist_id)
        if not playlist:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found")
        return parse_playlist(self, playlist)

    # Get related items

    @use_cache(3600 * 24 * 30)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks.

        :param prov_album_id: The provider album ID.
        :return: List of Track objects.
        """
        release = await self.client.get_release(prov_album_id)
        if not release or not release.tracks:
            return []

        tracks = []
        for index, track in enumerate(release.tracks):
            try:
                parsed_track = parse_track(self, track)
                parsed_track.disc_number = 1
                parsed_track.track_number = index + 1
                tracks.append(parsed_track)
            except InvalidDataError as err:
                self.logger.debug("Error parsing album track: %s", err)
        return tracks

    @use_cache(3600 * 3)
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks.

        :param prov_playlist_id: The provider playlist ID.
        :param page: Page number for pagination.
        :return: List of Track objects.
        """
        offset = page * PLAYLIST_TRACKS_PAGE_SIZE
        simple_tracks = await self.client.get_playlist_tracks(
            prov_playlist_id, limit=PLAYLIST_TRACKS_PAGE_SIZE, offset=offset
        )
        if not simple_tracks:
            return []

        # Fetch full track details from SimpleTrack IDs
        track_ids = [str(t.id) for t in simple_tracks if t.id]
        if not track_ids:
            return []

        full_tracks = await self.client.get_tracks(track_ids)
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
        artists = await self.client.get_artist_releases(prov_artist_id, limit=DEFAULT_LIMIT)
        if not artists:
            return []

        result = []
        for artist in artists:
            for release in artist.releases:
                try:
                    result.append(parse_album(self, release))
                except InvalidDataError as err:
                    self.logger.debug("Error parsing artist album: %s", err)
        return result

    @use_cache(3600 * 24 * 7)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get artist's top tracks.

        :param prov_artist_id: The provider artist ID.
        :return: List of Track objects.
        """
        artists = await self.client.get_artist_top_tracks(prov_artist_id, limit=DEFAULT_LIMIT)
        if not artists:
            return []

        result = []
        for artist in artists:
            for track in artist.popular_tracks:
                try:
                    result.append(parse_track(self, track))
                except InvalidDataError as err:
                    self.logger.debug("Error parsing artist track: %s", err)
        return result

    @use_cache(3600 * 24 * 7)
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Get similar tracks based on related releases of the track's album.

        Uses the Zvuk ``release.related`` field to find similar releases and samples tracks from
        them. Only called if provider supports ProviderFeature.SIMILAR_TRACKS.

        :param prov_track_id: The provider track ID.
        :param limit: Maximum number of similar tracks to return.
        :return: List of Track objects.
        """
        track = await self.client.get_track(prov_track_id)
        if not track or not track.release:
            return []

        release = await self.client.get_release(str(track.release.id))
        if not release or not getattr(release, "related", None):
            return []

        result: list[Track] = []
        for related_release in release.related:
            if len(result) >= limit:
                break
            related_full = await self.client.get_release(str(related_release.id))
            if not related_full or not related_full.tracks:
                continue
            for t in related_full.tracks[:2]:
                try:
                    result.append(parse_track(self, t))
                except InvalidDataError as err:
                    self.logger.debug("Error parsing similar track: %s", err)
                if len(result) >= limit:
                    break

        return result[:limit]

    # Library methods

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from Zvuk Music."""
        collection = await self.client.get_collection()
        if not collection or not collection.artists:
            return

        artist_ids = [str(item.id) for item in collection.artists if item.id]
        for i in range(0, len(artist_ids), DEFAULT_LIMIT):
            batch_ids = artist_ids[i : i + DEFAULT_LIMIT]
            artists = await self.client.get_artists(batch_ids)
            for artist in artists:
                try:
                    yield parse_artist(self, artist)
                except InvalidDataError as err:
                    self.logger.debug("Error parsing library artist: %s", err)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from Zvuk Music."""
        collection = await self.client.get_collection()
        if not collection or not collection.releases:
            return

        release_ids = [str(item.id) for item in collection.releases if item.id]
        for i in range(0, len(release_ids), DEFAULT_LIMIT):
            batch_ids = release_ids[i : i + DEFAULT_LIMIT]
            releases = await self.client.get_releases(batch_ids)
            for release in releases:
                try:
                    yield parse_album(self, release)
                except InvalidDataError as err:
                    self.logger.debug("Error parsing library album: %s", err)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Zvuk Music."""
        collection = await self.client.get_collection()
        if not collection or not collection.tracks:
            return

        track_ids = [str(item.id) for item in collection.tracks if item.id]
        for i in range(0, len(track_ids), DEFAULT_LIMIT):
            batch_ids = track_ids[i : i + DEFAULT_LIMIT]
            tracks = await self.client.get_tracks(batch_ids)
            for track in tracks:
                try:
                    yield parse_track(self, track)
                except InvalidDataError as err:
                    self.logger.debug("Error parsing library track: %s", err)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve library playlists from Zvuk Music.

        Yields user's own playlists followed by Zvuk's personalized synthesis
        playlists («Плейлисты для вас»: IDs 3, 4, 6, 11, 12, 13, 14, 15).
        """
        collection_items = await self.client.get_user_playlists()
        if not collection_items:
            return

        playlist_ids = [str(item.id) for item in collection_items if item.id]
        for i in range(0, len(playlist_ids), DEFAULT_LIMIT):
            batch_ids = playlist_ids[i : i + DEFAULT_LIMIT]
            playlists = await self.client.get_playlists(batch_ids)
            for playlist in playlists:
                try:
                    yield parse_playlist(self, playlist)
                except InvalidDataError as err:
                    self.logger.debug("Error parsing library playlist: %s", err)

        # Synthesis playlists — personalized AI playlists («Плейлисты для вас»)
        synthesis_playlists = await self.client.get_short_playlists(SYNTHESIS_PLAYLIST_IDS)
        for simple_pl in synthesis_playlists:
            try:
                yield parse_playlist(self, simple_pl)
            except InvalidDataError as err:
                self.logger.debug("Error parsing synthesis playlist: %s", err)

    async def recommendations(self) -> list[RecommendationFolder]:
        """Return personalized and editorial playlist recommendations.

        Returns two folders:
        - «Плейлисты для вас»: Zvuk's AI-generated personalized playlists.
        - «Подборки»: Editorial genre-themed curated playlists.
        """
        folders: list[RecommendationFolder] = []

        # Folder 1: Personalized synthesis playlists («Плейлисты для вас»)
        synthesis_playlists = await self.client.get_short_playlists(SYNTHESIS_PLAYLIST_IDS)
        for_you_items: list[Playlist] = []
        for simple_pl in synthesis_playlists:
            try:
                for_you_items.append(parse_playlist(self, simple_pl))
            except InvalidDataError as err:
                self.logger.debug("Error parsing synthesis playlist: %s", err)
        if for_you_items:
            folders.append(
                RecommendationFolder(
                    item_id="for_you",
                    provider=self.instance_id,
                    name="Плейлисты для вас",
                    subtitle="Персональные плейлисты от Звук",
                    icon="mdi-playlist-music",
                    items=for_you_items,  # type: ignore[arg-type]
                )
            )

        # Folder 2: Editorial curated playlists («Подборки»)
        editorial_ids = await self.client.get_editorial_playlist_ids()
        if editorial_ids:
            editorial_str_ids = [str(pid) for pid in editorial_ids[:DEFAULT_LIMIT]]
            editorial_playlists = await self.client.get_playlists(editorial_str_ids)
            editorial_items: list[Playlist] = []
            for full_pl in editorial_playlists:
                try:
                    editorial_items.append(parse_playlist(self, full_pl))
                except InvalidDataError as err:
                    self.logger.debug("Error parsing editorial playlist: %s", err)
            if editorial_items:
                folders.append(
                    RecommendationFolder(
                        item_id="editorial",
                        provider=self.instance_id,
                        name="Подборки",
                        subtitle="Плейлисты от редакции Звук по жанрам",
                        icon="mdi-music-box-multiple",
                        items=editorial_items,  # type: ignore[arg-type]
                    )
                )

        return folders

    async def get_track_metadata(self, track: Track) -> MediaItemMetadata | None:
        """Fetch lyrics for a track from Zvuk's lyrics API.

        Called by MA when ``ProviderFeature.TRACK_METADATA`` is declared.
        Returns LRC-synced lyrics (``lrc_lyrics``) when the API returns type
        ``'subtitle'``, otherwise plain text (``lyrics``). Returns ``None`` if
        the track has no lyrics or the API call fails.

        :param track: The MA Track object. ``item_id`` is used to call the API.
        :return: MediaItemMetadata with lyrics, or None.
        """
        track_id = track.item_id
        result = await self.client.get_lyrics(track_id)
        if not result:
            return None

        lyrics_text: str = result.get("lyrics") or ""
        lyrics_type: str = result.get("type") or ""
        if not lyrics_text:
            return None

        metadata = MediaItemMetadata()
        if lyrics_type == "subtitle":
            metadata.lrc_lyrics = lyrics_text
        else:
            metadata.lyrics = lyrics_text
        return metadata

    async def resolve_image(self, path: str) -> str | bytes:
        """Fetch a Zvuk static playlist image with authentication.

        Called by MA when a ``MediaItemImage`` has ``remotely_accessible=False``.
        Static playlist avatar images (``/static/avatar/playlist/...``) require
        Zvuk auth cookies and cannot be fetched anonymously.

        :param path: Full image URL (e.g. ``https://zvuk.com/static/avatar/...``).
        :return: Raw image bytes on success, original URL string as fallback.
        """
        token = self.config.get_value(CONF_TOKEN)
        try:
            async with self.mass.http_session.get(
                path,
                headers={
                    "X-Auth-Token": str(token),
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://zvuk.com/",
                    "Origin": "https://zvuk.com",
                },
            ) as resp:
                if resp.status == 200:
                    return bytes(await resp.read())
        except Exception as err:
            self.logger.debug("Failed to resolve static image %s: %s", path, err)
        return str(path)

    # Library edit methods

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to library.

        :param item: The media item to add.
        :return: True if successful.
        """
        prov_item_id = self._get_provider_item_id(item)
        if not prov_item_id:
            return False

        if item.media_type == MediaType.TRACK:
            return await self.client.like_track(prov_item_id)
        if item.media_type == MediaType.ALBUM:
            return await self.client.like_release(prov_item_id)
        if item.media_type == MediaType.ARTIST:
            return await self.client.like_artist(prov_item_id)
        if item.media_type == MediaType.PLAYLIST:
            return await self.client.like_playlist(prov_item_id)
        return False

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from library.

        :param prov_item_id: The provider item ID.
        :param media_type: The media type.
        :return: True if successful.
        """
        if media_type == MediaType.TRACK:
            return await self.client.unlike_track(prov_item_id)
        if media_type == MediaType.ALBUM:
            return await self.client.unlike_release(prov_item_id)
        if media_type == MediaType.ARTIST:
            return await self.client.unlike_artist(prov_item_id)
        if media_type == MediaType.PLAYLIST:
            return await self.client.unlike_playlist(prov_item_id)
        return False

    def _get_provider_item_id(self, item: MediaItemType) -> str | None:
        """Get provider item ID from media item."""
        for mapping in item.provider_mappings:
            if mapping.provider_instance == self.instance_id:
                return mapping.item_id
        return item.item_id if item.provider == self.instance_id else None

    # Playlist management

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist.

        :param name: Playlist name.
        :return: The created Playlist object.
        """
        playlist_id = await self.client.create_playlist(name)
        playlist = await self.client.get_playlist(playlist_id)
        if not playlist:
            raise MediaNotFoundError(f"Created playlist {playlist_id} not found")
        return parse_playlist(self, playlist)

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add tracks to a playlist.

        :param prov_playlist_id: The provider playlist ID.
        :param prov_track_ids: List of track IDs to add.
        """
        await self.client.add_tracks_to_playlist(prov_playlist_id, prov_track_ids)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove tracks from a playlist by position.

        :param prov_playlist_id: The provider playlist ID.
        :param positions_to_remove: Tuple of track positions (0-based) to remove.
        """
        # Fetch current tracks and filter out the ones at given positions
        simple_tracks = await self.client.get_playlist_tracks(prov_playlist_id, limit=10000)
        remove_positions = set(positions_to_remove)
        remaining_ids = [
            str(t.id) for i, t in enumerate(simple_tracks) if t.id and i not in remove_positions
        ]
        await self.client.update_playlist(prov_playlist_id, remaining_ids)

    # Streaming

    async def get_stream_details(
        self, item_id: str, media_type: MediaType = MediaType.TRACK
    ) -> StreamDetails:
        """Get stream details for a track.

        Uses /api/tiny/track/stream to get a direct (non-DRM) URL. When lossless is
        requested, always tries "flac" quality first, then falls back through "high"
        (320kbps MP3) → "mid" (128kbps MP3). The ``has_flac`` field from the API is
        not reliable enough to skip the FLAC attempt.

        :param item_id: The track ID.
        :param media_type: The media type (should be TRACK).
        :return: StreamDetails for the track.
        :raises MediaNotFoundError: If stream URL cannot be obtained.
        """
        quality_pref = self.config.get_value(CONF_QUALITY)
        quality_str = str(quality_pref) if quality_pref is not None else QUALITY_LOSSLESS

        # Fetch track metadata for duration and FLAC availability.
        track = await self.client.get_track(item_id)
        duration: int | None = None
        has_flac: bool = True  # default: always attempt FLAC; field is unreliable
        if track is not None:
            if getattr(track, "duration", None) is not None:
                duration = int(track.duration)
            if getattr(track, "has_flac", None) is not None:
                has_flac = bool(track.has_flac)

        # Build quality fallback chain.
        # /api/tiny/track/stream quality strings: "flac", "high", "mid"
        self.logger.debug(
            "Stream request for track %s: quality_pref=%s has_flac=%s",
            item_id,
            quality_str,
            has_flac,
        )
        if quality_str == QUALITY_LOSSLESS:
            quality_chain = [
                ("flac", ContentType.FLAC, 0),
                ("high", ContentType.MP3, 320),
                ("mid", ContentType.MP3, 128),
            ]
        else:
            quality_chain = [("high", ContentType.MP3, 320), ("mid", ContentType.MP3, 128)]

        url: str | None = None
        content_type = ContentType.UNKNOWN
        bitrate = 0

        for q_str, q_content_type, q_bitrate in quality_chain:
            url = await self.client.get_direct_stream_url(item_id, q_str)
            self.logger.debug(
                "Stream URL for track %s quality=%s: %s",
                item_id,
                q_str,
                "OK" if url else "None",
            )
            if url:
                content_type = q_content_type
                bitrate = q_bitrate
                break

        if not url:
            raise MediaNotFoundError(f"No stream URL available for track {item_id}")

        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=content_type,
                bit_rate=bitrate,
            ),
            stream_type=StreamType.HTTP,
            path=url,
            duration=duration,
            allow_seek=True,
            can_seek=True,
        )
