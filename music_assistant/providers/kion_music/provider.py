"""KION Music provider implementation."""

from __future__ import annotations

import asyncio
import logging
import zlib
from collections.abc import AsyncGenerator, Coroutine, Sequence
from io import BytesIO
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, ImageType, MediaType, ProviderFeature
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

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.datetime import utc
from music_assistant.models.music_provider import MusicProvider

from .api_client import KionMusicClient
from .constants import (
    BROWSE_INITIAL_TRACKS,
    COLLECTION_FOLDER_ID,
    CONF_ACTION_CLEAR_AUTH,
    CONF_BASE_URL,
    CONF_CODECS,
    CONF_LIKED_TRACKS_MAX_TRACKS,
    CONF_MY_WAVE_MAX_TRACKS,
    CONF_QUALITY,
    CONF_TOKEN,
    CONF_TRANSPORT,
    DEFAULT_BASE_URL,
    DISCOVERY_INITIAL_TRACKS,
    FOR_YOU_FOLDER_ID,
    IMAGE_SIZE_MEDIUM,
    LIKED_TRACKS_PLAYLIST_ID,
    LISTENING_HISTORY_FOLDER_ID,
    MY_WAVE_BATCH_SIZE,
    MY_WAVE_PLAYLIST_ID,
    MY_WAVES_FOLDER_ID,
    MY_WAVES_SET_FOLDER_ID,
    PINNED_ITEMS_FOLDER_ID,
    PLAYLIST_ID_SPLITTER,
    QUALITY_BALANCED,
    QUALITY_EFFICIENT,
    QUALITY_HIGH,
    QUALITY_LOSSLESS,
    RADIO_FOLDER_ID,
    RADIO_TRACK_ID_SEP,
    ROTOR_STATION_MY_MIX,
    TAG_CATEGORY_ACTIVITY,
    TAG_CATEGORY_ERA,
    TAG_CATEGORY_GENRES,
    TAG_CATEGORY_MOOD,
    TAG_CATEGORY_ORDER,
    TAG_MIXES,
    TAG_SEASONAL_MAP,
    TAG_SLUG_CATEGORY,
    TRACK_BATCH_SIZE,
    TRANSPORT_ENCRAW,
    TRANSPORT_RAW,
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
from .streaming import KionMusicStreamingManager

if TYPE_CHECKING:
    from music_assistant_models.streamdetails import StreamDetails


def _parse_radio_item_id(item_id: str) -> tuple[str, str | None]:
    """
    Extract track_id and optional station_id from provider item_id.

    My Mix tracks use item_id format 'track_id@station_id'. Other tracks use
    plain track_id.

    :param item_id: Provider item_id (may contain RADIO_TRACK_ID_SEP).
    :return: (track_id, station_id or None).
    """
    if RADIO_TRACK_ID_SEP in item_id:
        parts = item_id.split(RADIO_TRACK_ID_SEP, 1)
        return (parts[0], parts[1] if len(parts) > 1 else None)
    return (item_id, None)


# Collection sub-folder browse ids -> (ProviderFeature, library sub_id, label key, English name).
# The library sub_id ("tracks") and label key ("my_favorites") differ on purpose so the Collection
# labels stay distinct from the core "media.folder.*" library labels.
_COLLECTION_SUBFOLDERS: tuple[tuple[ProviderFeature, str, str, str], ...] = (
    (ProviderFeature.LIBRARY_TRACKS, "tracks", "my_favorites", "My Favorites"),
    (ProviderFeature.LIBRARY_ARTISTS, "artists", "my_artists", "My Artists"),
    (ProviderFeature.LIBRARY_ALBUMS, "albums", "my_albums", "My Albums"),
    (ProviderFeature.LIBRARY_PLAYLISTS, "playlists", "my_playlists", "My Playlists"),
)


def _media_label_key(slug: str) -> str:
    """Normalize a tag/category slug into its strings.json authoring key (spaces → underscores)."""
    return slug.replace(" ", "_")


class _WaveState:
    """Per-station mutable state for rotor wave playback."""

    def __init__(self) -> None:
        self.batch_id: str | None = None
        self.last_track_id: str | None = None
        self.seen_track_ids: set[str] = set()
        self.radio_started_sent: bool = False
        self.lock: asyncio.Lock = asyncio.Lock()


class KionMusicProvider(MusicProvider):
    """Implementation of a KION Music MusicProvider."""

    _client: KionMusicClient | None = None
    _streaming: KionMusicStreamingManager | None = None
    _my_wave_batch_id: str | None = None
    _my_wave_last_track_id: str | None = None  # last track id for "Load more" (API queue param)
    _my_wave_playlist_next_cursor: str | None = None  # first_track_id for next playlist page
    _my_wave_radio_started_sent: bool = False
    _my_wave_seen_track_ids: set[str]  # Track IDs seen in current My Mix session
    _my_wave_lock: asyncio.Lock  # Protects My Mix mutable state
    _wave_states: dict[str, _WaveState]  # Per-station state for tagged wave stations
    _wave_bg_colors: dict[str, str]  # image_url -> hex bg color for transparent covers

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

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """
        Get the available recommendation rows, without items.

        Returns My Mix, Made for you, Chart, New Releases, New Playlists,
        Top Picks, Mood Mix, Activity Mix and Seasonal Mix rows.
        """
        # The seasonal row title carries the current season, derived locally from the month.
        seasonal_tag = TAG_SEASONAL_MAP.get(utc().month, "autumn")
        seasonal_name = (
            self._media_source_name("folder", _media_label_key(seasonal_tag))
            or seasonal_tag.title()
        )
        return [
            RecommendationFolder(
                item_id=MY_WAVE_PLAYLIST_ID,
                provider=self.instance_id,
                name="My Mix",
                translation_key=MY_WAVE_PLAYLIST_ID,
                icon="mdi-waveform",
            ),
            RecommendationFolder(
                item_id="feed",
                provider=self.instance_id,
                name="Made for you",
                translation_key="made_for_you",
                icon="mdi-account-music",
            ),
            RecommendationFolder(
                item_id="chart",
                provider=self.instance_id,
                name="Chart",
                translation_key="chart",
                icon="mdi-chart-line",
            ),
            RecommendationFolder(
                item_id="new_releases",
                provider=self.instance_id,
                name="New Releases",
                translation_key="new_releases",
                icon="mdi-new-box",
            ),
            RecommendationFolder(
                item_id="new_playlists",
                provider=self.instance_id,
                name="New Playlists",
                translation_key="new_playlists",
                icon="mdi-playlist-star",
            ),
            RecommendationFolder(
                item_id="top_picks",
                provider=self.instance_id,
                name="Top Picks",
                translation_key="top_picks",
                icon="mdi-star",
            ),
            # Mood/Activity rows have a static title; the hourly rotating tag - derived
            # deterministically, so the items call independently computes the same one -
            # shows as the row subtitle (cache-only tag-list read, no backend I/O).
            RecommendationFolder(
                item_id="mood_mix",
                provider=self.instance_id,
                name="Mood Mix",
                translation_key="mood_mix",
                subtitle=await self._rotating_row_tag_subtitle("mood"),
                icon="mdi-emoticon-outline",
            ),
            RecommendationFolder(
                item_id="activity_mix",
                provider=self.instance_id,
                name="Activity Mix",
                translation_key="activity_mix",
                subtitle=await self._rotating_row_tag_subtitle("activity"),
                icon="mdi-run",
            ),
            RecommendationFolder(
                item_id="seasonal_mix",
                provider=self.instance_id,
                name=f"Seasonal: {seasonal_name}",
                translation_key="seasonal_mix",
                translation_params=[seasonal_name],
                icon="mdi-weather-sunny",
            ),
        ]

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        folder: RecommendationFolder | None = None
        if item_id == MY_WAVE_PLAYLIST_ID:
            folder = await self._get_my_wave_recommendations()
        elif item_id == "feed":
            folder = await self._get_feed_recommendations()
        elif item_id == "chart":
            folder = await self._get_chart_recommendations()
        elif item_id == "new_releases":
            folder = await self._get_new_releases_recommendations()
        elif item_id == "new_playlists":
            folder = await self._get_new_playlists_recommendations()
        elif item_id == "top_picks":
            folder = await self._get_top_picks_recommendations()
        elif item_id == "mood_mix":
            # the deterministic hourly tag keeps the served items matching the row subtitle
            if mood_tags := await self._get_valid_tags_for_category("mood"):
                folder = await self._get_mood_mix_recommendations(
                    self._rotating_row_tag("mood", mood_tags)
                )
        elif item_id == "activity_mix":
            if activity_tags := await self._get_valid_tags_for_category("activity"):
                folder = await self._get_activity_mix_recommendations(
                    self._rotating_row_tag("activity", activity_tags)
                )
        elif item_id == "seasonal_mix":
            folder = await self._get_seasonal_mix_recommendations()
        if folder is None:
            return UniqueList()
        return folder.items

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        is_authenticated = bool(self.get_config_value(CONF_TOKEN))
        return (
            CONF_ENTRY_UNOFFICIAL_PROVIDER,
            # Authentication
            ConfigEntry(
                key=CONF_TOKEN,
                type=ConfigEntryType.SECURE_STRING,
                required=True,
                hidden=is_authenticated,
            ),
            ConfigEntry(
                key=CONF_ACTION_CLEAR_AUTH,
                type=ConfigEntryType.ACTION,
                action=CONF_ACTION_CLEAR_AUTH,
                hidden=not is_authenticated,
            ),
            # Quality
            ConfigEntry(
                key=CONF_QUALITY,
                type=ConfigEntryType.STRING,
                options=[
                    ConfigValueOption(QUALITY_EFFICIENT),
                    ConfigValueOption(QUALITY_BALANCED),
                    ConfigValueOption(QUALITY_HIGH),
                    ConfigValueOption(QUALITY_LOSSLESS),
                ],
                default_value=QUALITY_BALANCED,
            ),
            # My Mix maximum tracks (advanced)
            ConfigEntry(
                key=CONF_MY_WAVE_MAX_TRACKS,
                type=ConfigEntryType.INTEGER,
                range=(10, 1000),
                default_value=150,
                required=False,
                advanced=True,
            ),
            # Liked Tracks maximum tracks (advanced)
            ConfigEntry(
                key=CONF_LIKED_TRACKS_MAX_TRACKS,
                type=ConfigEntryType.INTEGER,
                range=(50, 2000),
                default_value=500,
                required=False,
                advanced=True,
            ),
            # Transport mode (advanced)
            ConfigEntry(
                key=CONF_TRANSPORT,
                type=ConfigEntryType.STRING,
                options=[
                    ConfigValueOption(TRANSPORT_RAW),
                    ConfigValueOption(TRANSPORT_ENCRAW),
                ],
                default_value=TRANSPORT_RAW,
                required=False,
                advanced=True,
            ),
            # Custom codecs override (advanced)
            ConfigEntry(
                key=CONF_CODECS,
                type=ConfigEntryType.STRING,
                default_value="",
                required=False,
                advanced=True,
            ),
            # API Base URL (advanced)
            ConfigEntry(
                key=CONF_BASE_URL,
                type=ConfigEntryType.STRING,
                translation_params=[DEFAULT_BASE_URL],
                default_value=DEFAULT_BASE_URL,
                required=False,
                advanced=True,
            ),
        )

    async def handle_config_action(self, action: str) -> tuple[ConfigEntry, ...]:
        """Handle a one-shot config action button press and re-render the entries."""
        if action == CONF_ACTION_CLEAR_AUTH:
            self._update_config_value(CONF_TOKEN, None, immediate=True)
            return await self.get_config_entries()
        return await super().handle_config_action(action)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        token = self.config.get_value(CONF_TOKEN)
        if not token:
            raise LoginFailed("No KION Music token provided")

        base_url = self.config.get_value(CONF_BASE_URL, DEFAULT_BASE_URL)
        self._client = KionMusicClient(str(token), base_url=str(base_url))
        await self._client.connect()
        # Suppress kion_music library DEBUG dumps (full API request/response JSON)
        logging.getLogger("yandex_music").setLevel(self.logger.level + 10)
        self._streaming = KionMusicStreamingManager(self)
        # Initialize My Mix duplicate tracking
        self._my_wave_seen_track_ids = set()
        self._my_wave_lock = asyncio.Lock()
        # Initialize per-station wave state dict
        self._wave_states = {}
        self._wave_bg_colors = {}
        self.logger.info("Successfully connected to KION Music")

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        :param is_removed: Whether the provider is being removed.
        """
        if self._client:
            await self._client.disconnect()
        self._client = None
        self._streaming = None
        await super().unload(is_removed)

    def get_item_mapping(self, media_type: MediaType | str, key: str, name: str) -> ItemMapping:
        """
        Create a generic item mapping.

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
        """
        Browse provider items.

        Root level shows My Mix (personalised radio), For You (picks & mixes),
        Collection (liked tracks/albums/artists/playlists), Radio (rotor stations
        by genre/mood/activity/era/local) and AI Mix Sets. Folder labels carry a
        translation_key so the server localizes them for the connection locale.
        My Mix tracks use item_id format track_id@station_id for rotor feedback.

        :param path: The path to browse (e.g. provider_id:// or provider_id://waves).
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

        # Handle my_waves_set/ path (AI Mix Sets from /landing-blocks/mixes-waves)
        if subpath == MY_WAVES_SET_FOLDER_ID:
            return await self._browse_vibe_sets(path, path_parts)

        # Handle waves_landing/ path (Featured Mixes from /landing-blocks/waves)
        if subpath == WAVES_LANDING_FOLDER_ID:
            return await self._browse_waves_landing(path, path_parts)

        # Pinned items folder
        if subpath == PINNED_ITEMS_FOLDER_ID:
            return await self._browse_pins()

        # Listening history folder
        if subpath == LISTENING_HISTORY_FOLDER_ID:
            return await self._browse_history()

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
            PINNED_ITEMS_FOLDER_ID,
            LISTENING_HISTORY_FOLDER_ID,
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

        # Each folder carries an English name plus a translation_key; the server localizes the
        # name for the connection locale at serialization, falling back to the English name.
        folders: list[BrowseFolder] = []
        base = path if path.endswith("//") else path.rstrip("/") + "/"
        # My Mix folder (always enabled — Яндекс «Мой микс»)
        folders.append(
            BrowseFolder(
                item_id=MY_WAVE_PLAYLIST_ID,
                provider=self.instance_id,
                path=f"{base}{MY_WAVE_PLAYLIST_ID}",
                name="My Mix",
                translation_key=MY_WAVE_PLAYLIST_ID,
                is_playable=True,
            )
        )
        # For You folder — Picks + Mixes (Яндекс «Для вас»)
        folders.append(
            BrowseFolder(
                item_id=FOR_YOU_FOLDER_ID,
                provider=self.instance_id,
                path=f"{base}{FOR_YOU_FOLDER_ID}",
                name="For You",
                translation_key=FOR_YOU_FOLDER_ID,
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
                    name="Collection",
                    translation_key=COLLECTION_FOLDER_ID,
                    is_playable=False,
                )
            )
        # Radio folder — rotor stations (reuses the shared "Radio" label)
        folders.append(
            BrowseFolder(
                item_id=RADIO_FOLDER_ID,
                provider=self.instance_id,
                path=f"{base}{RADIO_FOLDER_ID}",
                name="Radio",
                translation_key="radios",
                is_playable=False,
            )
        )
        # AI Mix Sets — parametric stations from /landing-blocks/mixes-waves
        folders.append(
            BrowseFolder(
                item_id=MY_WAVES_SET_FOLDER_ID,
                provider=self.instance_id,
                path=f"{base}{MY_WAVES_SET_FOLDER_ID}",
                name="AI Mix Sets",
                translation_key=MY_WAVES_SET_FOLDER_ID,
                is_playable=False,
            )
        )
        # Pinned items — user-pinned artists/albums/playlists/waves
        folders.append(
            BrowseFolder(
                item_id=PINNED_ITEMS_FOLDER_ID,
                provider=self.instance_id,
                path=f"{base}{PINNED_ITEMS_FOLDER_ID}",
                name="Pinned",
                translation_key=PINNED_ITEMS_FOLDER_ID,
                is_playable=False,
            )
        )
        # Listening history — recently played tracks/albums
        folders.append(
            BrowseFolder(
                item_id=LISTENING_HISTORY_FOLDER_ID,
                provider=self.instance_id,
                path=f"{base}{LISTENING_HISTORY_FOLDER_ID}",
                name="Listening History",
                translation_key=LISTENING_HISTORY_FOLDER_ID,
                is_playable=False,
            )
        )
        if len(folders) == 1:
            return await self.browse(folders[0].path)
        return folders

    def _media_source_name(self, group: str, key: str) -> str | None:
        """
        Return the authored English ``name`` for *key* in *group*, or None when not authored.

        :param group: Media translation group (``folder``, ``recommendations`` or ``playlist``).
        :param key: Authoring key within the group.
        """
        return self.mass.translations.get_translation(
            f"provider.{self.domain}.media.{group}.{key}.name"
        )

    def _media_label(self, group: str, key: str, fallback: str) -> tuple[str, str | None]:
        """
        Map a browse label to the in-code ``name`` and ``translation_key`` for its media item.

        Authored keys return ``(English source name, key)`` — the English name from the
        provider's ``strings.json`` plus a ``translation_key`` so the server localizes it for
        the connection locale at serialization. An unauthored key — e.g. a tag discovered from
        KION's landing API — returns ``(fallback, None)`` so its already-localized name is kept.

        :param group: Media translation group (``folder``, ``recommendations`` or ``playlist``).
        :param key: Authoring key within the group; also the item's ``translation_key``.
        :param fallback: English name to use when no string is authored for *key*.
        """
        source = self._media_source_name(group, key)
        if source is None:
            return fallback, None
        return source, key

    async def _browse_my_wave(
        self, path: str, sub_subpath: str | None
    ) -> list[Track | BrowseFolder]:
        """
        Browse My Mix tracks (must be called under _my_wave_lock).

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

            raw_tracks, batch_id = await self.client.get_my_wave_tracks(queue=queue)
            if batch_id:
                self._my_wave_batch_id = batch_id
                last_batch_id = batch_id
            if not self._my_wave_radio_started_sent and raw_tracks:
                sent = await self.client.send_rotor_station_feedback(
                    ROTOR_STATION_MY_MIX,
                    "radioStarted",
                    batch_id=batch_id,
                )
                if sent:
                    self._my_wave_radio_started_sent = True
            first_track_id_this_batch = None
            for yt in raw_tracks:
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
                or not raw_tracks
                or total_track_count >= effective_limit
            ):
                break
            queue = first_track_id_this_batch

        # Only show "Load more" if we haven't reached the limit and there's more data
        if last_batch_id and total_track_count < max_tracks_config:
            all_tracks.append(
                BrowseFolder(
                    item_id="next",
                    provider=self.instance_id,
                    path=f"{path.rstrip('/')}/next",
                    name="Load more",
                    translation_key="load_more",
                    is_playable=False,
                )
            )
        return all_tracks

    def _parse_my_wave_track(self, yt: Any, seen_ids: set[str]) -> Track | None:
        """
        Parse a Kion track into a My Mix Track with composite item_id.

        Extracts the track_id, checks for duplicates in the seen_ids set,
        sets composite item_id (track_id@station_id), and updates provider_mappings.
        Callers using shared state must hold _my_wave_lock.

        :param yt: Kion track object from rotor station response.
        :param seen_ids: Set of already-seen track IDs to check and update.
        :return: Parsed Track with composite item_id, or None if duplicate/invalid.
        """
        try:
            t = parse_track(self, yt)
        except InvalidDataError as err:
            self.logger.debug("Error parsing My Mix track: %s", err)
            return None

        track_id = str(yt.id) if hasattr(yt, "id") and yt.id else getattr(yt, "track_id", None)
        if not track_id:
            return t

        if track_id in seen_ids:
            self.logger.debug("Skipping duplicate My Mix track: %s", track_id)
            return None

        seen_ids.add(track_id)
        t.item_id = f"{track_id}{RADIO_TRACK_ID_SEP}{ROTOR_STATION_MY_MIX}"
        for pm in t.provider_mappings:
            if pm.provider_instance == self.instance_id:
                pm.item_id = t.item_id
                break
        return t

    @use_cache(3600)
    async def _validate_tag(self, tag_slug: str) -> bool:
        """
        Check if a tag has playlists by calling client.get_tag_playlists().

        :param tag_slug: Tag identifier (e.g. 'chill', '80s').
        :return: True if the tag has at least one playlist.
        """
        try:
            playlists = await self.client.get_tag_playlists(tag_slug)
            return len(playlists) > 0
        except Exception as err:
            self.logger.debug("Tag validation failed for %s: %s", tag_slug, err)
            return False

    # allow_expired_cache keeps the items call serving the same (possibly stale) tag
    # list the rows subtitle was derived from, while a background refresh runs
    @use_cache(3600, allow_expired_cache=True)
    async def _get_valid_tags_for_category(self, category: str) -> list[str]:
        """
        Get validated tags for a category (only those with playlists).

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
        """
        Get all available tags by combining hardcoded tags with landing discovery.

        Starts with all hardcoded tags from category lists, adds landing-discovered
        tags, validates each via client.tags(), and returns only those with playlists.
        Results are cached for 1 hour. The locale parameter is included in the cache
        key so that a locale change invalidates the cached landing titles.

        :param locale: Current metadata locale (used as part of cache key).
        :return: List of (slug, fallback display name) tuples for tags that have playlists.
            Hardcoded tags use a derived fallback; landing-discovered tags carry their
            (already localized) API title. Folder sites attach the translation_key via
            ``_media_label`` so authored tags localize and discovered ones keep their title.
        """
        # Collect all hardcoded tags (non-seasonal)
        all_tags: dict[str, str] = {}
        for slug, cat in TAG_SLUG_CATEGORY.items():
            if cat != "seasonal":
                all_tags[slug] = slug.title()

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
            (slug, name) for (slug, name), valid in zip(tag_items, results, strict=True) if valid
        ]

    async def _get_discovered_tag_slugs(self) -> set[str]:
        """
        Get set of all valid tag slugs (cached).

        :return: Set of tag slug strings that have playlists.
        """
        discovered = await self._get_discovered_tags(self.mass.metadata.locale or "en_US")
        return {slug for slug, _name in discovered}

    async def _browse_for_you(
        self, path: str, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse «For You» folder — shows Picks and Mixes sub-folders.

        :param path: Full browse path.
        :param path_parts: Split path parts after ://.
        :return: List of sub-folders (Picks, Mixes).
        """
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
                    name="Picks",
                    translation_key="picks",
                    is_playable=False,
                ),
                BrowseFolder(
                    item_id="mixes",
                    provider=self.instance_id,
                    path=f"{root_base}mixes",
                    name="Mixes",
                    translation_key="mixes",
                    is_playable=False,
                ),
            ]
        # Deeper path: delegate to picks or mixes handler via canonical paths
        return await super().browse(path)

    async def _browse_collection(
        self, path: str
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse «Collection» folder — shows library sub-folders (tracks/artists/albums/playlists).

        :param path: Full browse path.
        :return: List of library sub-folders.
        """
        base_parts = path.split("//", 1)
        root_base = (base_parts[0] + "//") if len(base_parts) > 1 else path.rstrip("/") + "/"

        folders: list[BrowseFolder] = []
        for feature, sub_id, label_key, label_name in _COLLECTION_SUBFOLDERS:
            if feature not in self.supported_features:
                continue
            folders.append(
                BrowseFolder(
                    item_id=sub_id,
                    provider=self.instance_id,
                    path=f"{root_base}{sub_id}",
                    name=label_name,
                    translation_key=label_key,
                    is_playable=True,
                )
            )
        return folders

    async def _browse_pins(self) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse user's pinned items (artists/albums/playlists from Kion Pins).

        Resolves each pin to its full media item via existing single-item lookups.
        Wave pins are skipped — MA has no native concept for them.

        Pins are resolved concurrently via ``asyncio.gather`` so latency is
        dominated by the slowest lookup rather than their sum. Individual
        failures (``MediaNotFoundError`` / ``InvalidDataError``) are skipped
        without aborting the batch.

        :return: List of resolved media items.
        """
        pins_list = await self.client.get_pins()
        pins = getattr(pins_list, "pins", None) if pins_list else None
        if not pins:
            return []

        tasks: list[Coroutine[Any, Any, MediaItemType]] = []
        pin_descs: list[str] = []
        for pin in pins:
            pin_type = getattr(pin, "type", None)
            data = getattr(pin, "data", None)
            if data is None:
                continue
            if pin_type == "artist_item" and getattr(data, "id", None) is not None:
                tasks.append(self.get_artist(str(data.id)))
                pin_descs.append(f"artist:{data.id}")
            elif pin_type == "album_item" and getattr(data, "id", None) is not None:
                tasks.append(self.get_album(str(data.id)))
                pin_descs.append(f"album:{data.id}")
            elif pin_type == "playlist_item":
                uid = getattr(data, "uid", None)
                kind = getattr(data, "kind", None)
                if uid is not None and kind is not None:
                    tasks.append(self.get_playlist(f"{uid}:{kind}"))
                    pin_descs.append(f"playlist:{uid}:{kind}")

        if not tasks:
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)
        items: list[MediaItemType] = []
        for desc, result in zip(pin_descs, results, strict=True):
            if isinstance(result, (MediaNotFoundError, InvalidDataError)):
                self.logger.debug("Skipping pin %s: %s", desc, result)
            elif isinstance(result, BaseException):
                raise result
            else:
                items.append(result)
        return items

    async def _browse_history(self) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse user's recent listening history (flattened across days).

        Filters to ``type == "track"`` entries only — album/playlist context
        items in the history feed are dropped. Tracks are de-duplicated by
        id and returned in most-recent-first order.

        :return: List of recently played Track items.
        """
        history = await self.client.get_music_history()
        tabs = getattr(history, "history_tabs", None) if history else None
        if not tabs:
            return []

        seen_track_ids: set[str] = set()
        tracks: list[Track] = []
        for tab in tabs:
            groups = getattr(tab, "items", None) or []
            for group in groups:
                history_items = getattr(group, "tracks", None) or []
                for hist_item in history_items:
                    if getattr(hist_item, "type", None) != "track":
                        continue
                    full = getattr(getattr(hist_item, "data", None), "full_model", None)
                    if full is None or getattr(full, "id", None) is None:
                        continue
                    track_key = str(full.id)
                    if track_key in seen_track_ids:
                        continue
                    seen_track_ids.add(track_key)
                    try:
                        tracks.append(parse_track(self, full))
                    except InvalidDataError as err:
                        self.logger.debug("Skipping history track: %s", err)
        return tracks

    async def _browse_picks(
        self, path: str, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse picks folder using hardcoded tags validated against the API.

        Tags are sourced from hardcoded category lists and landing API discovery,
        then validated via client.tags() to ensure they have playlists.
        Only categories with at least one valid tag are shown.

        :param path: Full browse path.
        :param path_parts: Split path parts after ://.
        :return: List of folders or playlists.
        """
        base = path.rstrip("/") + "/"

        # Get validated tags
        discovered = await self._get_discovered_tags(self.mass.metadata.locale or "en_US")

        # Categorize valid tags, carrying each tag's (slug, fallback display name)
        categorized: dict[str, list[tuple[str, str]]] = {}
        for slug, fallback_name in discovered:
            cat = TAG_SLUG_CATEGORY.get(slug, "mood")
            # Skip seasonal tags — they belong in mixes, not picks
            if cat == "seasonal":
                continue
            categorized.setdefault(cat, []).append((slug, fallback_name))

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
                    name, translation_key = self._media_label("folder", cat, cat.title())
                    folders.append(
                        BrowseFolder(
                            item_id=cat,
                            provider=self.instance_id,
                            path=f"{base}{cat}",
                            name=name,
                            translation_key=translation_key,
                            is_playable=False,
                        )
                    )
            # Show any extra categories not in the standard order
            for cat in categorized:
                if cat not in category_display_order:
                    name, translation_key = self._media_label("folder", cat, cat.title())
                    folders.append(
                        BrowseFolder(
                            item_id=cat,
                            provider=self.instance_id,
                            path=f"{base}{cat}",
                            name=name,
                            translation_key=translation_key,
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
            for slug, fallback_name in category_tags:
                name, translation_key = self._media_label(
                    "folder", _media_label_key(slug), fallback_name
                )
                folders.append(
                    BrowseFolder(
                        item_id=slug,
                        provider=self.instance_id,
                        path=f"{base}{slug}",
                        name=name,
                        translation_key=translation_key,
                        is_playable=False,
                    )
                )
            self.logger.debug("Returning %d tag folders for category %s", len(folders), category)
            return folders

        # picks/category/tag - show playlists for the tag
        if tag:
            discovered_slugs = {slug for slug, _name in discovered}
            if tag in discovered_slugs:
                self.logger.debug("Fetching playlists for tag: %s", tag)
                return await self._get_tag_playlists_as_browse(tag)

        self.logger.debug("No match found, returning empty list")
        return []

    async def _browse_mixes(
        self, path: str, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse mixes folder (seasonal collections) using hardcoded tags.

        Uses TAG_MIXES directly and validates each tag via client.tags()
        to check if it has playlists. Does not depend on landing API discovery.

        :param path: Full browse path.
        :param path_parts: Split path parts after ://.
        :return: List of folders or playlists.
        """
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
                name, translation_key = self._media_label("folder", t, t.title())
                folders.append(
                    BrowseFolder(
                        item_id=t,
                        provider=self.instance_id,
                        path=f"{base}{t}",
                        name=name,
                        translation_key=translation_key,
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
        """
        Get or create per-station wave state.

        :param station_id: Rotor station ID (e.g. 'genre:rock', 'mood:chill').
        :return: _WaveState instance for this station.
        """
        return self._wave_states.setdefault(station_id, _WaveState())

    async def _browse_waves(
        self, path: str, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse waves folder (rotor stations by genre/mood/activity/epoch/local).

        Fetches available stations from the Kion rotor API and groups them by category.

        :param path: Full browse path.
        :param path_parts: Split path parts after ://.
        :return: List of folders or tracks.
        """
        base = path.rstrip("/") + "/"

        # Station names come back from the rotor API already localized; the metadata locale
        # selects the API content language (not the folder labels, which use translation_key).
        locale = (self.mass.metadata.locale or "en_US").lower()
        language = "ru" if locale.startswith("ru") else "en"

        all_stations = await self.client.get_wave_stations(language)

        # Group stations by category, preserving image_url
        categorized: dict[str, list[tuple[str, str, str | None]]] = {}
        for station_id, cat_key, station_name, image_url in all_stations:
            categorized.setdefault(cat_key, []).append((station_id, station_name, image_url))

        # waves/ — show category folders
        if len(path_parts) == 1:
            folders: list[BrowseFolder] = []
            # Personalized stations first — only show if dashboard returns stations
            dashboard_stations = await self._get_dashboard_stations_cached()
            if dashboard_stations:
                name, translation_key = self._media_label("folder", MY_WAVES_FOLDER_ID, "Personal")
                folders.append(
                    BrowseFolder(
                        item_id=MY_WAVES_FOLDER_ID,
                        provider=self.instance_id,
                        path=f"{base}{MY_WAVES_FOLDER_ID}",
                        name=name,
                        translation_key=translation_key,
                        is_playable=False,
                    )
                )
            # Featured Mixes — only show if landing-blocks/waves returns data
            waves_landing = await self._get_waves_landing_cached()
            if waves_landing:
                name, translation_key = self._media_label(
                    "folder", WAVES_LANDING_FOLDER_ID, "Featured Mixes"
                )
                folders.append(
                    BrowseFolder(
                        item_id=WAVES_LANDING_FOLDER_ID,
                        provider=self.instance_id,
                        path=f"{base}{WAVES_LANDING_FOLDER_ID}",
                        name=name,
                        translation_key=translation_key,
                        is_playable=False,
                    )
                )
            for cat in WAVE_CATEGORY_DISPLAY_ORDER:
                if cat in categorized:
                    name, translation_key = self._media_label("folder", cat, cat.title())
                    folders.append(
                        BrowseFolder(
                            item_id=cat,
                            provider=self.instance_id,
                            path=f"{base}{cat}",
                            name=name,
                            translation_key=translation_key,
                            is_playable=False,
                        )
                    )
            # Append any categories returned by API that aren't in the predefined order
            for cat in categorized:
                if cat not in WAVE_CATEGORY_DISPLAY_ORDER:
                    name, translation_key = self._media_label("folder", cat, cat.title())
                    folders.append(
                        BrowseFolder(
                            item_id=cat,
                            provider=self.instance_id,
                            path=f"{base}{cat}",
                            name=name,
                            translation_key=translation_key,
                            is_playable=False,
                        )
                    )
            return folders

        category: str | None = path_parts[1] if len(path_parts) > 1 else None
        tag: str | None = path_parts[2] if len(path_parts) > 2 else None

        # waves/my_waves/ — show personalized stations from dashboard
        if category == MY_WAVES_FOLDER_ID and not tag:
            return await self._browse_my_waves_stations(path)

        # waves/waves_landing/... — redirect to Featured Mixes browse
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
        """
        Get personalized dashboard stations, cached for 10 minutes.

        :return: List of (station_id, name, image_url) tuples.
        """
        return await self.client.get_dashboard_stations()

    async def _browse_my_waves_stations(self, path: str) -> list[BrowseFolder]:
        """
        Browse personalized wave stations from rotor/stations/dashboard.

        Names are resolved from the non-personalized station list so that
        stations show their actual genre/mood name (e.g. "Рок") rather than
        the generic "Мой микс" label that the dashboard API returns.

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
        """
        Browse a rotor wave station and return tracks.

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
            raw_tracks, batch_id = await self.client.get_rotor_station_tracks(
                station_id, queue=state.last_track_id
            )
            if batch_id:
                state.batch_id = batch_id

            if not state.radio_started_sent and raw_tracks:
                sent = await self.client.send_rotor_station_feedback(
                    station_id,
                    "radioStarted",
                    batch_id=batch_id,
                )
                if sent:
                    state.radio_started_sent = True

            tracks: list[Track] = []
            first_track_id: str | None = None
            for yt in raw_tracks:
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
            # This mirrors the My Mix mechanism and enables continuous radio playback.
            if tracks and len(state.seen_track_ids) < max_tracks and path:
                # Append /next to the current path (same pattern as _browse_my_wave).
                # This makes each "Load more" path unique (e.g. /next/next/next...)
                # so MA never serves a cached result for subsequent presses.
                result.append(
                    BrowseFolder(
                        item_id="next",
                        provider=self.instance_id,
                        path=f"{path.rstrip('/')}/next",
                        name="Load more",
                        translation_key="load_more",
                        is_playable=False,
                    )
                )

            return result

    @staticmethod
    def _extract_wave_item_cover(item: dict[str, Any]) -> tuple[str | None, str | None]:
        """
        Extract cover URI and background color from a wave/mix item.

        :param item: Wave or mix item dict from the API.
        :return: (cover_uri, bg_color) tuple where bg_color is a hex string or None.
        """
        agent_uri = item.get("agent", {}).get("cover", {}).get("uri", "")
        cover_uri = agent_uri or item.get("compact_image_url")
        bg_color = item.get("colors", {}).get("average")
        return cover_uri, bg_color

    @use_cache(3600)
    async def _get_mixes_waves_cached(self) -> list[dict[str, Any]] | None:
        """
        Get AI Wave Set data from /landing-blocks/mixes-waves, cached for 1 hour.

        :return: List of mix category dicts from the API, or None on error.
        """
        return await self.client.get_mixes_waves()

    @use_cache(3600)
    async def _get_waves_landing_cached(self) -> list[dict[str, Any]] | None:
        """
        Get Featured Mixes data from /landing-blocks/waves, cached for 1 hour.

        :return: List of wave category dicts from the API, or None on error.
        """
        return await self.client.get_waves_landing()

    async def _browse_waves_landing(
        self, path: str, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse Featured Mixes (from /landing-blocks/waves).

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
        """
        Browse wave-like category folders and their station items.

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
        """
        Browse AI Mix Sets (from /landing-blocks/mixes-waves).

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
        """
        Get playlists for a tag and return as browse items.

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
        """
        Perform search on KION Music.

        :param search_query: The search query.
        :param media_types: List of media types to search for.
        :param limit: Maximum number of results per type.
        :return: SearchResults with found items.
        """
        result = SearchResults()

        # Determine search type based on requested media types
        # Map MediaType to Kion API search type
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
        """
        Get artist details by ID, enriched with description and listener stats.

        :param prov_artist_id: The provider artist ID.
        :return: Artist object.
        :raises MediaNotFoundError: If artist not found.
        """
        artist, about = await asyncio.gather(
            self.client.get_artist(prov_artist_id),
            self.client.get_artist_about(prov_artist_id),
        )
        if not artist:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
        return parse_artist(self, artist, about=about)

    @use_cache(3600 * 24 * 30)
    async def get_album(self, prov_album_id: str) -> Album:
        """
        Get album details by ID.

        :param prov_album_id: The provider album ID.
        :return: Album object.
        :raises MediaNotFoundError: If album not found.
        """
        album = await self.client.get_album(prov_album_id)
        if not album:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        return parse_album(self, album)

    async def get_track(self, prov_track_id: str) -> Track:
        """
        Get track details by ID.

        Supports composite item_id (track_id@station_id) for My Mix tracks;
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
        """
        Get track details by normalized ID (cached).

        :param track_id: Normalized track ID (without station suffix).
        :return: Track object.
        :raises MediaNotFoundError: If track not found.
        """
        raw_track = await self.client.get_track(track_id)
        if not raw_track:
            raise MediaNotFoundError(f"Track {track_id} not found")

        # Use the already-fetched track object to avoid a duplicate API call
        lyrics, lyrics_synced = await self.client.get_track_lyrics_from_track(raw_track)

        return parse_track(self, raw_track, lyrics=lyrics, lyrics_synced=lyrics_synced)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """
        Get playlist details by ID.

        Supports virtual playlists MY_WAVE_PLAYLIST_ID (My Mix) and
        LIKED_TRACKS_PLAYLIST_ID (Liked Tracks). Real playlists use format "owner_id:kind".

        :param prov_playlist_id: The provider playlist ID (format: "owner_id:kind",
            my_wave, or liked_tracks).
        :return: Playlist object.
        :raises MediaNotFoundError: If playlist not found.
        """
        # Virtual playlists - constructed locally (no API call). translation_key localizes
        # the name for the connection locale at serialization; the English name is kept (not
        # dropped like browse/recommendation folders) because a playable item's name is also
        # read outside outbound API serialization (e.g. queue / now-playing metadata).
        if prov_playlist_id == MY_WAVE_PLAYLIST_ID:
            return Playlist(
                item_id=MY_WAVE_PLAYLIST_ID,
                provider=self.instance_id,
                name="My Mix",
                translation_key=MY_WAVE_PLAYLIST_ID,
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
            return Playlist(
                item_id=LIKED_TRACKS_PLAYLIST_ID,
                provider=self.instance_id,
                name="My Favorites",
                translation_key=LIKED_TRACKS_PLAYLIST_ID,
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
        """
        Get real playlist details by ID (cached).

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
        """
        Get My Mix tracks for virtual playlist (uncached; uses cursor for page > 0).

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

                raw_tracks, batch_id = await self.client.get_my_wave_tracks(queue=queue)
                if batch_id:
                    self._my_wave_batch_id = batch_id
                if not self._my_wave_radio_started_sent and raw_tracks:
                    sent = await self.client.send_rotor_station_feedback(
                        ROTOR_STATION_MY_MIX,
                        "radioStarted",
                        batch_id=batch_id,
                    )
                    if sent:
                        self._my_wave_radio_started_sent = True

                if not raw_tracks:
                    break

                first_track_id_this_batch = None
                for yt in raw_tracks:
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
        """
        Get liked tracks for virtual playlist (sorted in reverse chronological order).

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

    @use_cache(3600 * 24 * 30, allow_expired_cache=True)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """
        Get album tracks.

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

    @use_cache(3600 * 3, allow_expired_cache=True)
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """
        Get similar tracks using Kion Rotor station for this track.

        Uses rotor station track:{id} so MA radio mode gets Kion recommendations.

        :param prov_track_id: Provider track ID (plain or track_id@station_id).
        :param limit: Maximum number of tracks to return.
        :return: List of similar Track objects.
        """
        track_id, _ = _parse_radio_item_id(prov_track_id)
        station_id = f"track:{track_id}"
        raw_tracks, _ = await self.client.get_rotor_station_tracks(station_id, queue=None)
        tracks = []
        for yt in raw_tracks[:limit]:
            try:
                tracks.append(parse_track(self, yt))
            except InvalidDataError as err:
                self.logger.debug("Error parsing similar track: %s", err)
        return tracks

    @use_cache(3600 * 3)
    async def get_similar_artists(self, prov_artist_id: str, limit: int = 25) -> list[Artist]:
        """
        Get artists similar to the given one via Kion artists/similar endpoint.

        :param prov_artist_id: Provider artist ID.
        :param limit: Maximum number of artists to return.
        :return: List of similar Artist objects.
        """
        raw_artists = await self.client.get_similar_artists(prov_artist_id, limit=limit)
        artists: list[Artist] = []
        for ya in raw_artists:
            try:
                artists.append(parse_artist(self, ya))
            except InvalidDataError as err:
                self.logger.debug("Error parsing similar artist: %s", err)
        return artists

    @use_cache(600)
    async def _get_my_wave_recommendations(self) -> RecommendationFolder | None:
        """
        Get My Mix recommendation folder with personalized tracks.

        :return: RecommendationFolder with My Mix tracks, or None if empty.
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

            raw_tracks, _ = await self.client.get_my_wave_tracks(queue=queue)
            if not raw_tracks:
                break

            first_track_id_this_batch = None
            for yt in raw_tracks:
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

        # Recommendation folders keep their English name (not dropped like browse folders):
        # MusicProvider.browse() re-wraps them into plain BrowseFolders for the
        # "<provider>://recommendations" listing, where a bare translation_key would resolve
        # under the wrong media group. translation_key still localizes the recommendations view.
        return RecommendationFolder(
            item_id=MY_WAVE_PLAYLIST_ID,
            provider=self.instance_id,
            name="My Mix",
            translation_key=MY_WAVE_PLAYLIST_ID,
            items=UniqueList(items),
            icon="mdi-waveform",
        )

    @use_cache(1800)
    async def _get_feed_recommendations(self) -> RecommendationFolder | None:
        """
        Get personalized feed playlists (Playlist of the Day, DejaVu, etc.).

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
        return RecommendationFolder(
            item_id="feed",
            provider=self.instance_id,
            name="Made for you",
            translation_key="made_for_you",
            items=UniqueList(items),
            icon="mdi-account-music",
        )

    @use_cache(3600)
    async def _get_chart_recommendations(self) -> RecommendationFolder | None:
        """
        Get chart tracks (hot tracks of the month).

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
        return RecommendationFolder(
            item_id="chart",
            provider=self.instance_id,
            name="Chart",
            translation_key="chart",
            items=UniqueList(tracks),
            icon="mdi-chart-line",
        )

    @use_cache(3600)
    async def _get_new_releases_recommendations(self) -> RecommendationFolder | None:
        """
        Get new album releases.

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
        return RecommendationFolder(
            item_id="new_releases",
            provider=self.instance_id,
            name="New Releases",
            translation_key="new_releases",
            items=UniqueList(albums),
            icon="mdi-new-box",
        )

    @use_cache(3600)
    async def _get_new_playlists_recommendations(self) -> RecommendationFolder | None:
        """
        Get new editorial playlists.

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
        return RecommendationFolder(
            item_id="new_playlists",
            provider=self.instance_id,
            name="New Playlists",
            translation_key="new_playlists",
            items=UniqueList(playlists),
            icon="mdi-playlist-star",
        )

    @use_cache(3600)
    async def _get_top_picks_recommendations(self) -> RecommendationFolder | None:
        """
        Get Top Picks recommendation folder (tag: top).

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
        return RecommendationFolder(
            item_id="top_picks",
            provider=self.instance_id,
            name="Top Picks",
            translation_key="top_picks",
            items=UniqueList(items),
            icon="mdi-star",
        )

    @use_cache(1800)
    async def _get_mood_mix_recommendations(self, mood_tag: str) -> RecommendationFolder | None:
        """
        Get Mood Mix recommendation folder for a specific tag.

        :param mood_tag: Preselected mood tag slug.
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
        tag_name = self._media_source_name("folder", _media_label_key(mood_tag)) or mood_tag.title()
        return RecommendationFolder(
            item_id="mood_mix",
            provider=self.instance_id,
            name=f"Mood Mix: {tag_name}",
            translation_key="mood_mix",
            translation_params=[tag_name],
            items=UniqueList(items),
            icon="mdi-emoticon-outline",
        )

    @use_cache(1800)
    async def _get_activity_mix_recommendations(
        self, activity_tag: str
    ) -> RecommendationFolder | None:
        """
        Get Activity Mix recommendation folder for a specific tag.

        :param activity_tag: Preselected activity tag slug.
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
        tag_name = (
            self._media_source_name("folder", _media_label_key(activity_tag))
            or activity_tag.title()
        )
        return RecommendationFolder(
            item_id="activity_mix",
            provider=self.instance_id,
            name=f"Activity Mix: {tag_name}",
            translation_key="activity_mix",
            translation_params=[tag_name],
            items=UniqueList(items),
            icon="mdi-run",
        )

    @use_cache(3600 * 6)
    async def _get_seasonal_mix_recommendations(self) -> RecommendationFolder | None:
        """
        Get Seasonal Mix recommendation folder (based on current month).

        :return: RecommendationFolder with seasonal playlists, or None if unavailable.
        """
        # Determine current season tag
        current_month = utc().month
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
        tag_name = (
            self._media_source_name("folder", _media_label_key(seasonal_tag))
            or seasonal_tag.title()
        )
        return RecommendationFolder(
            item_id="seasonal_mix",
            provider=self.instance_id,
            name=f"Seasonal: {tag_name}",
            translation_key="seasonal_mix",
            translation_params=[tag_name],
            items=UniqueList(items),
            icon="mdi-weather-sunny",
        )

    @use_cache(3600 * 3, allow_expired_cache=True)
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """
        Get playlist tracks.

        :param prov_playlist_id: The provider playlist ID (format: "owner_id:kind",
            my_wave, or liked_tracks).
        :param page: Page number for pagination.
        :return: List of Track objects.
        """
        self.logger.debug(
            "get_playlist_tracks called: prov_playlist_id=%s, page=%s", prov_playlist_id, page
        )

        if prov_playlist_id == MY_WAVE_PLAYLIST_ID:
            self.logger.debug("Fetching My Mix tracks")
            return await self._get_my_wave_playlist_tracks(page)

        if prov_playlist_id == LIKED_TRACKS_PLAYLIST_ID:
            self.logger.debug("Fetching Liked Tracks for virtual playlist")
            result = await self._get_liked_tracks_playlist_tracks(page)
            self.logger.debug("Liked Tracks playlist returned %s tracks", len(result))
            return result

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

        # Kion returns TrackShort objects, we need to fetch full track info
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

    @use_cache(3600 * 24 * 7, allow_expired_cache=True)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """
        Get artist's albums.

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

    @use_cache(3600 * 24 * 7, allow_expired_cache=True)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """
        Get artist's top tracks.

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

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve library artists from KION Music."""
        artists = await self.client.get_liked_artists()
        for artist in artists:
            try:
                yield parse_artist(self, artist)
            except InvalidDataError as err:
                self.logger.debug("Error parsing library artist: %s", err)

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve library albums from KION Music."""
        batch_size = TRACK_BATCH_SIZE
        albums = await self.client.get_liked_albums(batch_size=batch_size)
        for album in albums:
            try:
                yield parse_album(self, album)
            except InvalidDataError as err:
                self.logger.debug("Error parsing library album: %s", err)

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve library tracks from KION Music."""
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

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """
        Retrieve library playlists from KION Music.

        Includes virtual playlists (My Mix and Liked Tracks if enabled), user-created playlists,
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
        """
        Add item to library.

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
        """
        Remove item from library.

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
        """
        Get stream details for a track.

        :param item_id: The track ID (or track_id@station_id for My Mix).
        :param media_type: The media type (should be TRACK).
        :return: StreamDetails for the track.
        """
        return await self.streaming.get_stream_details(item_id)

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Return the audio stream for the provider item.

        Uses windowed Range-request streaming to prevent Kion CDN drops.
        Handles both raw (direct) and encrypted (encraw) transports.

        :param streamdetails: Stream details with URL and optional decryption key.
        :param seek_position: Seek position in seconds (handled by provider for raw transport).
        :return: Async generator yielding audio chunks.
        """
        async for chunk in self.streaming.get_audio_stream(streamdetails, seek_position):
            yield chunk

    async def get_rotor_station_tracks(
        self, station_id: str, queue: str | int | None = None
    ) -> tuple[list[Any], str | None]:
        """
        Fetch tracks from a rotor station (My Mix, similar, etc.).

        Wrapper around client.get_rotor_station_tracks for use by ynison plugin.
        """
        return await self.client.get_rotor_station_tracks(station_id, queue=queue)

    def get_quality(self) -> str:
        """
        Return the configured audio quality tier (e.g. 'balanced', 'superb').

        Mirrors the legacy-value normalization used by the streaming layer:
        older configs store the lossless tier as ``"lossless"``, while the
        current canonical value is ``QUALITY_LOSSLESS`` (``"superb"``).
        External callers (e.g. the ynison plugin wrapper) see the same
        normalized value the streaming code would resolve to.
        """
        quality = str(self.config.get_value(CONF_QUALITY) or "").strip().lower()
        if quality == "lossless":
            quality = QUALITY_LOSSLESS
        return quality

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Resolve wave cover image with background color fill for transparent PNGs.

        If the image URL has an associated background color (stored in _wave_bg_colors),
        downloads the PNG from Kion CDN and composites it on a solid color background
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
            except ValueError, IndexError:
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
        """
        Report playback for rotor feedback when the track is from My Mix.

        Sends trackStarted when the track is currently playing (is_playing=True).
        trackFinished/skip are sent from on_streamed to use accurate seconds_streamed.
        """
        if media_type != MediaType.TRACK:
            return
        track_id, station_id = _parse_radio_item_id(prov_item_id)
        if not station_id:
            return
        if is_playing:
            if station_id == ROTOR_STATION_MY_MIX:
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

    async def on_streamed(self, streamdetails: StreamDetails) -> None:
        """
        Report stream completion for My Mix rotor feedback.

        Sends trackFinished or skip with actual seconds_streamed so Kion
        can improve recommendations.
        """
        track_id, station_id = _parse_radio_item_id(streamdetails.item_id)
        if not station_id:
            return
        seconds = int(streamdetails.seconds_streamed or 0)
        duration = streamdetails.duration or 0
        feedback_type = "trackFinished" if duration and seconds >= max(0, duration - 10) else "skip"
        if station_id == ROTOR_STATION_MY_MIX:
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

    async def _rotating_row_tag_subtitle(self, category: str) -> str | None:
        """
        Return the display label of the current rotating tag for a mood/activity row.

        Cache-only read of the validated tag list (rows must stay free of backend I/O):
        returns None - no subtitle - until an items fetch has warmed that cache.

        :param category: Tag category ('mood' or 'activity').
        """
        # key mirrors the @use_cache key construction on _get_valid_tags_for_category:
        # the wrapped function's __name__ (preserved by functools.wraps, so it survives
        # renames) plus its positional args, joined by dots
        tags, _, found = await self.mass.cache.get_with_freshness(
            f"{self._get_valid_tags_for_category.__name__}.{category}",
            provider=self.instance_id,
            include_expired=True,
        )
        if not found or not tags:
            return None
        tag = self._rotating_row_tag(category, tags)
        return self._media_source_name("folder", _media_label_key(tag)) or tag.title()

    def _rotating_row_tag(self, category: str, valid_tags: list[str]) -> str:
        """
        Deterministically pick the current hour's tag for a mood/activity row.

        Rows and items derive the same tag independently - no shared state, so
        concurrent clients (or multiple users on one instance) can never make the
        served items mismatch the row subtitle. The pick rotates hourly and
        differs per provider instance.

        :param category: Tag category the tags belong to.
        :param valid_tags: Non-empty list of valid tag slugs to pick from.
        """
        hour_bucket = int(utc().timestamp()) // 3600
        seed = f"{self.instance_id}.{category}.{hour_bucket}".encode()
        return sorted(valid_tags)[zlib.crc32(seed) % len(valid_tags)]
