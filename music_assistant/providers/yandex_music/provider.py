"""Yandex Music provider implementation."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime
from io import BytesIO
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ImageType, MediaType, ProviderFeature
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
    MediaItemImage,
    MediaItemType,
    Playlist,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from PIL import Image as PilImage

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .api_client import YandexMusicClient
from .constants import (
    BROWSE_INITIAL_TRACKS,
    BROWSE_NAMES_EN,
    BROWSE_NAMES_RU,
    COLLECTION_FOLDER_ID,
    CONF_BASE_URL,
    CONF_LIKED_TRACKS_MAX_TRACKS,
    CONF_MY_WAVE_MAX_TRACKS,
    CONF_TOKEN,
    DEFAULT_BASE_URL,
    DISCOVERY_INITIAL_TRACKS,
    FOR_YOU_FOLDER_ID,
    IMAGE_SIZE_MEDIUM,
    LIKED_TRACKS_PLAYLIST_ID,
    MY_WAVE_BATCH_SIZE,
    MY_WAVE_PLAYLIST_ID,
    MY_WAVES_FOLDER_ID,
    MY_WAVES_SET_FOLDER_ID,
    PLAYLIST_ID_SPLITTER,
    RADIO_FOLDER_ID,
    RADIO_TRACK_ID_SEP,
    ROTOR_STATION_MY_WAVE,
    TAG_CATEGORY_ACTIVITY,
    TAG_CATEGORY_ERA,
    TAG_CATEGORY_GENRES,
    TAG_CATEGORY_MOOD,
    TAG_CATEGORY_ORDER,
    TAG_MIXES,
    TAG_SEASONAL_MAP,
    TAG_SLUG_CATEGORY,
    TRACK_BATCH_SIZE,
    WAVE_CATEGORY_DISPLAY_ORDER,
    WAVES_FOLDER_ID,
    WAVES_LANDING_FOLDER_ID,
)
from .parsers import (
    _get_image_url as get_image_url,
)
from .parsers import (
    get_canonical_provider_name,
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
)
from .streaming import YandexMusicStreamingManager

if TYPE_CHECKING:
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


class _WaveState:
    """Per-station mutable state for rotor wave playback."""

    def __init__(self) -> None:
        self.batch_id: str | None = None
        self.last_track_id: str | None = None
        self.seen_track_ids: set[str] = set()
        self.radio_started_sent: bool = False
        self.lock: asyncio.Lock = asyncio.Lock()


class YandexMusicProvider(MusicProvider):
    """Implementation of a Yandex Music MusicProvider."""

    _client: YandexMusicClient | None = None
    _streaming: YandexMusicStreamingManager | None = None
    _my_wave_batch_id: str | None = None
    _my_wave_last_track_id: str | None = None  # last track id for "Load more" (API queue param)
    _my_wave_playlist_next_cursor: str | None = None  # first_track_id for next playlist page
    _my_wave_radio_started_sent: bool = False
    _my_wave_seen_track_ids: set[str]  # Track IDs seen in current My Wave session
    _my_wave_lock: asyncio.Lock  # Protects My Wave mutable state
    _wave_states: dict[str, _WaveState]  # Per-station state for tagged wave stations
    _wave_bg_colors: dict[str, str]  # image_url -> hex bg color for transparent covers

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
        self._my_wave_lock = asyncio.Lock()
        # Initialize per-station wave state dict
        self._wave_states = {}
        self._wave_bg_colors = {}
        self.logger.info("Successfully connected to Yandex Music")

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

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
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
            async with self._my_wave_lock:
                return await self._browse_my_wave(path, sub_subpath)

        # For You folder (picks + mixes)
        if subpath == FOR_YOU_FOLDER_ID:
            return await self._browse_for_you(path, path_parts)

        # Collection folder (library items)
        if subpath == COLLECTION_FOLDER_ID:
            return await self._browse_collection(path)

        # Handle picks/ path (mood, activity, era, genres)
        if subpath == "picks":
            return await self._browse_picks(path, path_parts)

        # Handle mixes/ path (seasonal collections)
        if subpath == "mixes":
            return await self._browse_mixes(path, path_parts)

        # Handle waves/ and radio/ paths (rotor stations by genre/mood/activity)
        if subpath in (WAVES_FOLDER_ID, RADIO_FOLDER_ID):
            return await self._browse_waves(path, path_parts)

        # Handle my_waves_set/ path (AI Wave Sets from /landing-blocks/mixes-waves)
        if subpath == MY_WAVES_SET_FOLDER_ID:
            return await self._browse_vibe_sets(path, path_parts)

        # Handle waves_landing/ path (Featured Waves from /landing-blocks/waves)
        if subpath == WAVES_LANDING_FOLDER_ID:
            return await self._browse_waves_landing(path, path_parts)

        # Handle direct tag subpath (when folder is played by URI, the full path
        # "picks/category/tag" is lost and only the tag slug arrives as subpath).
        # Skip the API call for standard top-level folders that are never tag slugs.
        _known_folders = {
            "artists",
            "albums",
            "tracks",
            "playlists",
            LIKED_TRACKS_PLAYLIST_ID,
            WAVES_FOLDER_ID,
            RADIO_FOLDER_ID,
            MY_WAVES_FOLDER_ID,
            MY_WAVES_SET_FOLDER_ID,
            WAVES_LANDING_FOLDER_ID,
            FOR_YOU_FOLDER_ID,
            COLLECTION_FOLDER_ID,
        }
        if subpath and subpath not in _known_folders:
            # Handle direct wave station_id (e.g. "activity:workout") passed when
            # MA plays a wave station folder using its item_id as the path subpath.
            # Station IDs have format "category:tag" where category is non-numeric.
            if ":" in subpath:
                cat_part = subpath.split(":", 1)[0]
                if not cat_part.isdigit():
                    return await self._browse_wave_station(subpath)

            discovered_tags = await self._get_discovered_tag_slugs()
            if subpath in discovered_tags:
                return await self._get_tag_playlists_as_browse(subpath)

        if subpath:
            return await super().browse(path)

        names = self._get_browse_names()

        folders: list[BrowseFolder] = []
        base = path if path.endswith("//") else path.rstrip("/") + "/"
        # My Wave folder (always enabled — Яндекс «Моя волна»)
        folders.append(
            BrowseFolder(
                item_id=MY_WAVE_PLAYLIST_ID,
                provider=self.instance_id,
                path=f"{base}{MY_WAVE_PLAYLIST_ID}",
                name=names[MY_WAVE_PLAYLIST_ID],
                is_playable=True,
            )
        )
        # For You folder — Picks + Mixes (Яндекс «Для вас»)
        folders.append(
            BrowseFolder(
                item_id=FOR_YOU_FOLDER_ID,
                provider=self.instance_id,
                path=f"{base}{FOR_YOU_FOLDER_ID}",
                name=names.get(FOR_YOU_FOLDER_ID, "For You"),
                is_playable=False,
            )
        )
        # Collection folder — library items (Яндекс «Коллекция»)
        has_library = any(
            f in self.supported_features
            for f in (
                ProviderFeature.LIBRARY_ARTISTS,
                ProviderFeature.LIBRARY_ALBUMS,
                ProviderFeature.LIBRARY_TRACKS,
                ProviderFeature.LIBRARY_PLAYLISTS,
            )
        )
        if has_library:
            folders.append(
                BrowseFolder(
                    item_id=COLLECTION_FOLDER_ID,
                    provider=self.instance_id,
                    path=f"{base}{COLLECTION_FOLDER_ID}",
                    name=names.get(COLLECTION_FOLDER_ID, "Collection"),
                    is_playable=False,
                )
            )
        # Radio folder — rotor stations (Яндекс волны, renamed to Radio)
        folders.append(
            BrowseFolder(
                item_id=RADIO_FOLDER_ID,
                provider=self.instance_id,
                path=f"{base}{RADIO_FOLDER_ID}",
                name=names.get(RADIO_FOLDER_ID, "Radio"),
                is_playable=False,
            )
        )
        # AI Wave Sets — parametric stations from /landing-blocks/mixes-waves
        folders.append(
            BrowseFolder(
                item_id=MY_WAVES_SET_FOLDER_ID,
                provider=self.instance_id,
                path=f"{base}{MY_WAVES_SET_FOLDER_ID}",
                name=names.get(MY_WAVES_SET_FOLDER_ID, "AI Wave Sets"),
                is_playable=False,
            )
        )
        if len(folders) == 1:
            return await self.browse(folders[0].path)
        return folders

    async def _browse_my_wave(
        self, path: str, sub_subpath: str | None
    ) -> list[Track | BrowseFolder]:
        """Browse My Wave tracks (must be called under _my_wave_lock).

        :param path: Full browse path.
        :param sub_subpath: Sub-path part ('next' for load more, or track_id cursor).
        :return: List of Track and optional BrowseFolder for "Load more".
        """
        max_tracks_config = int(
            self.config.get_value(CONF_MY_WAVE_MAX_TRACKS) or 150  # type: ignore[arg-type]
        )
        batch_size_config = MY_WAVE_BATCH_SIZE

        # Effective limit on tracks to collect for this call:
        # initial browse is capped to BROWSE_INITIAL_TRACKS to avoid marking
        # extra tracks as "seen" that are never shown to the user.
        effective_limit = min(
            BROWSE_INITIAL_TRACKS if sub_subpath != "next" else max_tracks_config,
            max_tracks_config,
        )

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
            if total_track_count >= effective_limit:
                break

            yandex_tracks, batch_id = await self.client.get_my_wave_tracks(queue=queue)
            if batch_id:
                self._my_wave_batch_id = batch_id
                last_batch_id = batch_id
            if not self._my_wave_radio_started_sent and yandex_tracks:
                sent = await self.client.send_rotor_station_feedback(
                    ROTOR_STATION_MY_WAVE,
                    "radioStarted",
                    batch_id=batch_id,
                )
                if sent:
                    self._my_wave_radio_started_sent = True
            first_track_id_this_batch = None
            for yt in yandex_tracks:
                if total_track_count >= effective_limit:
                    break

                track = self._parse_my_wave_track(yt, self._my_wave_seen_track_ids)
                if track is None:
                    continue
                all_tracks.append(track)
                total_track_count += 1

                track_id = track.item_id.split(RADIO_TRACK_ID_SEP, 1)[0]
                if first_track_id_this_batch is None:
                    first_track_id_this_batch = track_id

            if first_track_id_this_batch is not None:
                self._my_wave_last_track_id = first_track_id_this_batch
            if (
                first_track_id_this_batch is None
                or not batch_id
                or not yandex_tracks
                or total_track_count >= effective_limit
            ):
                break
            queue = first_track_id_this_batch

        # Only show "Load more" if we haven't reached the limit and there's more data
        if last_batch_id and total_track_count < max_tracks_config:
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

    def _parse_my_wave_track(self, yt: Any, seen_ids: set[str]) -> Track | None:
        """Parse a Yandex track into a My Wave Track with composite item_id.

        Extracts the track_id, checks for duplicates in the seen_ids set,
        sets composite item_id (track_id@station_id), and updates provider_mappings.
        Callers using shared state must hold _my_wave_lock.

        :param yt: Yandex track object from rotor station response.
        :param seen_ids: Set of already-seen track IDs to check and update.
        :return: Parsed Track with composite item_id, or None if duplicate/invalid.
        """
        try:
            t = parse_track(self, yt)
        except InvalidDataError as err:
            self.logger.debug("Error parsing My Wave track: %s", err)
            return None

        track_id = str(yt.id) if hasattr(yt, "id") and yt.id else getattr(yt, "track_id", None)
        if not track_id:
            return t

        if track_id in seen_ids:
            self.logger.debug("Skipping duplicate My Wave track: %s", track_id)
            return None

        seen_ids.add(track_id)
        t.item_id = f"{track_id}{RADIO_TRACK_ID_SEP}{ROTOR_STATION_MY_WAVE}"
        for pm in t.provider_mappings:
            if pm.provider_instance == self.instance_id:
                pm.item_id = t.item_id
                break
        return t

    @use_cache(3600)
    async def _validate_tag(self, tag_slug: str) -> bool:
        """Check if a tag has playlists by calling client.get_tag_playlists().

        :param tag_slug: Tag identifier (e.g. 'chill', '80s').
        :return: True if the tag has at least one playlist.
        """
        try:
            playlists = await self.client.get_tag_playlists(tag_slug)
            return len(playlists) > 0
        except Exception as err:
            self.logger.debug("Tag validation failed for %s: %s", tag_slug, err)
            return False

    @use_cache(3600)
    async def _get_valid_tags_for_category(self, category: str) -> list[str]:
        """Get validated tags for a category (only those with playlists).

        Combines hardcoded tags from the category lists with any landing-discovered
        tags, validates each by calling client.tags(), and returns only those with
        playlists.

        :param category: Category name ('mood', 'activity', 'era', 'genres').
        :return: List of valid tag slugs.
        """
        category_lists: dict[str, list[str]] = {
            "mood": list(TAG_CATEGORY_MOOD),
            "activity": list(TAG_CATEGORY_ACTIVITY),
            "era": list(TAG_CATEGORY_ERA),
            "genres": list(TAG_CATEGORY_GENRES),
        }
        tags = category_lists.get(category, [])

        # Add landing-discovered tags for this category
        try:
            landing_tags = await self.client.get_landing_tags()
            for slug, _title in landing_tags:
                cat = TAG_SLUG_CATEGORY.get(slug, "mood")
                if cat == category and slug not in tags:
                    tags.append(slug)
        except Exception as err:
            self.logger.debug("Landing tag discovery failed: %s", err)

        # Validate tags in parallel with bounded concurrency
        sem = asyncio.Semaphore(8)

        async def _check(tag: str) -> str | None:
            async with sem:
                return tag if await self._validate_tag(tag) else None

        results = await asyncio.gather(*[_check(tag) for tag in tags])
        return [tag for tag in results if tag is not None]

    @use_cache(3600)
    async def _get_discovered_tags(self, locale: str) -> list[tuple[str, str]]:
        """Get all available tags by combining hardcoded tags with landing discovery.

        Starts with all hardcoded tags from category lists, adds landing-discovered
        tags, validates each via client.tags(), and returns only those with playlists.
        Results are cached for 1 hour. The locale parameter is included in the cache
        key so that a locale change invalidates the cached result.

        :param locale: Current metadata locale (used as part of cache key).
        :return: List of (slug, title) tuples for tags that have playlists.
        """
        names = self._get_browse_names()

        # Collect all hardcoded tags (non-seasonal)
        all_tags: dict[str, str] = {}
        for slug, cat in TAG_SLUG_CATEGORY.items():
            if cat != "seasonal":
                all_tags[slug] = names.get(slug, slug.title())

        # Add landing-discovered tags
        try:
            landing_tags = await self.client.get_landing_tags()
            for slug, title in landing_tags:
                if slug not in all_tags:
                    all_tags[slug] = title
        except Exception as err:
            self.logger.debug("Failed to discover tags from landing API: %s", err)

        # Validate tags in parallel with bounded concurrency
        sem = asyncio.Semaphore(8)

        async def _check(slug: str) -> bool:
            async with sem:
                return await self._validate_tag(slug)

        tag_items = list(all_tags.items())
        results = await asyncio.gather(*[_check(slug) for slug, _ in tag_items])
        return [
            (slug, title) for (slug, title), valid in zip(tag_items, results, strict=True) if valid
        ]

    async def _get_discovered_tag_slugs(self) -> set[str]:
        """Get set of all valid tag slugs (cached).

        :return: Set of tag slug strings that have playlists.
        """
        discovered = await self._get_discovered_tags(self.mass.metadata.locale or "en_US")
        return {slug for slug, _title in discovered}

    async def _browse_for_you(
        self, path: str, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse «For You» folder — shows Picks and Mixes sub-folders.

        :param path: Full browse path.
        :param path_parts: Split path parts after ://.
        :return: List of sub-folders (Picks, Mixes).
        """
        names = self._get_browse_names()
        # Strip the for_you segment to build child paths that route to picks/mixes
        # Path format: ...//for_you  → child paths should be ...//picks, ...//mixes
        # We build base from the root (before for_you) by dropping the last segment.
        base_parts = path.split("//", 1)
        root_base = (base_parts[0] + "//") if len(base_parts) > 1 else path.rstrip("/") + "/"

        if len(path_parts) == 1:
            return [
                BrowseFolder(
                    item_id="picks",
                    provider=self.instance_id,
                    path=f"{root_base}picks",
                    name=names.get("picks", "Picks"),
                    is_playable=False,
                ),
                BrowseFolder(
                    item_id="mixes",
                    provider=self.instance_id,
                    path=f"{root_base}mixes",
                    name=names.get("mixes", "Mixes"),
                    is_playable=False,
                ),
            ]
        # Deeper path: delegate to picks or mixes handler via canonical paths
        return await super().browse(path)

    async def _browse_collection(
        self, path: str
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse «Collection» folder — shows library sub-folders (tracks/artists/albums/playlists).

        :param path: Full browse path.
        :return: List of library sub-folders.
        """
        names = self._get_browse_names()
        base_parts = path.split("//", 1)
        root_base = (base_parts[0] + "//") if len(base_parts) > 1 else path.rstrip("/") + "/"

        folders: list[BrowseFolder] = []
        if ProviderFeature.LIBRARY_TRACKS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="tracks",
                    provider=self.instance_id,
                    path=f"{root_base}tracks",
                    name=names["tracks"],
                    is_playable=True,
                )
            )
        if ProviderFeature.LIBRARY_ARTISTS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="artists",
                    provider=self.instance_id,
                    path=f"{root_base}artists",
                    name=names["artists"],
                    is_playable=True,
                )
            )
        if ProviderFeature.LIBRARY_ALBUMS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="albums",
                    provider=self.instance_id,
                    path=f"{root_base}albums",
                    name=names["albums"],
                    is_playable=True,
                )
            )
        if ProviderFeature.LIBRARY_PLAYLISTS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="playlists",
                    provider=self.instance_id,
                    path=f"{root_base}playlists",
                    name=names["playlists"],
                    is_playable=True,
                )
            )
        return folders

    async def _browse_picks(
        self, path: str, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse picks folder using hardcoded tags validated against the API.

        Tags are sourced from hardcoded category lists and landing API discovery,
        then validated via client.tags() to ensure they have playlists.
        Only categories with at least one valid tag are shown.

        :param path: Full browse path.
        :param path_parts: Split path parts after ://.
        :return: List of folders or playlists.
        """
        names = self._get_browse_names()
        base = path.rstrip("/") + "/"

        # Get validated tags
        discovered = await self._get_discovered_tags(self.mass.metadata.locale or "en_US")

        # Categorize valid tags
        categorized: dict[str, list[tuple[str, str]]] = {}
        for slug, title in discovered:
            cat = TAG_SLUG_CATEGORY.get(slug, "mood")
            # Skip seasonal tags — they belong in mixes, not picks
            if cat == "seasonal":
                continue
            categorized.setdefault(cat, []).append((slug, title))

        # Sort tags within each category by preferred order
        for cat, cat_tags in categorized.items():
            order = TAG_CATEGORY_ORDER.get(cat, [])
            order_map = {s: i for i, s in enumerate(order)}
            cat_tags.sort(key=lambda t: order_map.get(t[0], len(order)))

        # picks/ - show category folders (only those with valid tags)
        if len(path_parts) == 1:
            category_display_order = ["mood", "activity", "era", "genres"]
            folders: list[BrowseFolder] = []
            for cat in category_display_order:
                if cat in categorized:
                    folders.append(
                        BrowseFolder(
                            item_id=cat,
                            provider=self.instance_id,
                            path=f"{base}{cat}",
                            name=names.get(cat, cat.title()),
                            is_playable=False,
                        )
                    )
            # Show any extra categories not in the standard order
            for cat in categorized:
                if cat not in category_display_order:
                    folders.append(
                        BrowseFolder(
                            item_id=cat,
                            provider=self.instance_id,
                            path=f"{base}{cat}",
                            name=names.get(cat, cat.title()),
                            is_playable=False,
                        )
                    )
            return folders

        category: str | None = path_parts[1] if len(path_parts) > 1 else None
        tag: str | None = path_parts[2] if len(path_parts) > 2 else None

        self.logger.debug(
            "Browse picks: path=%s, category=%s, tag=%s",
            path,
            category,
            tag,
        )

        # picks/category/ - show valid tag folders for this category
        if category and not tag:
            category_tags = categorized.get(category, [])
            folders = []
            for slug, title in category_tags:
                folders.append(
                    BrowseFolder(
                        item_id=slug,
                        provider=self.instance_id,
                        path=f"{base}{slug}",
                        name=names.get(slug, title),
                        is_playable=False,
                    )
                )
            self.logger.debug("Returning %d tag folders for category %s", len(folders), category)
            return folders

        # picks/category/tag - show playlists for the tag
        if tag:
            discovered_slugs = {slug for slug, _ in discovered}
            if tag in discovered_slugs:
                self.logger.debug("Fetching playlists for tag: %s", tag)
                return await self._get_tag_playlists_as_browse(tag)

        self.logger.debug("No match found, returning empty list")
        return []

    async def _browse_mixes(
        self, path: str, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse mixes folder (seasonal collections) using hardcoded tags.

        Uses TAG_MIXES directly and validates each tag via client.tags()
        to check if it has playlists. Does not depend on landing API discovery.

        :param path: Full browse path.
        :param path_parts: Split path parts after ://.
        :return: List of folders or playlists.
        """
        names = self._get_browse_names()
        base = path.rstrip("/") + "/"

        # Validate seasonal tags in parallel (no landing dependency)
        sem = asyncio.Semaphore(5)

        async def _check(tag: str) -> str | None:
            async with sem:
                return tag if await self._validate_tag(tag) else None

        results = await asyncio.gather(*[_check(t) for t in TAG_MIXES])
        available_mixes = [t for t in results if t is not None]

        # mixes/ - show seasonal folders (only valid ones)
        if len(path_parts) == 1:
            folders = []
            for t in available_mixes:
                folders.append(
                    BrowseFolder(
                        item_id=t,
                        provider=self.instance_id,
                        path=f"{base}{t}",
                        name=names.get(t, t.title()),
                        is_playable=False,
                    )
                )
            return folders

        # mixes/tag - show playlists for the tag
        tag = path_parts[1] if len(path_parts) > 1 else None
        if tag and tag in TAG_MIXES:
            return await self._get_tag_playlists_as_browse(tag)

        return []

    def _get_wave_state(self, station_id: str) -> _WaveState:
        """Get or create per-station wave state.

        :param station_id: Rotor station ID (e.g. 'genre:rock', 'mood:chill').
        :return: _WaveState instance for this station.
        """
        return self._wave_states.setdefault(station_id, _WaveState())

    async def _browse_waves(
        self, path: str, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse waves folder (rotor stations by genre/mood/activity/epoch/local).

        Fetches available stations from the Yandex rotor API and groups them by category.

        :param path: Full browse path.
        :param path_parts: Split path parts after ://.
        :return: List of folders or tracks.
        """
        names = self._get_browse_names()
        base = path.rstrip("/") + "/"

        locale = (self.mass.metadata.locale or "en_US").lower()
        language = "ru" if locale.startswith("ru") else "en"

        all_stations = await self.client.get_wave_stations(language)

        # Group stations by category, preserving image_url
        categorized: dict[str, list[tuple[str, str, str | None]]] = {}
        for station_id, cat_key, name, image_url in all_stations:
            categorized.setdefault(cat_key, []).append((station_id, name, image_url))

        # waves/ — show category folders
        if len(path_parts) == 1:
            folders: list[BrowseFolder] = []
            # Personalized "My Waves" first — only show if dashboard returns stations
            dashboard_stations = await self._get_dashboard_stations_cached()
            if dashboard_stations:
                folders.append(
                    BrowseFolder(
                        item_id=MY_WAVES_FOLDER_ID,
                        provider=self.instance_id,
                        path=f"{base}{MY_WAVES_FOLDER_ID}",
                        name=names.get(MY_WAVES_FOLDER_ID, "My Waves"),
                        is_playable=False,
                    )
                )
            # Featured Waves — only show if landing-blocks/waves returns data
            waves_landing = await self._get_waves_landing_cached()
            if waves_landing:
                folders.append(
                    BrowseFolder(
                        item_id=WAVES_LANDING_FOLDER_ID,
                        provider=self.instance_id,
                        path=f"{base}{WAVES_LANDING_FOLDER_ID}",
                        name=names.get(WAVES_LANDING_FOLDER_ID, "Featured Waves"),
                        is_playable=False,
                    )
                )
            for cat in WAVE_CATEGORY_DISPLAY_ORDER:
                if cat in categorized:
                    folders.append(
                        BrowseFolder(
                            item_id=cat,
                            provider=self.instance_id,
                            path=f"{base}{cat}",
                            name=names.get(cat, cat.title()),
                            is_playable=False,
                        )
                    )
            # Append any categories returned by API that aren't in the predefined order
            for cat in categorized:
                if cat not in WAVE_CATEGORY_DISPLAY_ORDER:
                    folders.append(
                        BrowseFolder(
                            item_id=cat,
                            provider=self.instance_id,
                            path=f"{base}{cat}",
                            name=names.get(cat, cat.title()),
                            is_playable=False,
                        )
                    )
            return folders

        category: str | None = path_parts[1] if len(path_parts) > 1 else None
        tag: str | None = path_parts[2] if len(path_parts) > 2 else None

        # waves/my_waves/ — show personalized stations from dashboard
        if category == MY_WAVES_FOLDER_ID and not tag:
            return await self._browse_my_waves_stations(path)

        # waves/waves_landing/... — redirect to Featured Waves browse
        if category == WAVES_LANDING_FOLDER_ID:
            return await self._browse_waves_landing(path, path_parts[1:])

        # waves/my_waves/<tag>[/next] — play a specific personal station
        # The full station_id has format "genre:allrock", not "my_waves:allrock".
        # Resolve by matching against dashboard stations cache.
        if category == MY_WAVES_FOLDER_ID and tag:
            dashboard_stations = await self._get_dashboard_stations_cached()
            for sid, _, _ in dashboard_stations:
                sid_tag = sid.split(":", 1)[1] if ":" in sid else sid
                if sid_tag == tag:
                    return await self._browse_wave_station(sid, path=path)
            # Fallback: try tag as direct station_id (e.g. "genre:allrock" passed verbatim)
            if ":" in tag:
                return await self._browse_wave_station(tag, path=path)
            return []

        # waves/<category>/ — show station folders with artwork
        if category and not tag:
            cat_stations = categorized.get(category, [])
            folders = []
            for station_id, station_name, image_url in cat_stations:
                tag_part = station_id.split(":", 1)[1] if ":" in station_id else station_id
                station_image: MediaItemImage | None = None
                if image_url:
                    station_image = MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                folders.append(
                    BrowseFolder(
                        item_id=station_id,
                        provider=self.instance_id,
                        path=f"{base}{tag_part}",
                        name=station_name,
                        is_playable=True,
                        image=station_image,
                    )
                )
            return folders

        # waves/<category>/<tag>[/next] — stream tracks from rotor station
        if category and tag:
            station_id = f"{category}:{tag}"
            return await self._browse_wave_station(station_id, path=path)

        return []

    @use_cache(600)
    async def _get_dashboard_stations_cached(self) -> list[tuple[str, str, str | None]]:
        """Get personalized dashboard stations, cached for 10 minutes.

        :return: List of (station_id, name, image_url) tuples.
        """
        return await self.client.get_dashboard_stations()

    async def _browse_my_waves_stations(self, path: str) -> list[BrowseFolder]:
        """Browse personalized wave stations from rotor/stations/dashboard.

        Names are resolved from the non-personalized station list so that
        stations show their actual genre/mood name (e.g. "Рок") rather than
        the generic "Моя волна" label that the dashboard API returns.

        :param path: Full browse path (used to build sub-paths).
        :return: List of playable BrowseFolder items, one per station.
        """
        stations = await self._get_dashboard_stations_cached()

        # Build a name map from the non-personalized list for proper localized names.
        locale = (self.mass.metadata.locale or "en_US").lower()
        language = "ru" if locale.startswith("ru") else "en"
        all_stations = await self.client.get_wave_stations(language)
        station_name_map: dict[str, str] = {sid: name for sid, _, name, _ in all_stations}

        base = path.rstrip("/") + "/"
        folders: list[BrowseFolder] = []
        for station_id, fallback_name, image_url in stations:
            # Use full station_id (e.g. "genre:rock") in path to avoid collisions
            # when two stations share the same tag but differ by category.
            # The routing fallback (if ":" in tag) handles this correctly.
            name = station_name_map.get(station_id, fallback_name)
            station_image: MediaItemImage | None = None
            if image_url:
                station_image = MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            folders.append(
                BrowseFolder(
                    item_id=station_id,
                    provider=self.instance_id,
                    path=f"{base}{station_id}",
                    name=name,
                    is_playable=True,
                    image=station_image,
                )
            )
        return folders

    async def _browse_wave_station(
        self, station_id: str, path: str = ""
    ) -> list[Track | BrowseFolder]:
        """Browse a rotor wave station and return tracks.

        Fetches tracks from the rotor station, deduplicates within the current session,
        and sends radioStarted feedback on first call. Appends a "Load more" BrowseFolder
        at the end so MA can continue fetching the next batch automatically (radio mode).

        :param station_id: Rotor station ID (e.g. 'genre:rock', 'mood:chill').
        :param path: Current browse path, used to construct the "Load more" next path.
        :return: List of Track objects with composite item_id (track_id@station_id),
                 followed by a "Load more" BrowseFolder if more tracks are available.
        """
        state = self._get_wave_state(station_id)
        async with state.lock:
            max_tracks = int(
                self.config.get_value(CONF_MY_WAVE_MAX_TRACKS) or 150  # type: ignore[arg-type]
            )

            self.logger.debug(
                "Browse wave station: station_id=%s path=%s last_track_id=%s",
                station_id,
                path,
                state.last_track_id,
            )
            yandex_tracks, batch_id = await self.client.get_rotor_station_tracks(
                station_id, queue=state.last_track_id
            )
            if batch_id:
                state.batch_id = batch_id

            if not state.radio_started_sent and yandex_tracks:
                sent = await self.client.send_rotor_station_feedback(
                    station_id,
                    "radioStarted",
                    batch_id=batch_id,
                )
                if sent:
                    state.radio_started_sent = True

            tracks: list[Track] = []
            first_track_id: str | None = None
            for yt in yandex_tracks:
                if len(state.seen_track_ids) >= max_tracks:
                    break
                track = self._parse_my_wave_track(yt, state.seen_track_ids)
                if track is None:
                    continue
                # Override station_id in composite item_id to reflect this specific station
                old_item_id = track.item_id
                track_id = old_item_id.split(RADIO_TRACK_ID_SEP, 1)[0]
                track.item_id = f"{track_id}{RADIO_TRACK_ID_SEP}{station_id}"
                # Keep provider mappings in sync with the new item_id
                for pm in getattr(track, "provider_mappings", []):
                    if (
                        getattr(pm, "item_id", None) == old_item_id
                        and getattr(pm, "provider_instance", None) == self.instance_id
                    ):
                        pm.item_id = track.item_id
                if first_track_id is None:
                    first_track_id = track_id
                tracks.append(track)

            if first_track_id is not None:
                state.last_track_id = first_track_id

            self.logger.debug(
                "Wave station %s returned %d tracks: %s",
                station_id,
                len(tracks),
                [t.item_id.split(RADIO_TRACK_ID_SEP, 1)[0] for t in tracks[:5]],
            )
            result: list[Track | BrowseFolder] = list(tracks)

            # Append "Load more" sentinel so MA knows to call browse again for next batch.
            # This mirrors the My Wave mechanism and enables continuous radio playback.
            if tracks and len(state.seen_track_ids) < max_tracks and path:
                names = self._get_browse_names()
                next_name = "Ещё" if names == BROWSE_NAMES_RU else "Load more"
                # Append /next to the current path (same pattern as _browse_my_wave).
                # This makes each "Load more" path unique (e.g. /next/next/next...)
                # so MA never serves a cached result for subsequent presses.
                result.append(
                    BrowseFolder(
                        item_id="next",
                        provider=self.instance_id,
                        path=f"{path.rstrip('/')}/next",
                        name=next_name,
                        is_playable=False,
                    )
                )

            return result

    @staticmethod
    def _extract_wave_item_cover(item: dict[str, Any]) -> tuple[str | None, str | None]:
        """Extract cover URI and background color from a wave/mix item.

        :param item: Wave or mix item dict from the API.
        :return: (cover_uri, bg_color) tuple where bg_color is a hex string or None.
        """
        agent_uri = item.get("agent", {}).get("cover", {}).get("uri", "")
        cover_uri = agent_uri or item.get("compact_image_url")
        bg_color = item.get("colors", {}).get("average")
        return cover_uri, bg_color

    @use_cache(3600)
    async def _get_mixes_waves_cached(self) -> list[dict[str, Any]] | None:
        """Get AI Wave Set data from /landing-blocks/mixes-waves, cached for 1 hour.

        :return: List of mix category dicts from the API, or None on error.
        """
        return await self.client.get_mixes_waves()

    @use_cache(3600)
    async def _get_waves_landing_cached(self) -> list[dict[str, Any]] | None:
        """Get Featured Waves data from /landing-blocks/waves, cached for 1 hour.

        :return: List of wave category dicts from the API, or None on error.
        """
        return await self.client.get_waves_landing()

    async def _browse_waves_landing(
        self, path: str, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse Featured Waves (from /landing-blocks/waves).

        :param path: Full browse path.
        :param path_parts: Split path parts after ://.
        :return: List of folders or tracks.
        """
        waves_data = await self._get_waves_landing_cached()
        return await self._browse_wave_categories(
            path, path_parts, waves_data or [], WAVES_LANDING_FOLDER_ID
        )

    async def _browse_wave_categories(
        self,
        path: str,
        path_parts: list[str],
        categories_data: list[dict[str, Any]],
        id_prefix: str,
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse wave-like category folders and their station items.

        Shared logic for both 'my_waves_set' browse trees:
        - Level 1 (e.g. my_waves_set/): category folders
        - Level 2 (e.g. my_waves_set/ai-sets/): playable station folders with artwork
        - Level 3+ (e.g. my_waves_set/ai-sets/genre:rock[/next]): track listing

        :param path: Full browse path.
        :param path_parts: Split path parts after ://.
        :param categories_data: List of category dicts from the API.
        :param id_prefix: Prefix for BrowseFolder item_id (e.g. 'my_waves_set').
        :return: List of folders or tracks.
        """
        base = path.rstrip("/") + "/"

        if not categories_data:
            return []

        # Level 1 → category folders
        if len(path_parts) == 1:
            folders: list[BrowseFolder] = []
            for wave_category in categories_data:
                cat_id = wave_category.get("id", "")
                cat_title = wave_category.get("title", "")
                items = wave_category.get("items", [])
                if not items or not cat_id:
                    continue
                display_name = cat_title.capitalize() if cat_title else cat_id.capitalize()
                folders.append(
                    BrowseFolder(
                        item_id=f"{id_prefix}_{cat_id}",
                        provider=self.instance_id,
                        path=f"{base}{cat_id}",
                        name=display_name,
                        is_playable=False,
                    )
                )
            return folders

        category_id = path_parts[1] if len(path_parts) > 1 else None
        if not category_id:
            return []

        # Level 3+ → stream tracks from rotor station
        if len(path_parts) > 2:
            station_id = path_parts[2]
            return await self._browse_wave_station(station_id, path=path)

        # Level 2 → playable station folders with artwork
        for wave_category in categories_data:
            if wave_category.get("id") == category_id:
                items = wave_category.get("items", [])
                result: list[BrowseFolder] = []
                for item in items:
                    station_id = item.get("station_id", "")
                    title = item.get("title", "")
                    if not station_id or not title:
                        continue
                    cover_uri, bg_color = self._extract_wave_item_cover(item)
                    image: MediaItemImage | None = None
                    if cover_uri:
                        if cover_uri.startswith("http"):
                            img_url: str = cover_uri.replace("%%", IMAGE_SIZE_MEDIUM)
                        else:
                            raw = get_image_url(cover_uri)
                            img_url = "" if raw is None else raw
                        if img_url:
                            if bg_color:
                                # Append bg_color as URL fragment for cache-key uniqueness.
                                # MA will call resolve_image() to composite the transparent PNG.
                                if len(self._wave_bg_colors) > 200:
                                    self._wave_bg_colors.clear()
                                img_url = f"{img_url}#{bg_color.lstrip('#')}"
                                self._wave_bg_colors[img_url] = bg_color
                            image = MediaItemImage(
                                type=ImageType.THUMB,
                                path=img_url,
                                provider=self.instance_id,
                                remotely_accessible=bg_color is None,
                            )
                    result.append(
                        BrowseFolder(
                            item_id=station_id,
                            provider=self.instance_id,
                            path=f"{base}{station_id}",
                            name=title,
                            is_playable=True,
                            image=image,
                        )
                    )
                return result

        return []

    async def _browse_vibe_sets(
        self, path: str, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse AI Wave Sets (from /landing-blocks/mixes-waves).

        :param path: Full browse path.
        :param path_parts: Split path parts after ://.
        :return: List of folders or tracks.
        """
        mixes_data = await self._get_mixes_waves_cached()
        return await self._browse_wave_categories(
            path, path_parts, mixes_data or [], MY_WAVES_SET_FOLDER_ID
        )

    @use_cache(600)
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

    async def get_track(self, prov_track_id: str) -> Track:
        """Get track details by ID.

        Supports composite item_id (track_id@station_id) for My Wave tracks;
        only the track_id part is used for the API. Normalizes the ID before
        caching to avoid duplicate cache entries.

        :param prov_track_id: The provider track ID (or track_id@station_id).
        :return: Track object.
        :raises MediaNotFoundError: If track not found.
        """
        track_id, _ = _parse_radio_item_id(prov_track_id)
        return await self._get_track_cached(track_id)

    @use_cache(3600 * 24 * 30)
    async def _get_track_cached(self, track_id: str) -> Track:
        """Get track details by normalized ID (cached).

        :param track_id: Normalized track ID (without station suffix).
        :return: Track object.
        :raises MediaNotFoundError: If track not found.
        """
        yandex_track = await self.client.get_track(track_id)
        if not yandex_track:
            raise MediaNotFoundError(f"Track {track_id} not found")

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
                owner=get_canonical_provider_name(self),
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
                name=names[LIKED_TRACKS_PLAYLIST_ID],
                owner=get_canonical_provider_name(self),
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

        Fetches MY_WAVE_BATCH_SIZE Rotor API batches per page call to reduce
        the number of round-trips when the player controller paginates through pages.

        :param page: Page number (0 = first batch, 1+ = next batches via queue cursor).
        :return: List of Track objects for this page.
        """
        async with self._my_wave_lock:
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

            tracks: list[Track] = []
            next_cursor: str | None = None

            # Fetch MY_WAVE_BATCH_SIZE Rotor API batches per page to reduce API round-trips
            for _ in range(MY_WAVE_BATCH_SIZE):
                if len(self._my_wave_seen_track_ids) >= max_tracks_config:
                    break

                yandex_tracks, batch_id = await self.client.get_my_wave_tracks(queue=queue)
                if batch_id:
                    self._my_wave_batch_id = batch_id
                if not self._my_wave_radio_started_sent and yandex_tracks:
                    sent = await self.client.send_rotor_station_feedback(
                        ROTOR_STATION_MY_WAVE,
                        "radioStarted",
                        batch_id=batch_id,
                    )
                    if sent:
                        self._my_wave_radio_started_sent = True

                if not yandex_tracks:
                    break

                first_track_id_this_batch = None
                for yt in yandex_tracks:
                    if len(self._my_wave_seen_track_ids) >= max_tracks_config:
                        break

                    track = self._parse_my_wave_track(yt, self._my_wave_seen_track_ids)
                    if track is None:
                        continue

                    tracks.append(track)
                    track_id = track.item_id.split(RADIO_TRACK_ID_SEP, 1)[0]
                    if first_track_id_this_batch is None:
                        first_track_id_this_batch = track_id

                if first_track_id_this_batch is not None:
                    next_cursor = first_track_id_this_batch
                    queue = first_track_id_this_batch
                else:
                    # All tracks in this batch were duplicates or failed to parse
                    break

            # Store cursor for next page call (None clears pagination so next call returns [])
            self._my_wave_playlist_next_cursor = next_cursor
            return tracks

    async def _get_liked_tracks_playlist_tracks(self, page: int) -> list[Track]:
        """Get liked tracks for virtual playlist (sorted in reverse chronological order).

        :param page: Page number (0 = all tracks limited by config, >0 = empty for pagination).
        :return: List of Track objects.
        """
        # Liked tracks API returns all tracks at once, so only return tracks on page 0
        if page > 0:
            return []

        max_tracks_config = int(
            self.config.get_value(CONF_LIKED_TRACKS_MAX_TRACKS) or 500  # type: ignore[arg-type]
        )

        # Fetch liked tracks (already sorted in reverse chronological order by api_client)
        track_shorts = await self.client.get_liked_tracks()
        if not track_shorts:
            self.logger.debug("No liked tracks found")
            return []

        # Apply max tracks limit
        track_shorts = track_shorts[:max_tracks_config]

        # Fetch full track details in batches
        track_ids = [str(ts.track_id) for ts in track_shorts if ts.track_id]

        batch_size = TRACK_BATCH_SIZE
        full_tracks = []
        for i in range(0, len(track_ids), batch_size):
            batch_ids = track_ids[i : i + batch_size]
            batch_result = await self.client.get_tracks(batch_ids)
            full_tracks.extend(batch_result)

        # Create track ID to full track mapping by track ID directly
        track_map = {}
        for t in full_tracks:
            if hasattr(t, "id") and t.id:
                track_map[str(t.id)] = t

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

        self.logger.debug("Liked tracks: fetched %s, parsed %s", len(track_shorts), len(tracks))
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

        # Mood mix: select tag outside cache so rotation actually works
        mood_tag = await self._pick_random_tag_for_category("mood")
        if mood_tag:
            folder = await self._get_mood_mix_recommendations(mood_tag)
            if folder:
                folders.append(folder)

        # Activity mix: select tag outside cache so rotation actually works
        activity_tag = await self._pick_random_tag_for_category("activity")
        if activity_tag:
            folder = await self._get_activity_mix_recommendations(activity_tag)
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

                track = self._parse_my_wave_track(yt, seen_ids=seen_track_ids)
                if track is None:
                    continue

                items.append(track)
                track_id = track.item_id.split(RADIO_TRACK_ID_SEP, 1)[0]
                if first_track_id_this_batch is None:
                    first_track_id_this_batch = track_id

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

    async def _pick_random_tag_for_category(self, category: str) -> str | None:
        """Pick a random valid tag for a category (not cached — enables rotation).

        :param category: Category name ('mood', 'activity', etc.).
        :return: Random tag slug, or None if no valid tags.
        """
        valid_tags = await self._get_valid_tags_for_category(category)
        if not valid_tags:
            return None
        return random.choice(valid_tags)

    @use_cache(1800)
    async def _get_mood_mix_recommendations(self, mood_tag: str) -> RecommendationFolder | None:
        """Get Mood Mix recommendation folder for a specific tag.

        :param mood_tag: Pre-selected mood tag slug.
        :return: RecommendationFolder with mood playlists, or None if unavailable.
        """
        playlists = await self.client.get_tag_playlists(mood_tag)
        if not playlists:
            self.logger.debug("No playlists for mood tag %s, skipping recommendation", mood_tag)
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
    async def _get_activity_mix_recommendations(
        self, activity_tag: str
    ) -> RecommendationFolder | None:
        """Get Activity Mix recommendation folder for a specific tag.

        :param activity_tag: Pre-selected activity tag slug.
        :return: RecommendationFolder with activity playlists, or None if unavailable.
        """
        playlists = await self.client.get_tag_playlists(activity_tag)
        if not playlists:
            self.logger.debug(
                "No playlists for activity tag %s, skipping recommendation", activity_tag
            )
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

        # Validate the seasonal tag; fall back to autumn if not available
        if not await self._validate_tag(seasonal_tag):
            seasonal_tag = "autumn"

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

        Includes virtual playlists (My Wave and Liked Tracks if enabled), user-created playlists,
        and user-liked editorial playlists (returned by a separate API endpoint).
        """
        yield await self.get_playlist(MY_WAVE_PLAYLIST_ID)
        yield await self.get_playlist(LIKED_TRACKS_PLAYLIST_ID)
        seen_ids: set[str] = set()
        # User-created playlists
        playlists = await self.client.get_user_playlists()
        for playlist in playlists:
            try:
                parsed = parse_playlist(self, playlist)
                seen_ids.add(parsed.item_id)
                yield parsed
            except InvalidDataError as err:
                self.logger.debug("Error parsing library playlist: %s", err)
        # User-liked editorial playlists (not in users_playlists_list)
        liked_playlists = await self.client.get_liked_playlists()
        for playlist in liked_playlists:
            try:
                parsed = parse_playlist(self, playlist)
                if parsed.item_id not in seen_ids:
                    yield parsed
            except InvalidDataError as err:
                self.logger.debug("Error parsing liked playlist: %s", err)

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

    async def resolve_image(self, path: str) -> str | bytes:
        """Resolve wave cover image with background color fill for transparent PNGs.

        If the image URL has an associated background color (stored in _wave_bg_colors),
        downloads the PNG from Yandex CDN and composites it on a solid color background
        using Pillow, returning JPEG bytes. Falls back to the original URL on any error.

        :param path: Image URL (may include #rrggbb fragment used as cache key).
        :return: Composited JPEG bytes, or original path string as fallback.
        """
        bg_color = self._wave_bg_colors.get(path)
        if not bg_color:
            return path

        # Strip the #color fragment before fetching the actual image
        fetch_url = path.split("#", maxsplit=1)[0] if "#" in path else path
        try:
            async with self.mass.http_session.get(fetch_url) as resp:
                resp.raise_for_status()
                raw = await resp.read()
        except Exception as err:
            self.logger.debug("Failed to fetch wave cover %s: %s", fetch_url, err)
            return fetch_url

        def _composite() -> bytes:
            bg_clean = bg_color.lstrip("#")
            try:
                r = int(bg_clean[0:2], 16)
                g = int(bg_clean[2:4], 16)
                b = int(bg_clean[4:6], 16)
            except (ValueError, IndexError):
                return raw
            fg = PilImage.open(BytesIO(raw)).convert("RGBA")
            bg = PilImage.new("RGBA", fg.size, (r, g, b, 255))
            bg.paste(fg, mask=fg)
            out = BytesIO()
            bg.convert("RGB").save(out, "JPEG", quality=92)
            return out.getvalue()

        try:
            return await asyncio.to_thread(_composite)
        except Exception as err:
            self.logger.debug("Wave cover composite failed for %s: %s", fetch_url, err)
            return fetch_url

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

        Also auto-enables "Don't stop the music" for any queue playing a radio track
        so that MA refills the queue via get_similar_tracks when < 5 tracks remain.
        """
        # Radio feedback always enabled
        if media_type != MediaType.TRACK:
            return
        track_id, station_id = _parse_radio_item_id(prov_item_id)
        if not station_id:
            return
        # Auto-enable "Don't stop the music" on every on_played call for radio tracks.
        # Calling on every invocation (not just is_playing=True) ensures it fires even
        # for short tracks that finish before the 30-second periodic callback.
        self._ensure_dont_stop_the_music(prov_item_id)
        if is_playing:
            if station_id == ROTOR_STATION_MY_WAVE:
                batch_id = self._my_wave_batch_id
            else:
                state = self._wave_states.get(station_id)
                batch_id = state.batch_id if state else None
            await self.client.send_rotor_station_feedback(
                station_id,
                "trackStarted",
                track_id=track_id,
                batch_id=batch_id,
            )
            # Remove duplicate call that was under is_playing guard.
            # _ensure_dont_stop_the_music is now called unconditionally above.

    def _ensure_dont_stop_the_music(self, prov_item_id: str) -> None:
        """Enable 'Don't stop the music' on queues playing this specific radio item.

        Iterates all queues and enables the setting on queues whose current track
        mapping matches this exact composite item_id (track_id@station_id) for this
        provider instance.

        Also sets queue.radio_source directly to the current track because
        enqueued_media_items is empty for BrowseFolder-initiated playback, which
        normally prevents MA's auto-fill from triggering. Setting radio_source
        directly bypasses that gap so _fill_radio_tracks runs when < 5 tracks remain.
        """
        for queue in self.mass.player_queues:
            current = queue.current_item
            if current is None or current.media_item is None:
                continue
            item = current.media_item
            # Match by provider instance and exact composite item_id
            for mapping in getattr(item, "provider_mappings", []):
                if (
                    mapping.provider_instance == self.instance_id
                    and mapping.item_id == prov_item_id
                ):
                    # Set radio_source directly so MA's fill mechanism works even when
                    # the queue was started from a BrowseFolder (enqueued_media_items empty).
                    if not queue.radio_source and isinstance(item, Track):
                        queue.radio_source = [item]
                    if not queue.dont_stop_the_music_enabled:
                        try:
                            self.mass.player_queues.set_dont_stop_the_music(
                                queue.queue_id, dont_stop_the_music_enabled=True
                            )
                            self.logger.info(
                                "Auto-enabled 'Don't stop the music' for queue %s (radio station)",
                                queue.display_name,
                            )
                        except Exception as err:
                            self.logger.debug(
                                "Could not enable 'Don't stop the music' for queue %s: %s",
                                queue.display_name,
                                err,
                            )
                    break

    def _ensure_dont_stop_the_music_for_queue(self, queue_id: str | None) -> None:
        """Enable 'Don't stop the music' for a specific queue by ID.

        Faster variant of _ensure_dont_stop_the_music used from on_streamed where
        queue_id is available directly, avoiding iteration over all queues.
        """
        if not queue_id:
            return
        queue = self.mass.player_queues.get(queue_id)
        if queue is None:
            return
        current = queue.current_item
        if current is None or current.media_item is None:
            return
        item = current.media_item
        for mapping in getattr(item, "provider_mappings", []):
            if (
                mapping.provider_instance == self.instance_id
                and RADIO_TRACK_ID_SEP in mapping.item_id
            ):
                if not queue.radio_source and isinstance(item, Track):
                    queue.radio_source = [item]
                if not queue.dont_stop_the_music_enabled:
                    try:
                        self.mass.player_queues.set_dont_stop_the_music(
                            queue_id, dont_stop_the_music_enabled=True
                        )
                        self.logger.info(
                            "Auto-enabled 'Don't stop the music' for queue %s (radio)",
                            queue.display_name,
                        )
                    except Exception as err:
                        self.logger.debug(
                            "Could not enable 'Don't stop the music' for queue %s: %s",
                            queue.display_name,
                            err,
                        )
                break

    async def on_streamed(self, streamdetails: StreamDetails) -> None:
        """Report stream completion for My Wave rotor feedback.

        Sends trackFinished or skip with actual seconds_streamed so Yandex
        can improve recommendations.
        """
        # Radio feedback always enabled
        track_id, station_id = _parse_radio_item_id(streamdetails.item_id)
        if not station_id:
            return
        # Also ensure Don't stop the music is active — on_streamed fires even for
        # very short tracks and we have queue_id here directly.
        self._ensure_dont_stop_the_music_for_queue(streamdetails.queue_id)
        seconds = int(streamdetails.seconds_streamed or 0)
        duration = streamdetails.duration or 0
        feedback_type = "trackFinished" if duration and seconds >= max(0, duration - 10) else "skip"
        if station_id == ROTOR_STATION_MY_WAVE:
            batch_id = self._my_wave_batch_id
        else:
            state = self._wave_states.get(station_id)
            batch_id = state.batch_id if state else None
        await self.client.send_rotor_station_feedback(
            station_id,
            feedback_type,
            track_id=track_id,
            total_played_seconds=seconds,
            batch_id=batch_id,
        )
