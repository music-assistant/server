"""Yandex Music provider implementation."""

from __future__ import annotations

import logging
import random
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime
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

from .api_client import YandexMusicClient
from .constants import (
    BROWSE_INITIAL_TRACKS,
    BROWSE_NAMES_EN,
    BROWSE_NAMES_RU,
    CONF_BASE_URL,
    CONF_LIKED_TRACKS_MAX_TRACKS,
    CONF_MY_WAVE_MAX_TRACKS,
    CONF_TOKEN,
    DEFAULT_BASE_URL,
    DISCOVERY_INITIAL_TRACKS,
    LIKED_TRACKS_PLAYLIST_ID,
    MY_WAVE_BATCH_SIZE,
    MY_WAVE_PLAYLIST_ID,
    PLAYLIST_ID_SPLITTER,
    RADIO_TRACK_ID_SEP,
    ROTOR_STATION_MY_WAVE,
    TAG_CATEGORY_ACTIVITY,
    TAG_CATEGORY_ERA,
    TAG_CATEGORY_GENRES,
    TAG_CATEGORY_MOOD,
    TAG_MIXES,
    TAG_SEASONAL_MAP,
    TRACK_BATCH_SIZE,
)
from .parsers import parse_album, parse_artist, parse_playlist, parse_track
from .streaming import YandexMusicStreamingManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.streamdetails import StreamDetails


def _parse_radio_item_id(item_id: str) -> tuple[str, str | None]:
    """Extract track_id and optional station_id from provider item_id.

    My Wave tracks use item_id format 'track_id@station_id'. Other tracks use
    plain track_id.

    :param item_id: Provider item_id (may contain RADIO_TRACK_ID_SEP).
    :return: (track_id, station_id or None).
    """
    if RADIO_TRACK_ID_SEP in item_id:
        parts = item_id.split(RADIO_TRACK_ID_SEP, 1)
        return (parts[0], parts[1] if len(parts) > 1 else None)
    return (item_id, None)


class YandexMusicProvider(MusicProvider):
    """Implementation of a Yandex Music MusicProvider."""

    _client: YandexMusicClient | None = None
    _streaming: YandexMusicStreamingManager | None = None
    _my_wave_batch_id: str | None = None
    _my_wave_last_track_id: str | None = None  # last track id for "Load more" (API queue param)
    _my_wave_playlist_next_cursor: str | None = None  # first_track_id for next playlist page
    _my_wave_radio_started_sent: bool = False
    _my_wave_seen_track_ids: set[str]  # Track IDs seen in current My Wave session

    @property
    def client(self) -> YandexMusicClient:
        """Return the Yandex Music client."""
        if self._client is None:
            raise ProviderUnavailableError("Provider not initialized")
        return self._client

    @property
    def streaming(self) -> YandexMusicStreamingManager:
        """Return the streaming manager."""
        if self._streaming is None:
            raise ProviderUnavailableError("Provider not initialized")
        return self._streaming

    def _get_browse_names(self) -> dict[str, str]:
        """Get locale-based browse folder names."""
        try:
            locale = (self.mass.metadata.locale or "en_US").lower()
            use_russian = locale.startswith("ru")
            self.logger.debug("Locale detection: locale=%s, use_russian=%s", locale, use_russian)
        except Exception as err:
            self.logger.debug("Locale detection failed: %s", err)
            use_russian = False
        return BROWSE_NAMES_RU if use_russian else BROWSE_NAMES_EN

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        token = self.config.get_value(CONF_TOKEN)
        if not token:
            raise LoginFailed("No Yandex Music token provided")

        base_url = self.config.get_value(CONF_BASE_URL, DEFAULT_BASE_URL)
        self._client = YandexMusicClient(str(token), base_url=str(base_url))
        await self._client.connect()
        # Suppress yandex_music library DEBUG dumps (full API request/response JSON)
        logging.getLogger("yandex_music").setLevel(self.logger.level + 10)
        self._streaming = YandexMusicStreamingManager(self)
        # Initialize My Wave duplicate tracking
        self._my_wave_seen_track_ids = set()
        self.logger.info("Successfully connected to Yandex Music")

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider.

        :param is_removed: Whether the provider is being removed.
        """
        # Clean up any temp files from preload streaming
        if self._streaming:
            self._streaming.cleanup_all_temp_files()
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

    async def browse(  # noqa: PLR0915
        self, path: str
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse provider items with locale-based folder names and My Wave.

        Root level shows My Wave, artists, albums, liked tracks, playlists. Names
        are in Russian when MA locale is ru_*, otherwise in English. My Wave
        tracks use item_id format track_id@station_id for rotor feedback.

        :param path: The path to browse (e.g. provider_id:// or provider_id://artists).
        """
        if ProviderFeature.BROWSE not in self.supported_features:
            raise NotImplementedError

        path_parts = path.split("://")[1].split("/") if "://" in path else []
        subpath = path_parts[0] if len(path_parts) > 0 else None
        sub_subpath = path_parts[1] if len(path_parts) > 1 else None

        if subpath == MY_WAVE_PLAYLIST_ID:
            # Get config values for max tracks and batch size
            max_tracks_config = int(
                self.config.get_value(CONF_MY_WAVE_MAX_TRACKS) or 150  # type: ignore[arg-type]
            )
            batch_size_config = MY_WAVE_BATCH_SIZE

            # Root my_wave: fetch up to batch_size_config batches so Play adds more tracks.
            # "Load more" always uses single next batch.
            max_batches = batch_size_config if sub_subpath != "next" else 1

            # Reset seen tracks on fresh browse (not "load more")
            if sub_subpath != "next":
                self._my_wave_seen_track_ids = set()

            queue: str | int | None = None
            if sub_subpath == "next":
                queue = self._my_wave_last_track_id
            elif sub_subpath:
                queue = sub_subpath

            all_tracks: list[Track | BrowseFolder] = []
            last_batch_id: str | None = None
            first_track_id_this_batch: str | None = None
            total_track_count = 0

            for _ in range(max_batches):
                # Check if we've reached the max track limit
                if total_track_count >= max_tracks_config:
                    break

                yandex_tracks, batch_id = await self.client.get_my_wave_tracks(queue=queue)
                if batch_id:
                    self._my_wave_batch_id = batch_id
                    last_batch_id = batch_id
                if not self._my_wave_radio_started_sent and yandex_tracks:
                    self._my_wave_radio_started_sent = True
                    await self.client.send_rotor_station_feedback(
                        ROTOR_STATION_MY_WAVE,
                        "radioStarted",
                        batch_id=batch_id,
                    )
                first_track_id_this_batch = None
                for yt in yandex_tracks:
                    # Check if we've reached the max track limit
                    if total_track_count >= max_tracks_config:
                        break

                    try:
                        t = parse_track(self, yt)
                        track_id = (
                            str(yt.id)
                            if hasattr(yt, "id") and yt.id
                            else getattr(yt, "track_id", None)
                        )
                        if track_id:
                            # Check for duplicates
                            if track_id in self._my_wave_seen_track_ids:
                                self.logger.debug("Skipping duplicate My Wave track: %s", track_id)
                                continue

                            # Mark track as seen
                            self._my_wave_seen_track_ids.add(track_id)

                            if first_track_id_this_batch is None:
                                first_track_id_this_batch = track_id
                            t.item_id = f"{track_id}{RADIO_TRACK_ID_SEP}{ROTOR_STATION_MY_WAVE}"
                            for pm in t.provider_mappings:
                                if pm.provider_instance == self.instance_id:
                                    pm.item_id = t.item_id
                                    break
                        all_tracks.append(t)
                        total_track_count += 1
                    except InvalidDataError as err:
                        self.logger.debug("Error parsing My Wave track: %s", err)
                if first_track_id_this_batch is not None:
                    self._my_wave_last_track_id = first_track_id_this_batch
                if (
                    first_track_id_this_batch is None
                    or not batch_id
                    or not yandex_tracks
                    or total_track_count >= max_tracks_config
                ):
                    break
                queue = first_track_id_this_batch

            # Apply initial tracks limit if not in "load more" mode
            if sub_subpath != "next":
                initial_tracks_limit = BROWSE_INITIAL_TRACKS
                if len(all_tracks) > initial_tracks_limit:
                    all_tracks = all_tracks[:initial_tracks_limit]

            # Only show "Load more" if we haven't reached the limit and there's more data
            if last_batch_id and total_track_count < max_tracks_config:
                names = self._get_browse_names()
                next_name = "Ещё" if names is BROWSE_NAMES_RU else "Load more"
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

        # Handle picks/ path (mood, activity, era, genres)
        if subpath == "picks":
            return await self._browse_picks(path, path_parts)

        # Handle mixes/ path (seasonal collections)
        if subpath == "mixes":
            return await self._browse_mixes(path, path_parts)

        # Handle direct tag subpath (when folder is played by URI, the full path
        # "picks/category/tag" is lost and only the tag slug arrives as subpath)
        if subpath:
            all_hardcoded_tags = set(
                TAG_CATEGORY_MOOD
                + TAG_CATEGORY_ACTIVITY
                + TAG_CATEGORY_ERA
                + TAG_CATEGORY_GENRES
                + TAG_MIXES
            )
            discovered_tags = await self._get_discovered_tag_slugs()
            if subpath in (all_hardcoded_tags | discovered_tags):
                return await self._get_tag_playlists_as_browse(subpath)

        if subpath:
            return await super().browse(path)

        names = self._get_browse_names()

        folders: list[BrowseFolder] = []
        base = path if path.endswith("//") else path.rstrip("/") + "/"
        # My Wave folder (always enabled)
        folders.append(
            BrowseFolder(
                item_id=MY_WAVE_PLAYLIST_ID,
                provider=self.instance_id,
                path=f"{base}{MY_WAVE_PLAYLIST_ID}",
                name=names[MY_WAVE_PLAYLIST_ID],
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
        # Liked Tracks folder (always enabled)
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
        # Picks folder (always enabled)
        folders.append(
            BrowseFolder(
                item_id="picks",
                provider=self.instance_id,
                path=f"{base}picks",
                name=names.get("picks", "Picks"),
                is_playable=False,
            )
        )
        # Mixes folder (always enabled)
        folders.append(
            BrowseFolder(
                item_id="mixes",
                provider=self.instance_id,
                path=f"{base}mixes",
                name=names.get("mixes", "Mixes"),
                is_playable=False,
            )
        )
        if len(folders) == 1:
            return await self.browse(folders[0].path)
        return folders

    @use_cache(3600)
    async def _get_discovered_tags(self) -> list[tuple[str, str]]:
        """Discover available tags from Landing API.

        Calls the landing("mixes") API to discover actual tag slugs that Yandex Music
        supports. Results are cached for 1 hour. Falls back to empty list on failure.

        :return: List of (slug, title) tuples discovered from the API.
        """
        try:
            tags = await self.client.get_landing_tags()
        except Exception as err:
            self.logger.debug("Failed to discover tags from landing API: %s", err)
            tags = []

        return tags

    async def _get_discovered_tag_slugs(self) -> set[str]:
        """Get set of all discovered tag slugs (cached).

        :return: Set of tag slug strings discovered from Landing API.
        """
        discovered = await self._get_discovered_tags()
        return {slug for slug, _title in discovered}

    async def _browse_picks(
        self, path: str, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse picks folder (mood, activity, era, genres).

        :param path: Full browse path.
        :param path_parts: Split path parts after ://.
        :return: List of folders or playlists.
        """
        names = self._get_browse_names()
        # base should always end with current path + /
        base = path.rstrip("/") + "/"

        # picks/ - show category folders
        if len(path_parts) == 1:
            return [
                BrowseFolder(
                    item_id="mood",
                    provider=self.instance_id,
                    path=f"{base}mood",
                    name=names.get("mood", "Mood"),
                    is_playable=False,
                ),
                BrowseFolder(
                    item_id="activity",
                    provider=self.instance_id,
                    path=f"{base}activity",
                    name=names.get("activity", "Activity"),
                    is_playable=False,
                ),
                BrowseFolder(
                    item_id="era",
                    provider=self.instance_id,
                    path=f"{base}era",
                    name=names.get("era", "Era"),
                    is_playable=False,
                ),
                BrowseFolder(
                    item_id="genres",
                    provider=self.instance_id,
                    path=f"{base}genres",
                    name=names.get("genres", "Genres"),
                    is_playable=False,
                ),
            ]

        category = path_parts[1] if len(path_parts) > 1 else None
        tag = path_parts[2] if len(path_parts) > 2 else None

        self.logger.debug(
            "Browse picks: path=%s, path_parts=%s, category=%s, tag=%s, base=%s",
            path,
            path_parts,
            category,
            tag,
            base,
        )

        # Determine tags for the category
        category_tags: list[str] = []
        if category == "mood":
            category_tags = list(TAG_CATEGORY_MOOD)
        elif category == "activity":
            category_tags = list(TAG_CATEGORY_ACTIVITY)
        elif category == "era":
            category_tags = list(TAG_CATEGORY_ERA)
        elif category == "genres":
            category_tags = list(TAG_CATEGORY_GENRES)

        # Enrich with dynamically discovered tags not already in any hardcoded category
        all_hardcoded = set(
            TAG_CATEGORY_MOOD
            + TAG_CATEGORY_ACTIVITY
            + TAG_CATEGORY_ERA
            + TAG_CATEGORY_GENRES
            + TAG_MIXES
        )
        discovered = await self._get_discovered_tags()
        discovered_tags = [(slug, title) for slug, title in discovered if slug not in all_hardcoded]

        self.logger.debug("Category tags for %s: %s", category, category_tags)

        # Build set of all valid tags for this category (hardcoded + discovered)
        discovered_slug_set = {slug for slug, _ in discovered_tags}
        all_valid_tags = set(category_tags) | discovered_slug_set

        # picks/category/ - show tag folders
        if category and not tag:
            folders = []
            for t in category_tags:
                folders.append(
                    BrowseFolder(
                        item_id=t,
                        provider=self.instance_id,
                        path=f"{base}{t}",
                        name=names.get(t, t.title()),
                        is_playable=True,
                    )
                )
            # Add discovered tags (not in any hardcoded category) to the "mood" category
            if category == "mood":
                for slug, title in discovered_tags:
                    if slug not in category_tags:
                        folders.append(
                            BrowseFolder(
                                item_id=slug,
                                provider=self.instance_id,
                                path=f"{base}{slug}",
                                name=names.get(slug, title),
                                is_playable=True,
                            )
                        )
            self.logger.debug("Returning %d tag folders for category %s", len(folders), category)
            return folders

        # picks/category/tag - show playlists for the tag
        if tag and tag in all_valid_tags:
            self.logger.debug("Fetching playlists for tag: %s", tag)
            return await self._get_tag_playlists_as_browse(tag)

        self.logger.debug("No match found, returning empty list")
        return []

    async def _browse_mixes(
        self, path: str, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse mixes folder (seasonal collections).

        :param path: Full browse path.
        :param path_parts: Split path parts after ://.
        :return: List of folders or playlists.
        """
        names = self._get_browse_names()
        # base should always end with current path + /
        base = path.rstrip("/") + "/"

        # mixes/ - show seasonal folders
        if len(path_parts) == 1:
            folders = []
            for t in TAG_MIXES:
                folders.append(
                    BrowseFolder(
                        item_id=t,
                        provider=self.instance_id,
                        path=f"{base}{t}",
                        name=names.get(t, t.title()),
                        is_playable=True,
                    )
                )
            return folders

        # mixes/tag - show playlists for the tag
        tag = path_parts[1] if len(path_parts) > 1 else None
        if tag and tag in TAG_MIXES:
            return await self._get_tag_playlists_as_browse(tag)

        return []

    @use_cache(3600)
    async def _get_tag_playlists_as_browse(
        self, tag_id: str
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Get playlists for a tag and return as browse items.

        :param tag_id: Tag identifier (e.g. 'chill', '80s').
        :return: List of Playlist objects.
        """
        self.logger.debug("Fetching playlists for tag: %s", tag_id)
        playlists = await self.client.get_tag_playlists(tag_id)
        self.logger.debug("Got %d playlists for tag %s", len(playlists), tag_id)
        result: list[Playlist] = []
        for playlist in playlists:
            try:
                result.append(parse_playlist(self, playlist))
            except InvalidDataError as err:
                self.logger.debug("Error parsing tag playlist: %s", err)
        self.logger.debug("Parsed %d playlists for tag %s", len(result), tag_id)
        return result

    # Search

    @use_cache(3600 * 24 * 14)
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Perform search on Yandex Music.

        :param search_query: The search query.
        :param media_types: List of media types to search for.
        :param limit: Maximum number of results per type.
        :return: SearchResults with found items.
        """
        result = SearchResults()

        # Determine search type based on requested media types
        # Map MediaType to Yandex API search type
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

    @use_cache(3600 * 24 * 30)
    async def get_track(self, prov_track_id: str) -> Track:
        """Get track details by ID.

        Supports composite item_id (track_id@station_id) for My Wave tracks;
        only the track_id part is used for the API.

        :param prov_track_id: The provider track ID (or track_id@station_id).
        :return: Track object.
        :raises MediaNotFoundError: If track not found.
        """
        track_id, _ = _parse_radio_item_id(prov_track_id)
        yandex_track = await self.client.get_track(track_id)
        if not yandex_track:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")

        # Use the already-fetched track object to avoid a duplicate API call
        lyrics, lyrics_synced = await self.client.get_track_lyrics_from_track(yandex_track)

        return parse_track(self, yandex_track, lyrics=lyrics, lyrics_synced=lyrics_synced)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details by ID.

        Supports virtual playlists MY_WAVE_PLAYLIST_ID (My Wave) and
        LIKED_TRACKS_PLAYLIST_ID (Liked Tracks). Real playlists use format "owner_id:kind".

        :param prov_playlist_id: The provider playlist ID (format: "owner_id:kind",
            my_wave, or liked_tracks).
        :return: Playlist object.
        :raises MediaNotFoundError: If playlist not found.
        """
        # Virtual playlists - not cached (locale-dependent names)
        if prov_playlist_id == MY_WAVE_PLAYLIST_ID:
            names = self._get_browse_names()
            return Playlist(
                item_id=MY_WAVE_PLAYLIST_ID,
                provider=self.instance_id,
                name=names[MY_WAVE_PLAYLIST_ID],
                owner="Yandex Music",
                provider_mappings={
                    ProviderMapping(
                        item_id=MY_WAVE_PLAYLIST_ID,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        is_unique=True,
                    )
                },
                is_editable=False,
            )

        if prov_playlist_id == LIKED_TRACKS_PLAYLIST_ID:
            names = self._get_browse_names()
            return Playlist(
                item_id=LIKED_TRACKS_PLAYLIST_ID,
                provider=self.instance_id,
                name=names["tracks"],
                owner="Yandex Music",
                provider_mappings={
                    ProviderMapping(
                        item_id=LIKED_TRACKS_PLAYLIST_ID,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        is_unique=True,
                    )
                },
                is_editable=False,
            )

        # Real playlists - use cached method
        return await self._get_real_playlist(prov_playlist_id)

    @use_cache(3600 * 24 * 30)
    async def _get_real_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get real playlist details by ID (cached).

        :param prov_playlist_id: The provider playlist ID (format: "owner_id:kind").
        :return: Playlist object.
        :raises MediaNotFoundError: If playlist not found.
        """
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

    async def _get_my_wave_playlist_tracks(self, page: int) -> list[Track]:
        """Get My Wave tracks for virtual playlist (uncached; uses cursor for page > 0).

        :param page: Page number (0 = first batch, 1+ = next batches via queue cursor).
        :return: List of Track objects for this page.
        """
        max_tracks_config = int(
            self.config.get_value(CONF_MY_WAVE_MAX_TRACKS) or 150  # type: ignore[arg-type]
        )

        # Reset seen tracks on first page
        if page == 0:
            self._my_wave_seen_track_ids = set()

        queue: str | int | None = None
        if page > 0:
            queue = self._my_wave_playlist_next_cursor
            if not queue:
                return []

        # Check if we've already reached the limit
        if len(self._my_wave_seen_track_ids) >= max_tracks_config:
            return []

        yandex_tracks, batch_id = await self.client.get_my_wave_tracks(queue=queue)
        if batch_id:
            self._my_wave_batch_id = batch_id
        if not self._my_wave_radio_started_sent and yandex_tracks:
            self._my_wave_radio_started_sent = True
            await self.client.send_rotor_station_feedback(
                ROTOR_STATION_MY_WAVE,
                "radioStarted",
                batch_id=batch_id,
            )
        first_track_id_this_batch = None
        tracks = []
        for yt in yandex_tracks:
            # Check if we've reached the max track limit
            if len(self._my_wave_seen_track_ids) >= max_tracks_config:
                break

            try:
                t = parse_track(self, yt)
                track_id = (
                    str(yt.id) if hasattr(yt, "id") and yt.id else getattr(yt, "track_id", None)
                )
                if track_id:
                    # Check for duplicates
                    if track_id in self._my_wave_seen_track_ids:
                        self.logger.debug("Skipping duplicate My Wave track: %s", track_id)
                        continue

                    # Mark track as seen
                    self._my_wave_seen_track_ids.add(track_id)

                    if first_track_id_this_batch is None:
                        first_track_id_this_batch = track_id
                    t.item_id = f"{track_id}{RADIO_TRACK_ID_SEP}{ROTOR_STATION_MY_WAVE}"
                    for pm in t.provider_mappings:
                        if pm.provider_instance == self.instance_id:
                            pm.item_id = t.item_id
                            break
                tracks.append(t)
            except InvalidDataError as err:
                self.logger.debug("Error parsing My Wave track: %s", err)
        if first_track_id_this_batch is not None:
            self._my_wave_playlist_next_cursor = first_track_id_this_batch
        else:
            # All tracks in this batch were duplicates or failed to parse;
            # clear cursor so the next page call returns [] instead of re-fetching
            self._my_wave_playlist_next_cursor = None
        return tracks

    async def _get_liked_tracks_playlist_tracks(self, page: int) -> list[Track]:
        """Get liked tracks for virtual playlist (sorted in reverse chronological order).

        :param page: Page number (0 = all tracks limited by config, >0 = empty for pagination).
        :return: List of Track objects.
        """
        self.logger.debug("_get_liked_tracks_playlist_tracks called with page=%s", page)
        # Liked tracks API returns all tracks at once, so only return tracks on page 0
        if page > 0:
            self.logger.debug("Returning empty list for page > 0")
            return []

        max_tracks_config = int(
            self.config.get_value(CONF_LIKED_TRACKS_MAX_TRACKS) or 500  # type: ignore[arg-type]
        )
        self.logger.debug("Max tracks config: %s", max_tracks_config)

        # Fetch liked tracks (already sorted in reverse chronological order by api_client)
        track_shorts = await self.client.get_liked_tracks()
        self.logger.debug("Got %s liked tracks from API", len(track_shorts))
        if not track_shorts:
            self.logger.warning("No liked tracks found!")
            return []

        # Apply max tracks limit
        track_shorts = track_shorts[:max_tracks_config]
        self.logger.debug("After limit, processing %s tracks", len(track_shorts))

        # Fetch full track details in batches
        track_ids = [str(ts.track_id) for ts in track_shorts if ts.track_id]
        self.logger.debug("Extracted %s track IDs. First 3: %s", len(track_ids), track_ids[:3])

        batch_size = TRACK_BATCH_SIZE
        full_tracks = []
        for i in range(0, len(track_ids), batch_size):
            batch_ids = track_ids[i : i + batch_size]
            self.logger.debug("Fetching batch %s: %s tracks", i // batch_size + 1, len(batch_ids))
            batch_result = await self.client.get_tracks(batch_ids)
            self.logger.debug("Batch returned %s tracks", len(batch_result))
            full_tracks.extend(batch_result)

        self.logger.debug("Total full_tracks fetched: %s", len(full_tracks))

        # Create track ID to full track mapping by track ID directly
        track_map = {}
        for t in full_tracks:
            if hasattr(t, "id") and t.id:
                track_map[str(t.id)] = t
        self.logger.debug("Created track_map with %s entries", len(track_map))

        # Parse tracks in the original order (reverse chronological)
        tracks = []
        for track_id in track_ids:
            # track_id may be compound "trackId:albumId", extract base ID for lookup
            base_id = track_id.split(":")[0] if ":" in track_id else track_id
            found = track_map.get(track_id) or track_map.get(base_id)
            if found:
                try:
                    tracks.append(parse_track(self, found))
                except InvalidDataError as err:
                    self.logger.debug("Error parsing liked track %s: %s", track_id, err)
            else:
                self.logger.debug("Track ID %s not found in track_map", track_id)

        self.logger.debug("Successfully parsed %s tracks", len(tracks))
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
        """Get similar tracks using Yandex Rotor station for this track.

        Uses rotor station track:{id} so MA radio mode gets Yandex recommendations.

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

    async def recommendations(self) -> list[RecommendationFolder]:
        """Get recommendations with multiple discovery folders.

        Returns My Wave, Feed (Made for You), Chart, New Releases, and
        New Playlists sections.

        :return: List of recommendation folders.
        """
        folders: list[RecommendationFolder] = []

        folder = await self._get_my_wave_recommendations()
        if folder:
            folders.append(folder)

        folder = await self._get_feed_recommendations()
        if folder:
            folders.append(folder)

        folder = await self._get_chart_recommendations()
        if folder:
            folders.append(folder)

        folder = await self._get_new_releases_recommendations()
        if folder:
            folders.append(folder)

        folder = await self._get_new_playlists_recommendations()
        if folder:
            folders.append(folder)

        # Picks & Mixes recommendations
        folder = await self._get_top_picks_recommendations()
        if folder:
            folders.append(folder)

        folder = await self._get_mood_mix_recommendations()
        if folder:
            folders.append(folder)

        folder = await self._get_activity_mix_recommendations()
        if folder:
            folders.append(folder)

        folder = await self._get_seasonal_mix_recommendations()
        if folder:
            folders.append(folder)

        return folders

    @use_cache(600)
    async def _get_my_wave_recommendations(self) -> RecommendationFolder | None:
        """Get My Wave recommendation folder with personalized tracks.

        :return: RecommendationFolder with My Wave tracks, or None if empty.
        """
        max_tracks_config = int(
            self.config.get_value(CONF_MY_WAVE_MAX_TRACKS) or 150  # type: ignore[arg-type]
        )
        batch_size_config = MY_WAVE_BATCH_SIZE

        seen_track_ids: set[str] = set()
        items: list[Track] = []
        queue: str | int | None = None

        for _ in range(batch_size_config):
            if len(seen_track_ids) >= max_tracks_config:
                break

            yandex_tracks, _ = await self.client.get_my_wave_tracks(queue=queue)
            if not yandex_tracks:
                break

            first_track_id_this_batch = None
            for yt in yandex_tracks:
                if len(seen_track_ids) >= max_tracks_config:
                    break

                try:
                    t = parse_track(self, yt)
                    track_id = (
                        str(yt.id) if hasattr(yt, "id") and yt.id else getattr(yt, "track_id", None)
                    )
                    if track_id:
                        if track_id in seen_track_ids:
                            continue

                        seen_track_ids.add(track_id)

                        if first_track_id_this_batch is None:
                            first_track_id_this_batch = track_id
                        t.item_id = f"{track_id}{RADIO_TRACK_ID_SEP}{ROTOR_STATION_MY_WAVE}"
                        for pm in t.provider_mappings:
                            if pm.provider_instance == self.instance_id:
                                pm.item_id = t.item_id
                                break
                    items.append(t)
                except InvalidDataError as err:
                    self.logger.debug("Error parsing My Wave track for recommendations: %s", err)

            queue = first_track_id_this_batch
            if not queue:
                break

        if not items:
            return None

        initial_tracks_limit = DISCOVERY_INITIAL_TRACKS
        if len(items) > initial_tracks_limit:
            items = items[:initial_tracks_limit]

        names = self._get_browse_names()
        return RecommendationFolder(
            item_id=MY_WAVE_PLAYLIST_ID,
            provider=self.instance_id,
            name=names[MY_WAVE_PLAYLIST_ID],
            items=UniqueList(items),
            icon="mdi-waveform",
        )

    @use_cache(1800)
    async def _get_feed_recommendations(self) -> RecommendationFolder | None:
        """Get personalized feed playlists (Playlist of the Day, DejaVu, etc.).

        :return: RecommendationFolder with generated playlists, or None if unavailable.
        """
        feed = await self.client.get_feed()
        if not feed or not feed.generated_playlists:
            return None
        items: list[Playlist] = []
        for gen_playlist in feed.generated_playlists:
            if gen_playlist.data and gen_playlist.ready:
                try:
                    items.append(parse_playlist(self, gen_playlist.data))
                except InvalidDataError as err:
                    self.logger.debug("Error parsing feed playlist: %s", err)
        if not items:
            return None
        names = self._get_browse_names()
        return RecommendationFolder(
            item_id="feed",
            provider=self.instance_id,
            name=names["feed"],
            items=UniqueList(items),
            icon="mdi-account-music",
        )

    @use_cache(3600)
    async def _get_chart_recommendations(self) -> RecommendationFolder | None:
        """Get chart tracks (hot tracks of the month).

        :return: RecommendationFolder with chart tracks, or None if unavailable.
        """
        chart_info = await self.client.get_chart()
        if not chart_info or not chart_info.chart:
            return None
        playlist = chart_info.chart
        if not playlist.tracks:
            return None
        # TrackShort objects in chart context have .track (full Track) and .chart (position)
        tracks: list[Track] = []
        for track_short in playlist.tracks[:20]:
            track_obj = getattr(track_short, "track", None)
            if not track_obj:
                continue
            try:
                tracks.append(parse_track(self, track_obj))
            except InvalidDataError as err:
                self.logger.debug("Error parsing chart track: %s", err)
        if not tracks:
            return None
        names = self._get_browse_names()
        return RecommendationFolder(
            item_id="chart",
            provider=self.instance_id,
            name=names["chart"],
            items=UniqueList(tracks),
            icon="mdi-chart-line",
        )

    @use_cache(3600)
    async def _get_new_releases_recommendations(self) -> RecommendationFolder | None:
        """Get new album releases.

        :return: RecommendationFolder with new albums, or None if unavailable.
        """
        releases = await self.client.get_new_releases()
        if not releases or not releases.new_releases:
            return None
        # new_releases is a list of album IDs (int) — need to batch-fetch full details
        album_ids = [str(aid) for aid in releases.new_releases[:20]]
        if not album_ids:
            return None
        full_albums = await self.client.get_albums(album_ids)
        if not full_albums:
            return None
        albums: list[Album] = []
        for album in full_albums:
            try:
                albums.append(parse_album(self, album))
            except InvalidDataError as err:
                self.logger.debug("Error parsing new release album: %s", err)
        if not albums:
            return None
        names = self._get_browse_names()
        return RecommendationFolder(
            item_id="new_releases",
            provider=self.instance_id,
            name=names["new_releases"],
            items=UniqueList(albums),
            icon="mdi-new-box",
        )

    @use_cache(3600)
    async def _get_new_playlists_recommendations(self) -> RecommendationFolder | None:
        """Get new editorial playlists.

        :return: RecommendationFolder with new playlists, or None if unavailable.
        """
        result = await self.client.get_new_playlists()
        if not result or not result.new_playlists:
            return None
        # new_playlists is a list of PlaylistId objects (uid, kind) — fetch full details
        playlist_ids = [
            f"{pid.uid}:{pid.kind}"
            for pid in result.new_playlists[:20]
            if hasattr(pid, "uid") and hasattr(pid, "kind")
        ]
        if not playlist_ids:
            return None
        full_playlists = await self.client.get_playlists(playlist_ids)
        if not full_playlists:
            return None
        playlists: list[Playlist] = []
        for playlist in full_playlists:
            try:
                playlists.append(parse_playlist(self, playlist))
            except InvalidDataError as err:
                self.logger.debug("Error parsing new playlist: %s", err)
        if not playlists:
            return None
        names = self._get_browse_names()
        return RecommendationFolder(
            item_id="new_playlists",
            provider=self.instance_id,
            name=names["new_playlists"],
            items=UniqueList(playlists),
            icon="mdi-playlist-star",
        )

    @use_cache(3600)
    async def _get_top_picks_recommendations(self) -> RecommendationFolder | None:
        """Get Top Picks recommendation folder (tag: top).

        :return: RecommendationFolder with top playlists, or None if unavailable.
        """
        playlists = await self.client.get_tag_playlists("top")
        if not playlists:
            return None
        items: list[Playlist] = []
        for playlist in playlists[:10]:
            try:
                items.append(parse_playlist(self, playlist))
            except InvalidDataError as err:
                self.logger.debug("Error parsing top picks playlist: %s", err)
        if not items:
            return None
        names = self._get_browse_names()
        return RecommendationFolder(
            item_id="top_picks",
            provider=self.instance_id,
            name=names.get("top_picks", "Top Picks"),
            items=UniqueList(items),
            icon="mdi-star",
        )

    @use_cache(1800)
    async def _get_mood_mix_recommendations(self) -> RecommendationFolder | None:
        """Get Mood Mix recommendation folder (rotating mood tags).

        :return: RecommendationFolder with mood playlists, or None if unavailable.
        """
        # Rotate through mood tags
        mood_tag = random.choice(TAG_CATEGORY_MOOD)
        playlists = await self.client.get_tag_playlists(mood_tag)
        if not playlists:
            return None
        items: list[Playlist] = []
        for playlist in playlists[:8]:
            try:
                items.append(parse_playlist(self, playlist))
            except InvalidDataError as err:
                self.logger.debug("Error parsing mood playlist: %s", err)
        if not items:
            return None
        names = self._get_browse_names()
        tag_name = names.get(mood_tag, mood_tag.title())
        return RecommendationFolder(
            item_id="mood_mix",
            provider=self.instance_id,
            name=f"{names.get('mood_mix', 'Mood')}: {tag_name}",
            items=UniqueList(items),
            icon="mdi-emoticon-outline",
        )

    @use_cache(1800)
    async def _get_activity_mix_recommendations(self) -> RecommendationFolder | None:
        """Get Activity Mix recommendation folder (rotating activity tags).

        :return: RecommendationFolder with activity playlists, or None if unavailable.
        """
        # Rotate through activity tags
        activity_tag = random.choice(TAG_CATEGORY_ACTIVITY)
        playlists = await self.client.get_tag_playlists(activity_tag)
        if not playlists:
            return None
        items: list[Playlist] = []
        for playlist in playlists[:8]:
            try:
                items.append(parse_playlist(self, playlist))
            except InvalidDataError as err:
                self.logger.debug("Error parsing activity playlist: %s", err)
        if not items:
            return None
        names = self._get_browse_names()
        tag_name = names.get(activity_tag, activity_tag.title())
        return RecommendationFolder(
            item_id="activity_mix",
            provider=self.instance_id,
            name=f"{names.get('activity_mix', 'Activity')}: {tag_name}",
            items=UniqueList(items),
            icon="mdi-run",
        )

    @use_cache(3600 * 6)
    async def _get_seasonal_mix_recommendations(self) -> RecommendationFolder | None:
        """Get Seasonal Mix recommendation folder (based on current month).

        :return: RecommendationFolder with seasonal playlists, or None if unavailable.
        """
        # Determine current season tag
        current_month = datetime.now(tz=UTC).month
        seasonal_tag = TAG_SEASONAL_MAP.get(current_month, "autumn")

        # Handle spring fallback (spring tag may not exist)
        if seasonal_tag == "spring":
            seasonal_tag = "autumn"  # Fallback

        playlists = await self.client.get_tag_playlists(seasonal_tag)
        if not playlists:
            return None
        items: list[Playlist] = []
        for playlist in playlists[:8]:
            try:
                items.append(parse_playlist(self, playlist))
            except InvalidDataError as err:
                self.logger.debug("Error parsing seasonal playlist: %s", err)
        if not items:
            return None
        names = self._get_browse_names()
        tag_name = names.get(seasonal_tag, seasonal_tag.title())
        return RecommendationFolder(
            item_id="seasonal_mix",
            provider=self.instance_id,
            name=f"{names.get('seasonal_mix', 'Seasonal')}: {tag_name}",
            items=UniqueList(items),
            icon="mdi-weather-sunny",
        )

    @use_cache(3600 * 3)
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks.

        :param prov_playlist_id: The provider playlist ID (format: "owner_id:kind",
            my_wave, or liked_tracks).
        :param page: Page number for pagination.
        :return: List of Track objects.
        """
        self.logger.debug(
            "get_playlist_tracks called: prov_playlist_id=%s, page=%s", prov_playlist_id, page
        )

        if prov_playlist_id == MY_WAVE_PLAYLIST_ID:
            self.logger.debug("Fetching My Wave tracks")
            return await self._get_my_wave_playlist_tracks(page)

        if prov_playlist_id == LIKED_TRACKS_PLAYLIST_ID:
            self.logger.debug("Fetching Liked Tracks for virtual playlist")
            result = await self._get_liked_tracks_playlist_tracks(page)
            self.logger.debug("Liked Tracks playlist returned %s tracks", len(result))
            return result

        # Yandex Music API returns all playlist tracks in one call (no server-side pagination).
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

        # Yandex returns TrackShort objects, we need to fetch full track info
        track_ids = [
            str(track.track_id) if hasattr(track, "track_id") else str(track.id)
            for track in tracks_list
            if track
        ]
        if not track_ids:
            return []

        # Fetch full track details in batches to avoid timeouts
        batch_size = TRACK_BATCH_SIZE
        full_tracks = []
        for i in range(0, len(track_ids), batch_size):
            batch = track_ids[i : i + batch_size]
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
        """Retrieve library artists from Yandex Music."""
        artists = await self.client.get_liked_artists()
        for artist in artists:
            try:
                yield parse_artist(self, artist)
            except InvalidDataError as err:
                self.logger.debug("Error parsing library artist: %s", err)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from Yandex Music."""
        batch_size = TRACK_BATCH_SIZE
        albums = await self.client.get_liked_albums(batch_size=batch_size)
        for album in albums:
            try:
                yield parse_album(self, album)
            except InvalidDataError as err:
                self.logger.debug("Error parsing library album: %s", err)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Yandex Music."""
        track_shorts = await self.client.get_liked_tracks()
        if not track_shorts:
            return

        # Fetch full track details in batches
        track_ids = [str(ts.track_id) for ts in track_shorts if ts.track_id]
        batch_size = TRACK_BATCH_SIZE
        for i in range(0, len(track_ids), batch_size):
            batch_ids = track_ids[i : i + batch_size]
            full_tracks = await self.client.get_tracks(batch_ids)
            for track in full_tracks:
                try:
                    yield parse_track(self, track)
                except InvalidDataError as err:
                    self.logger.debug("Error parsing library track: %s", err)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve library playlists from Yandex Music.

        Includes virtual playlists (My Wave and Liked Tracks if enabled), then user playlists.
        """
        # Include My Wave playlist (always enabled)
        self.logger.debug("My Wave playlist enabled: %s", True)
        yield await self.get_playlist(MY_WAVE_PLAYLIST_ID)
        # Include Liked Tracks playlist (always enabled)
        self.logger.debug("Liked Tracks playlist enabled: %s", True)
        self.logger.debug("Yielding Liked Tracks playlist with ID: %s", LIKED_TRACKS_PLAYLIST_ID)
        yield await self.get_playlist(LIKED_TRACKS_PLAYLIST_ID)
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

        :param item_id: The track ID (or track_id@station_id for My Wave).
        :param media_type: The media type (should be TRACK).
        :return: StreamDetails for the track.
        """
        return await self.streaming.get_stream_details(item_id)

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item.

        This method is called when StreamType.CUSTOM is used, enabling on-the-fly
        decryption of encrypted FLAC streams without disk I/O.

        :param streamdetails: Stream details containing encrypted URL and decryption key.
        :param seek_position: Seek position in seconds (not supported for encrypted streams).
        :return: Async generator yielding decrypted audio chunks.
        """
        async for chunk in self.streaming.get_audio_stream(streamdetails, seek_position):
            yield chunk

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """Report playback for rotor feedback when the track is from My Wave.

        Sends trackStarted when the track is currently playing (is_playing=True).
        trackFinished/skip are sent from on_streamed to use accurate seconds_streamed.
        """
        # Radio feedback always enabled
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
                batch_id=self._my_wave_batch_id,
            )

    async def on_streamed(self, streamdetails: StreamDetails) -> None:
        """Report stream completion for My Wave rotor feedback and cleanup temp files.

        Sends trackFinished or skip with actual seconds_streamed so Yandex
        can improve recommendations. Also cleans up any temp files from preload mode.
        """
        # Clean up temp file if this was a preloaded stream
        if self._streaming:
            self._streaming.cleanup_temp_file(streamdetails.item_id)

        # Radio feedback always enabled
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
            batch_id=self._my_wave_batch_id,
        )
