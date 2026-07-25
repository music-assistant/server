"""Yandex Music provider implementation."""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
import zlib
from collections.abc import AsyncGenerator, Sequence
from io import BytesIO
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ImageType, MediaType, ProviderFeature, StreamType
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
    Audiobook,
    BrowseFolder,
    ItemMapping,
    MediaItemChapter,
    MediaItemImage,
    MediaItemType,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails
from PIL import Image as PilImage
from ya_passport_auth import SecretStr

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.datetime import utc
from music_assistant.models.music_provider import MusicProvider

from .api_client import YandexMusicClient
from .auth import refresh_credentials_via_passport, refresh_music_token
from .constants import (
    BROWSE_INITIAL_TRACKS,
    COLLECTION_FOLDER_ID,
    CONF_BASE_URL,
    CONF_LIKED_TRACKS_MAX_TRACKS,
    CONF_MY_WAVE_MAX_TRACKS,
    CONF_QUALITY,
    CONF_REFRESH_TOKEN,
    CONF_RESTRICTIVE_RATE_LIMITS,
    CONF_TOKEN,
    CONF_WAVE_PRESETS_DATA,
    CONF_X_TOKEN,
    DEFAULT_BASE_URL,
    DISCOVERY_INITIAL_TRACKS,
    FOR_YOU_FOLDER_ID,
    IMAGE_SIZE_MEDIUM,
    LIKED_BATCH_JITTER_MIN_S,
    LIKED_BATCH_JITTER_SPAN_S,
    LIKED_TRACKS_PLAYLIST_ID,
    LISTENING_HISTORY_FOLDER_ID,
    MY_WAVE_BATCH_SIZE,
    MY_WAVE_MODES_FOLDER_ID,
    MY_WAVE_PLAYLIST_ID,
    MY_WAVE_PRESETS_FOLDER_ID,
    MY_WAVES_FOLDER_ID,
    MY_WAVES_SET_FOLDER_ID,
    PINNED_ITEMS_FOLDER_ID,
    PLAYLIST_ID_SPLITTER,
    QUALITY_BALANCED,
    QUALITY_SUPERB,
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
    WAVE_MODE_ORDER,
    WAVE_MODE_PRESETS,
    WAVE_MODE_SEP,
    WAVES_FOLDER_ID,
    WAVES_LANDING_FOLDER_ID,
)
from .parsers import (
    _get_image_url as get_image_url,
)
from .parsers import (
    classify_album,
    get_canonical_provider_name,
    parse_album,
    parse_artist,
    parse_audiobook,
    parse_playlist,
    parse_podcast,
    parse_podcast_episode,
    parse_track,
)
from .presets import parse_stored_presets
from .streaming import YandexMusicStreamingManager

if TYPE_CHECKING:
    from yandex_music import Album as YandexAlbum
    from yandex_music import Track as YandexTrack


# MediaType sub-paths that MA's default MusicProvider.browse() understands.
# Used by the Collection dispatcher to delegate nested paths back to core.
_COLLECTION_SUB_FOLDERS: frozenset[str] = frozenset(
    {"tracks", "artists", "albums", "playlists", "audiobooks", "podcasts"}
)

# Collection sub-folder rows: (ProviderFeature, browse sub_id, strings.json label key,
# is_playable). The sub_id ("tracks") and label key ("my_favorites") differ on purpose so the
# Collection labels stay distinct from the core "media.folder.*" library labels.
_COLLECTION_SUBFOLDERS: tuple[tuple[ProviderFeature, str, str, bool], ...] = (
    (ProviderFeature.LIBRARY_TRACKS, "tracks", "my_favorites", True),
    (ProviderFeature.LIBRARY_ARTISTS, "artists", "my_artists", True),
    (ProviderFeature.LIBRARY_ALBUMS, "albums", "my_albums", True),
    (ProviderFeature.LIBRARY_PLAYLISTS, "playlists", "my_playlists", True),
    (ProviderFeature.LIBRARY_PODCASTS, "podcasts", "my_podcasts", False),
    (ProviderFeature.LIBRARY_AUDIOBOOKS, "audiobooks", "my_audiobooks", False),
)


def _media_label_key(slug: str) -> str:
    """Normalize a tag/category slug into its strings.json authoring key (spaces → underscores)."""
    return slug.replace(" ", "_")


def _split_wave_mode(station_id: str) -> tuple[str, dict[str, str]]:
    """
    Split a wave-mode station key into its base station ID and preset settings.

    Keys like ``user:onyourwave#discover`` encode a specific preset on top of
    the base rotor station. The part before ``#`` is the station ID that goes
    to Yandex; the part after is a key into WAVE_MODE_PRESETS.

    :param station_id: Station key, with or without a ``#preset`` suffix.
    :return: Tuple of (base_station_id, settings_dict). The suffix, if
        present, is always stripped — only the base station goes to
        Yandex. ``settings_dict`` is the preset's settings when the suffix
        matches a known WAVE_MODE_PRESETS key, or an empty dict otherwise
        (unknown suffix → base station fired with no extra seeds).
    """
    if WAVE_MODE_SEP not in station_id:
        return (station_id, {})
    base, preset = station_id.split(WAVE_MODE_SEP, 1)
    return (base, dict(WAVE_MODE_PRESETS.get(preset, {})))


def _parse_radio_item_id(item_id: str) -> tuple[str, str | None]:
    """
    Extract track_id and optional station_id from provider item_id.

    My Wave tracks use item_id format 'track_id@station_id'. Other tracks use
    plain track_id.

    :param item_id: Provider item_id (may contain RADIO_TRACK_ID_SEP).
    :return: (track_id, station_id or None).
    """
    if RADIO_TRACK_ID_SEP in item_id:
        parts = item_id.split(RADIO_TRACK_ID_SEP, 1)
        return (parts[0], parts[1] if len(parts) > 1 else None)
    return (item_id, None)


def _extract_chapter_map_from_album(album: YandexAlbum) -> tuple[list[str], list[int]]:
    """
    Flatten an audiobook album's volumes into (chapter_track_ids, chapter_durations_ms).

    Shared by ``_get_audiobook_stream_details`` and ``_resolve_audiobook_chapter_map``
    so the two code paths can't drift (e.g. when we later filter bad tracks).
    """
    chapter_ids: list[str] = []
    chapter_durations_ms: list[int] = []
    for disc in album.volumes or []:
        for track_obj in disc:
            chapter_ids.append(str(track_obj.id))
            chapter_durations_ms.append(int(track_obj.duration_ms or 0))
    return chapter_ids, chapter_durations_ms


class _WaveState:
    """
    Per-station mutable state for rotor wave playback.

    Holds both the new session-based rotor identifiers (`session_id`) and the
    legacy stations-based ones (`batch_id`). Call sites prefer `session_id`
    when present; `batch_id` is still carried because feedback events anchor
    to a specific batch within the session.
    """

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.batch_id: str | None = None
        self.last_track_id: str | None = None
        self.playlist_next_cursor: str | None = None
        self.seen_track_ids: set[str] = set()
        self.radio_started_sent: bool = False
        self.prefetched: list[Any] = []
        self.settings: dict[str, str] = {}
        self.lock: asyncio.Lock = asyncio.Lock()


class YandexMusicProvider(MusicProvider):
    """Implementation of a Yandex Music MusicProvider."""

    _client: YandexMusicClient | None = None
    _streaming: YandexMusicStreamingManager | None = None
    _wave_states: dict[str, _WaveState]  # Per-station state (incl. My Wave)
    _wave_bg_colors: dict[str, str]  # image_url -> hex bg color for transparent covers
    # Short-lived cache to dedupe the three library syncs (albums/podcasts/audiobooks)
    # that all derive from the same liked-albums endpoint.
    _liked_albums_cache: tuple[float, list[YandexAlbum]] | None = None
    _liked_albums_lock: asyncio.Lock
    # Per-audiobook cache of (chapter_track_ids, chapter_durations_ms) used to
    # report playback progress per chapter via play_audio.
    _audiobook_chapter_cache: dict[str, tuple[list[str], list[int]]]
    # Stable play_id per audiobook session, cleared in on_streamed.
    _audiobook_play_ids: dict[str, str]

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

    def _media_label(self, group: str, key: str, fallback: str) -> tuple[str, str | None]:
        """
        Resolve a media label to its English ``name`` and ``translation_key``.

        The English source string lives in the provider's ``strings.json`` (the single source
        of truth) and is localized for the connection locale at serialization via the returned
        key. An unauthored key — e.g. a tag discovered from Yandex's landing API — returns
        ``(fallback, None)`` so its already-localized name is kept verbatim.

        :param group: Media translation group (``folder``, ``recommendations`` or ``playlist``).
        :param key: Authoring key within the group; also the item's ``translation_key``.
        :param fallback: English name to use when no string is authored for *key*.
        """
        authored = self.mass.translations.get_translation(
            f"provider.{self.domain}.media.{group}.{key}.name"
        )
        if authored is None:
            return fallback, None
        return authored, key

    async def _reauth_via_refresh_token(
        self, x_token: str, refresh_token: str, base_url: str, original_err: Exception
    ) -> None:
        """
        Silently re-issue full credentials when x_token refresh fails.

        Device-flow accounts have a refresh_token that can mint a new
        x_token + refresh_token + music_token without any user interaction.
        Persists the rotated triple and connects the client. Any failure
        here is terminal — clears all credentials and forces re-auth.
        """
        try:
            new_creds = await refresh_credentials_via_passport(
                SecretStr(x_token), SecretStr(refresh_token)
            )
        except ResourceTemporarilyUnavailable as err2:
            # Transient Passport failure — keep creds, let MA retry later
            self.logger.warning(
                "Credential refresh temporarily unavailable: %s", type(err2).__name__
            )
            raise ProviderUnavailableError(
                "Unable to refresh credentials right now. Please try again later."
            ) from err2
        except LoginFailed as err2:
            self.logger.warning("Session and refresh tokens are both expired")
            self._update_setup_data(CONF_TOKEN, None)
            self._update_setup_data(CONF_X_TOKEN, None)
            self._update_setup_data(CONF_REFRESH_TOKEN, None)
            raise LoginFailed("Session expired. Please re-authenticate.") from err2

        new_music_token = new_creds.music_token
        new_refresh_token = new_creds.refresh_token
        if new_music_token is None or new_refresh_token is None:
            self._update_setup_data(CONF_TOKEN, None)
            self._update_setup_data(CONF_X_TOKEN, None)
            self._update_setup_data(CONF_REFRESH_TOKEN, None)
            raise LoginFailed(
                "Credential refresh returned an incomplete response."
            ) from original_err

        self._update_setup_data(CONF_TOKEN, new_music_token.get_secret())
        self._update_setup_data(CONF_X_TOKEN, new_creds.x_token.get_secret())
        self._update_setup_data(CONF_REFRESH_TOKEN, new_refresh_token.get_secret())
        restrictive = bool(self.config.get_value(CONF_RESTRICTIVE_RATE_LIMITS, False))
        self._client = YandexMusicClient(
            new_music_token, base_url=base_url, restrictive_rate_limits=restrictive
        )
        await self._client.connect()
        self.logger.info("Re-issued credentials silently from refresh token")

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        token = self.get_setup_value(CONF_TOKEN)
        x_token = self.get_setup_value(CONF_X_TOKEN)
        refresh_token = self.get_setup_value(CONF_REFRESH_TOKEN)
        base_url = self.config.get_value(CONF_BASE_URL, DEFAULT_BASE_URL)
        restrictive = bool(self.config.get_value(CONF_RESTRICTIVE_RATE_LIMITS, False))

        if not token and not x_token:
            raise LoginFailed("No Yandex Music token provided. Please authenticate.")

        # Try existing music token first (fast path)
        if token:
            try:
                self._client = YandexMusicClient(
                    SecretStr(str(token)),
                    base_url=str(base_url),
                    restrictive_rate_limits=restrictive,
                )
                await self._client.connect()
            except LoginFailed:
                self.logger.warning("Music token is invalid or expired")
                # Clear the dead token so restarts go straight to refresh
                self._update_setup_data(CONF_TOKEN, None)
                if x_token:
                    self.logger.info("Attempting to refresh from session token")
                    token = None
                    self._client = None
                else:
                    raise

        # Refresh from x_token if music token absent or failed
        if not token and x_token:
            try:
                new_music_token = await refresh_music_token(SecretStr(str(x_token)))
                self._update_setup_data(CONF_TOKEN, new_music_token.get_secret())
                self._client = YandexMusicClient(
                    new_music_token,
                    base_url=str(base_url),
                    restrictive_rate_limits=restrictive,
                )
                await self._client.connect()
                self.logger.info("Refreshed music token from session token")
            except LoginFailed as err:
                # x_token refresh failed. If a refresh_token is available
                # (device-flow accounts), try silent re-issue of the full
                # credential triple before giving up.
                if refresh_token:
                    await self._reauth_via_refresh_token(
                        str(x_token), str(refresh_token), str(base_url), err
                    )
                else:
                    # Definitive auth failure — clear dead credentials
                    self.logger.warning("Session token is invalid or expired")
                    self._update_setup_data(CONF_TOKEN, None)
                    self._update_setup_data(CONF_X_TOKEN, None)
                    raise LoginFailed("Session token expired. Please re-authenticate.") from err
            except asyncio.CancelledError:
                raise
            except Exception as err:
                # Transient/network failure — keep credentials for retry
                self.logger.warning(
                    "Session token refresh failed (network): %s",
                    type(err).__name__,
                )
                raise ProviderUnavailableError(
                    "Unable to refresh music token right now. Please try again later."
                ) from err

        # Suppress yandex_music library DEBUG dumps (full API request/response JSON)
        logging.getLogger("yandex_music").setLevel(self.logger.level + 10)
        # Propagate the MA instance log level to our per-module loggers
        # (api_client, streaming, parsers, auth) so DEBUG hooks there actually
        # print when MA is set to DEBUG for this provider.
        logging.getLogger("music_assistant.providers.yandex_music").setLevel(self.logger.level)
        self._streaming = YandexMusicStreamingManager(self)
        # Per-station wave state (incl. My Wave under ROTOR_STATION_MY_WAVE).
        # Entries are created lazily by _get_wave_state() on first access.
        self._wave_states = {}
        self._wave_bg_colors = {}
        self._liked_albums_lock, self._liked_albums_cache = asyncio.Lock(), None
        self._audiobook_chapter_cache, self._audiobook_play_ids = {}, {}
        self.logger.info("Successfully connected to Yandex Music")

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        :param is_removed: Whether the provider is being removed.
        """
        if self._client:
            await self._client.disconnect()
        self._client = None
        self._streaming = None
        self._wave_states.clear()
        self._wave_bg_colors.clear()
        self._liked_albums_cache = None
        self._audiobook_chapter_cache.clear()
        self._audiobook_play_ids.clear()
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

    async def browse(  # noqa: PLR0911, PLR0915
        self, path: str
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse provider items with locale-based folder names and My Wave.

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
            async with self._get_wave_state(ROTOR_STATION_MY_WAVE).lock:
                return await self._browse_my_wave(path, sub_subpath)

        # Wave modes — accept two equivalent URL forms so both browse
        # navigation (slash form "my_wave_modes/<preset>", emitted by our
        # listing) and MA's play-time reconstruction (underscore form
        # "my_wave_modes_<preset>", built as "<instance>://<item_id>") work.
        mode_preset: str | None = None
        if subpath == MY_WAVE_MODES_FOLDER_ID and sub_subpath is None:
            return self._browse_my_wave_modes_list(path)
        if subpath == MY_WAVE_MODES_FOLDER_ID and sub_subpath is not None:
            mode_preset = sub_subpath if sub_subpath != "next" else None
            if mode_preset is None:
                return []
            load_more_modes = len(path_parts) > 2 and path_parts[2] == "next"
        elif subpath and subpath.startswith(f"{MY_WAVE_MODES_FOLDER_ID}_"):
            mode_preset = subpath[len(MY_WAVE_MODES_FOLDER_ID) + 1 :]
            load_more_modes = sub_subpath == "next"
        if mode_preset is not None:
            if mode_preset not in WAVE_MODE_PRESETS:
                return []
            station_key = f"{ROTOR_STATION_MY_WAVE}{WAVE_MODE_SEP}{mode_preset}"
            async with self._get_wave_state(station_key).lock:
                return await self._browse_my_wave_mode(path, station_key, load_more_modes)

        # User-saved wave presets — same dual-form handling.
        preset_idx: int | None = None
        load_more_presets = False
        if subpath == MY_WAVE_PRESETS_FOLDER_ID and sub_subpath is None:
            return self._browse_user_presets_list(path, self._get_user_wave_presets())
        if subpath == MY_WAVE_PRESETS_FOLDER_ID and sub_subpath is not None:
            try:
                preset_idx = int(sub_subpath)
            except ValueError:
                return []
            load_more_presets = len(path_parts) > 2 and path_parts[2] == "next"
        elif subpath and subpath.startswith(f"{MY_WAVE_PRESETS_FOLDER_ID}_"):
            try:
                preset_idx = int(subpath[len(MY_WAVE_PRESETS_FOLDER_ID) + 1 :])
            except ValueError:
                return []
            load_more_presets = sub_subpath == "next"
        if preset_idx is not None:
            user_presets = self._get_user_wave_presets()
            if not 0 <= preset_idx < len(user_presets):
                return []
            preset_data = user_presets[preset_idx]
            station_key = f"{ROTOR_STATION_MY_WAVE}{WAVE_MODE_SEP}preset_{preset_idx}"
            wave = self._get_wave_state(station_key)
            # Stash user-chosen settings so _fetch_rotor_session_batch sends them
            wave.settings = {
                k: v
                for k, v in preset_data.items()
                if k in ("diversity", "moodEnergy", "language") and v
            }
            async with wave.lock:
                return await self._browse_my_wave_mode(path, station_key, load_more_presets)

        # For You folder (picks + mixes)
        if subpath == FOR_YOU_FOLDER_ID:
            return await self._browse_for_you(path, path_parts)

        # Collection folder (library items). Two shapes:
        #   <prov>://collection              → listing of library sub-folders
        #   <prov>://collection/<sub>        → delegate to MA's library handler
        # The nested form is what lets MA's "back" button return here (strip
        # last /-segment) instead of dumping the user at the provider root.
        if subpath == COLLECTION_FOLDER_ID:
            if sub_subpath in _COLLECTION_SUB_FOLDERS:
                return await super().browse(f"{self.instance_id}://{sub_subpath}")
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

        # Pinned items folder
        if subpath == PINNED_ITEMS_FOLDER_ID:
            return await self._browse_pins()

        # Listening history folder
        if subpath == LISTENING_HISTORY_FOLDER_ID:
            return await self._browse_history()

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
            "audiobooks",
            "podcasts",
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

        # The English name on each folder doubles as the fallback; translation_key localizes
        # it for the connection locale at serialization (the server is the single source).
        folders: list[BrowseFolder] = []
        base = path if path.endswith("//") else path.rstrip("/") + "/"
        # My Wave folder (always enabled — Яндекс «Моя волна»)
        folders.append(
            BrowseFolder(
                item_id=MY_WAVE_PLAYLIST_ID,
                provider=self.instance_id,
                path=f"{base}{MY_WAVE_PLAYLIST_ID}",
                name="My Wave",
                translation_key=MY_WAVE_PLAYLIST_ID,
                is_playable=True,
            )
        )
        # Wave modes folder (P4): discover / calm / active / language presets
        folders.append(
            BrowseFolder(
                item_id=MY_WAVE_MODES_FOLDER_ID,
                provider=self.instance_id,
                path=f"{base}{MY_WAVE_MODES_FOLDER_ID}",
                name="Wave Modes",
                translation_key=MY_WAVE_MODES_FOLDER_ID,
                is_playable=False,
            )
        )
        # User-defined wave presets (P8) — shown only when any configured.
        if self._get_user_wave_presets():
            folders.append(
                BrowseFolder(
                    item_id=MY_WAVE_PRESETS_FOLDER_ID,
                    provider=self.instance_id,
                    path=f"{base}{MY_WAVE_PRESETS_FOLDER_ID}",
                    name="My Presets",
                    translation_key=MY_WAVE_PRESETS_FOLDER_ID,
                    is_playable=False,
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
        # Radio folder — rotor stations (Яндекс волны, shown as Radio)
        folders.append(
            BrowseFolder(
                item_id=RADIO_FOLDER_ID,
                provider=self.instance_id,
                path=f"{base}{RADIO_FOLDER_ID}",
                name="Radio",
                translation_key=RADIO_FOLDER_ID,
                is_playable=False,
            )
        )
        # AI Wave Sets — parametric stations from /landing-blocks/mixes-waves
        folders.append(
            BrowseFolder(
                item_id=MY_WAVES_SET_FOLDER_ID,
                provider=self.instance_id,
                path=f"{base}{MY_WAVES_SET_FOLDER_ID}",
                name="AI Wave Sets",
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

    async def _browse_my_wave(
        self, path: str, sub_subpath: str | None
    ) -> list[Track | BrowseFolder]:
        """
        Browse My Wave tracks (must be called under the My Wave state lock).

        :param path: Full browse path.
        :param sub_subpath: Sub-path part ('next' for load more, or track_id cursor).
        :return: List of Track and optional BrowseFolder for "Load more".
        """
        wave = self._get_wave_state(ROTOR_STATION_MY_WAVE)
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
            wave.seen_track_ids = set()

        queue: str | int | None = None
        if sub_subpath == "next":
            queue = wave.last_track_id
        elif sub_subpath:
            queue = sub_subpath

        all_tracks: list[Track | BrowseFolder] = []
        last_batch_id: str | None = None
        first_track_id_this_batch: str | None = None
        total_track_count = 0

        for _ in range(max_batches):
            if total_track_count >= effective_limit:
                break

            # On a fresh browse (non-"next"), honour any sub_subpath cursor override
            # by seeding wave.last_track_id so the helper picks it up.
            if queue is not None:
                wave.last_track_id = str(queue)
            yandex_tracks, batch_id = await self._fetch_rotor_session_batch(
                wave, ROTOR_STATION_MY_WAVE
            )
            if batch_id:
                last_batch_id = batch_id
            if not wave.radio_started_sent and yandex_tracks:
                sent = await self._send_wave_feedback(wave, ROTOR_STATION_MY_WAVE, "radioStarted")
                if sent:
                    wave.radio_started_sent = True
            first_track_id_this_batch = None
            for yt in yandex_tracks:
                if total_track_count >= effective_limit:
                    break

                track = self._parse_my_wave_track(yt, wave.seen_track_ids)
                if track is None:
                    continue
                all_tracks.append(track)
                total_track_count += 1

                track_id = track.item_id.split(RADIO_TRACK_ID_SEP, 1)[0]
                if first_track_id_this_batch is None:
                    first_track_id_this_batch = track_id

            if first_track_id_this_batch is not None:
                wave.last_track_id = first_track_id_this_batch
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

    def _get_user_wave_presets(self) -> list[dict[str, str]]:
        """
        Decode user-defined wave presets from the hidden JSON config key.

        Thin wrapper around :func:`presets.parse_stored_presets` so browse
        code and settings actions use the exact same parsing — avoids schema
        drift when preset fields are added or renamed.
        """
        return parse_stored_presets(self.config.get_value(CONF_WAVE_PRESETS_DATA))

    def _browse_user_presets_list(
        self, path: str, presets: list[dict[str, str]]
    ) -> list[BrowseFolder]:
        """
        Return one playable BrowseFolder per configured user preset.

        ``path`` is nested (``my_wave_presets/<idx>``) so MA's back-nav —
        which strips the last ``/``-segment — returns the user to the
        listing instead of the provider root. ``item_id`` uses the
        underscore form (``my_wave_presets_<idx>``) because MA rebuilds a
        playable folder's path from its item_id at play time. The browse
        dispatcher accepts both forms.

        :param path: Current browse path.
        :param presets: Sanitized presets from ``_get_user_wave_presets``.
        :return: List of playable BrowseFolder entries.
        """
        base = path if path.endswith("/") else f"{path}/"
        folders: list[BrowseFolder] = []
        for idx, preset in enumerate(presets):
            folders.append(
                BrowseFolder(
                    item_id=f"{MY_WAVE_PRESETS_FOLDER_ID}_{idx}",
                    provider=self.instance_id,
                    path=f"{base}{idx}",
                    name=preset.get("name", f"Preset {idx + 1}"),
                    is_playable=True,
                )
            )
        return folders

    def _browse_my_wave_modes_list(self, path: str) -> list[BrowseFolder]:
        """
        Return the 11 wave-mode entries as playable browse folders.

        Same dual-form contract as user presets: nested ``path`` keeps
        back-navigation intact, underscore ``item_id`` survives MA's
        play-time reconstruction.

        :param path: Browse path the user navigated into.
        :return: Ordered list of BrowseFolder entries, one per preset.
        """
        base = path if path.endswith("/") else f"{path}/"
        folders: list[BrowseFolder] = []
        for preset in WAVE_MODE_ORDER:
            name, translation_key = self._media_label(
                "folder", f"wave_mode_{preset}", preset.replace("_", " ").title()
            )
            folders.append(
                BrowseFolder(
                    item_id=f"{MY_WAVE_MODES_FOLDER_ID}_{preset}",
                    provider=self.instance_id,
                    path=f"{base}{preset}",
                    name=name,
                    translation_key=translation_key,
                    is_playable=True,
                )
            )
        return folders

    async def _browse_my_wave_mode(
        self, path: str, station_key: str, load_more: bool
    ) -> list[Track | BrowseFolder]:
        """
        Fetch a batch of tracks for a specific wave-mode preset.

        Reuses the session-API machinery: tracks live in
        ``_wave_states[station_key]`` where station_key is
        ``user:onyourwave#{preset}``. Tracks carry composite item_ids that
        route feedback back to this state.

        :param path: Full browse path to this preset.
        :param station_key: Station key with a ``#preset`` suffix.
        :param load_more: True when called for ``.../next`` pagination.
        :return: Tracks + optional "Load more" folder.
        """
        wave = self._get_wave_state(station_key)
        max_tracks_config = int(
            self.config.get_value(CONF_MY_WAVE_MAX_TRACKS) or 150  # type: ignore[arg-type]
        )
        batch_size_config = MY_WAVE_BATCH_SIZE
        effective_limit = min(
            BROWSE_INITIAL_TRACKS if not load_more else max_tracks_config,
            max_tracks_config,
        )
        max_batches = batch_size_config if not load_more else 1

        if not load_more:
            wave.seen_track_ids = set()

        all_tracks: list[Track | BrowseFolder] = []
        last_batch_id: str | None = None
        total_track_count = 0

        for _ in range(max_batches):
            if total_track_count >= effective_limit:
                break
            yandex_tracks, batch_id = await self._fetch_rotor_session_batch(wave, station_key)
            if batch_id:
                last_batch_id = batch_id
            if not wave.radio_started_sent and yandex_tracks:
                sent = await self._send_wave_feedback(wave, station_key, "radioStarted")
                if sent:
                    wave.radio_started_sent = True
            first_track_id_this_batch: str | None = None
            for yt in yandex_tracks:
                if total_track_count >= effective_limit:
                    break
                track = self._parse_my_wave_track(yt, wave.seen_track_ids, station_key=station_key)
                if track is None:
                    continue
                all_tracks.append(track)
                total_track_count += 1
                track_id = track.item_id.split(RADIO_TRACK_ID_SEP, 1)[0]
                if first_track_id_this_batch is None:
                    first_track_id_this_batch = track_id
            if first_track_id_this_batch is not None:
                wave.last_track_id = first_track_id_this_batch
            if (
                first_track_id_this_batch is None
                or not batch_id
                or not yandex_tracks
                or total_track_count >= effective_limit
            ):
                break

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

    def _parse_my_wave_track(
        self,
        yt: Any,
        seen_ids: set[str],
        *,
        station_key: str = ROTOR_STATION_MY_WAVE,
    ) -> Track | None:
        """
        Parse a Yandex track into a My Wave Track with composite item_id.

        Extracts the track_id, checks for duplicates in the seen_ids set,
        sets composite item_id (track_id@station_key) and updates
        provider_mappings. `station_key` is the key in `_wave_states` under
        which the matching session lives; for preset modes it carries a
        `#preset` suffix so `on_played`/`on_streamed` find the right session.

        Callers using shared state must hold the My Wave state lock.

        :param yt: Yandex track object from rotor station response.
        :param seen_ids: Set of already-seen track IDs to check and update.
        :param station_key: Station key to embed in the composite item_id.
            Defaults to the plain My Wave station.
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
        t.item_id = f"{track_id}{RADIO_TRACK_ID_SEP}{station_key}"
        for pm in t.provider_mappings:
            if pm.provider_instance == self.instance_id:
                pm.item_id = t.item_id
                break
        return t

    @use_cache(3600, allow_expired_cache=True)
    async def _get_valid_tags_for_category(self, category: str) -> list[str]:
        """
        Return tags for a category by combining hardcoded + landing-discovered.

        Trusts the hardcoded ``TAG_CATEGORY_*`` lists (evergreen Yandex
        categories) and the landing API output (Yandex returns landing tags
        only when they have playlists). No per-tag runtime validation: that
        machinery was a parallel ``asyncio.gather`` over
        ``get_tag_playlists`` for every tag and tripped Yandex's edge
        per-endpoint concurrency limit on first browse — captcha within
        ~460ms of the burst. If a tag turns out to be empty at click time,
        ``_get_tag_playlists_as_browse`` already renders an empty folder.

        :param category: Category name ('mood', 'activity', 'era', 'genres').
        :return: List of tag slugs (hardcoded order preserved, landing tags appended).
        """
        category_lists: dict[str, list[str]] = {
            "mood": list(TAG_CATEGORY_MOOD),
            "activity": list(TAG_CATEGORY_ACTIVITY),
            "era": list(TAG_CATEGORY_ERA),
            "genres": list(TAG_CATEGORY_GENRES),
        }
        tags = category_lists.get(category, [])
        try:
            landing_tags = await self.client.get_landing_tags()
            for slug, _title in landing_tags:
                cat = TAG_SLUG_CATEGORY.get(slug, "mood")
                if cat == category and slug not in tags:
                    tags.append(slug)
        except Exception as err:
            self.logger.debug("Landing tag discovery failed: %s", err)
        return tags

    @use_cache(3600, allow_expired_cache=True)
    async def _get_discovered_tags(self, locale: str) -> list[tuple[str, str, str | None]]:
        """
        Return all browse-able tags: hardcoded (non-seasonal) + landing-discovered.

        Same rationale as :meth:`_get_valid_tags_for_category` — runtime
        validation removed to avoid the per-endpoint concurrency burst that
        triggered Yandex captcha. The locale parameter is part of the cache
        key so locale changes invalidate the cached landing titles.

        :param locale: Current metadata locale (used as part of cache key).
        :return: List of (slug, English name, translation_key) tuples in
            hardcoded-then-discovered order. Landing-discovered tags carry their
            (already localized) API title and no translation_key.
        """
        all_tags: dict[str, tuple[str, str | None]] = {}
        for slug, cat in TAG_SLUG_CATEGORY.items():
            if cat != "seasonal":
                all_tags[slug] = self._media_label("folder", _media_label_key(slug), slug.title())
        try:
            landing_tags = await self.client.get_landing_tags()
            for slug, title in landing_tags:
                if slug not in all_tags:
                    all_tags[slug] = (title, None)
        except Exception as err:
            self.logger.debug("Failed to discover tags from landing API: %s", err)
        return [(slug, name, translation_key) for slug, (name, translation_key) in all_tags.items()]

    async def _get_discovered_tag_slugs(self) -> set[str]:
        """
        Get set of all valid tag slugs (cached).

        :return: Set of tag slug strings that have playlists.
        """
        discovered = await self._get_discovered_tags(self.mass.metadata.locale or "en_US")
        return {slug for slug, _name, _key in discovered}

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

        Child ``path`` is nested (``…/collection/tracks``) so MA's "back"
        button lands on this listing instead of the provider root. The
        dispatcher then strips the ``collection/`` prefix and hands off to
        core's default library handler.

        :param path: Full browse path.
        :return: List of library sub-folders.
        """
        base = path if path.endswith("/") else f"{path}/"

        folders: list[BrowseFolder] = []
        for feature, sub_id, label_key, is_playable in _COLLECTION_SUBFOLDERS:
            if feature not in self.supported_features:
                continue
            name, translation_key = self._media_label(
                "folder", label_key, label_key.replace("_", " ").title()
            )
            folders.append(
                BrowseFolder(
                    item_id=sub_id,
                    provider=self.instance_id,
                    path=f"{base}{sub_id}",
                    name=name,
                    translation_key=translation_key,
                    is_playable=is_playable,
                )
            )
        return folders

    async def _browse_pins(self) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse user's pinned items (artists/albums/playlists from Yandex Pins).

        Resolves each pin to its full media item via existing single-item lookups.
        Wave pins are skipped — MA has no native concept for them.

        :return: List of resolved media items.
        """
        pins_list = await self.client.get_pins()
        pins = getattr(pins_list, "pins", None) if pins_list else None
        if not pins:
            return []

        items: list[MediaItemType] = []
        for pin in pins:
            pin_type = getattr(pin, "type", None)
            data = getattr(pin, "data", None)
            if data is None:
                continue
            try:
                if pin_type == "artist_item" and getattr(data, "id", None) is not None:
                    items.append(await self.get_artist(str(data.id)))
                elif pin_type == "album_item" and getattr(data, "id", None) is not None:
                    items.append(await self.get_album(str(data.id)))
                elif pin_type == "playlist_item":
                    uid = getattr(data, "uid", None)
                    kind = getattr(data, "kind", None)
                    if uid is not None and kind is not None:
                        items.append(await self.get_playlist(f"{uid}:{kind}"))
            except (MediaNotFoundError, InvalidDataError) as err:
                self.logger.debug("Skipping pin %s: %s", pin_type, err)
        return items

    async def _browse_history(self) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse user's recent listening history (flattened across days).

        Collects ``track_id`` values from each history entry's ``item_id``
        sub-object (``full_model`` is not populated by the current API
        response — MarshalX exposes the IDs separately), dedupes, and
        batch-resolves them via ``get_tracks`` so the returned Track objects
        carry full artist/album/cover metadata.

        Entries without a resolvable ``track_id`` (e.g. album-only context
        rows) are skipped silently. Order is preserved — most recent first —
        by collecting unique IDs in response order into ``ordered_ids``,
        then rebuilding the final list by iterating ``ordered_ids`` and
        looking up each batch-fetched track in an id→track map.

        :return: List of recently played Track items.
        """
        history = await self.client.get_music_history()
        tabs = getattr(history, "history_tabs", None) if history else None
        if not tabs:
            return []

        seen_track_ids: set[str] = set()
        ordered_ids: list[str] = []
        for tab in tabs:
            for group in getattr(tab, "items", None) or []:
                for hist_item in getattr(group, "tracks", None) or []:
                    if getattr(hist_item, "type", None) != "track":
                        continue
                    item_id_obj = getattr(getattr(hist_item, "data", None), "item_id", None)
                    track_key: str | None = None
                    if isinstance(item_id_obj, dict):
                        track_key = item_id_obj.get("track_id") or item_id_obj.get("id")
                    else:
                        track_key = getattr(item_id_obj, "track_id", None) or getattr(
                            item_id_obj, "id", None
                        )
                    if not track_key:
                        continue
                    track_key = str(track_key)
                    if track_key in seen_track_ids:
                        continue
                    seen_track_ids.add(track_key)
                    ordered_ids.append(track_key)

        if not ordered_ids:
            return []

        try:
            fetched = await self.client.get_tracks(ordered_ids)
        except ResourceTemporarilyUnavailable as err:
            self.logger.warning("Failed to hydrate history tracks: %s", err)
            return []

        by_id = {str(t.id): t for t in fetched if getattr(t, "id", None) is not None}
        tracks: list[Track] = []
        for tid in ordered_ids:
            yt = by_id.get(tid)
            if yt is None:
                continue
            try:
                tracks.append(parse_track(self, yt))
            except InvalidDataError as err:
                self.logger.debug("Skipping history track %s: %s", tid, err)
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

        # Categorize valid tags, carrying each tag's (slug, English name, translation_key)
        categorized: dict[str, list[tuple[str, str, str | None]]] = {}
        for slug, name, translation_key in discovered:
            cat = TAG_SLUG_CATEGORY.get(slug, "mood")
            # Skip seasonal tags — they belong in mixes, not picks
            if cat == "seasonal":
                continue
            categorized.setdefault(cat, []).append((slug, name, translation_key))

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
            for slug, name, translation_key in category_tags:
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
            discovered_slugs = {slug for slug, _name, _key in discovered}
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

        Renders every seasonal tag from ``TAG_MIXES`` unconditionally. The
        old per-tag validation fired a ``Semaphore(5)+gather`` of
        ``get_tag_playlists`` calls and tripped Yandex's per-endpoint
        concurrency limit on first browse. If a season ends up empty at
        click time, ``_get_tag_playlists_as_browse`` already returns an
        empty folder.

        :param path: Full browse path.
        :param path_parts: Split path parts after ://.
        :return: List of folders or playlists.
        """
        base = path.rstrip("/") + "/"

        # mixes/ - show seasonal folders
        if len(path_parts) == 1:
            folders: list[BrowseFolder] = []
            for t in TAG_MIXES:
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

    async def _send_wave_feedback(
        self,
        wave: _WaveState,
        station_id: str,
        event_type: str,
        *,
        track_id: str | None = None,
        total_played_seconds: int | None = None,
    ) -> bool:
        """
        Route rotor feedback to the session endpoint.

        Requires an active ``wave.session_id`` — rotor feedback is only
        meaningful inside the session it originated from. The legacy
        stations-based endpoint (``/rotor/station/{id}/feedback``) is no
        longer reachable (returns 404 "not-found"), so when there's no
        session we skip silently rather than spamming the log.

        This happens when the track's composite item_id was parsed in a
        previous provider run (e.g. loaded from MA's library cache) and
        the corresponding session_id is not in memory any more. History
        reporting via ``play_audio`` still works in that case — only the
        rotor recommendation signal is lost.

        :param wave: Station state carrying session_id + batch_id.
        :param station_id: Rotor station ID (used only for logging here).
        :param event_type: Rotor event type (radioStarted, trackStarted, …).
        :param track_id: Yandex track ID the event refers to.
        :param total_played_seconds: Seconds played (trackFinished / skip only).
        :return: True if the feedback POST succeeded, False when skipped.
        """
        if not wave.session_id:
            self.logger.debug(
                "Skipping rotor feedback %s for %s: no active session",
                event_type,
                station_id,
            )
            return False
        return await self.client.rotor_session_feedback(
            wave.session_id,
            event_type,
            track_id=track_id,
            total_played_seconds=total_played_seconds,
            batch_id=wave.batch_id,
        )

    async def _prefetch_rotor_session(self, station_key: str) -> None:
        """
        Fire-and-forget: fetch the next batch for an active wave session.

        Called from ``on_played`` while a wave track starts playing, so by the
        time Music Assistant's DSTM asks for more via ``get_similar_tracks``,
        we already have Yandex-curated wave tracks sitting in
        ``wave.prefetched`` ready to serve (no extra round-trip).

        No-op when the station has no active session yet (prefetch cannot
        safely create one — that requires holding the lock across the
        network call and would stall readers), or when the buffer already
        has items (avoids burning rate limit).

        Three-phase lock discipline so the network round-trip does not
        block browse / drain paths that share the lock:

          1. Acquire, verify session + empty buffer, snapshot
             ``session_id`` and ``last_track_id``, release.
          2. Call ``client.rotor_session_tracks`` **directly** (no
             ``_fetch_rotor_session_batch``) — that helper mutates shared
             state (session creation, batch_id write) and would race with
             other callers now that we hold no lock. The raw client call
             only reads the arguments we pass in.
          3. Re-acquire, verify the session hasn't been recycled and the
             buffer is still empty, then ``extend``.

        :param station_key: Station key whose state to top up.
        """
        wave = self._wave_states.get(station_key)
        if wave is None:
            return

        async with wave.lock:
            if wave.session_id is None or wave.prefetched:
                return
            session_id = wave.session_id
            cursor = wave.last_track_id

        if not cursor:
            return  # No anchor for the next batch yet; try again later.

        tracks, _ = await self.client.rotor_session_tracks(session_id, current_track_id=str(cursor))
        if not tracks:
            return

        async with wave.lock:
            # Another task could have restarted the session or filled the
            # buffer while we were awaiting the network call; bail in both
            # cases to avoid stale extends.
            if wave.session_id != session_id or wave.prefetched:
                return
            wave.prefetched.extend(tracks)

    async def _fetch_rotor_session_batch(
        self, wave: _WaveState, station_id: str
    ) -> tuple[list[YandexTrack], str | None]:
        """
        Fetch the next rotor-session batch for any station.

        On first call (wave.session_id is None), starts a new rotor session
        and records session_id + batch_id on the wave state. On subsequent
        calls, paginates via rotor_session_tracks using wave.last_track_id.

        If station_id carries a wave-mode suffix (e.g. "user:onyourwave#discover"),
        the suffix maps to a preset in WAVE_MODE_PRESETS and its settings are
        merged with wave.settings (wave.settings wins on key conflict). The
        base station ID (before "#") is what actually goes to Yandex.

        :param wave: The _WaveState for this station (persists across calls).
        :param station_id: Rotor station key (may include a "#preset" suffix).
        :return: Tuple of (list of yandex tracks, batch_id or None).
        """
        # Session-creation path: no session yet, or we have a session but no
        # cursor yet (`tracks` with an empty queue returns a hard-to-debug
        # empty batch — starting a fresh session is the same latency but
        # actually yields tracks).
        if wave.session_id is None or not wave.last_track_id:
            base_station, preset_settings = _split_wave_mode(station_id)
            merged = {**preset_settings, **wave.settings}
            session_id, tracks, batch_id = await self.client.rotor_session_new(
                base_station, settings=merged or None
            )
            if session_id:
                wave.session_id = session_id
        else:
            tracks, batch_id = await self.client.rotor_session_tracks(
                wave.session_id, current_track_id=str(wave.last_track_id)
            )
        if batch_id:
            wave.batch_id = batch_id
        return (tracks, batch_id)

    async def _browse_waves(
        self, path: str, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse waves folder (rotor stations by genre/mood/activity/epoch/local).

        Fetches available stations from the Yandex rotor API and groups them by category.

        :param path: Full browse path.
        :param path_parts: Split path parts after ://.
        :return: List of folders or tracks.
        """
        base = path.rstrip("/") + "/"

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
            # Personalized "My Waves" first — only show if dashboard returns stations
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
            # Featured Waves — only show if landing-blocks/waves returns data
            waves_landing = await self._get_waves_landing_cached()
            if waves_landing:
                name, translation_key = self._media_label(
                    "folder", WAVES_LANDING_FOLDER_ID, "Featured Waves"
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

    @use_cache(600, allow_expired_cache=True)
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
                "Browse wave station: station_id=%s path=%s last_track_id=%s session=%s",
                station_id,
                path,
                state.last_track_id,
                state.session_id,
            )
            # Tagged stations (genre:*, mood:*, activity:*, epoch:*) accept the
            # same /rotor/session/* endpoint as user:onyourwave / track:{id},
            # verified against the live Yandex API. Reuse the session helper so
            # batch_id + session_id stay anchored across browse/play/feedback.
            yandex_tracks, _ = await self._fetch_rotor_session_batch(state, station_id)

            if not state.radio_started_sent and yandex_tracks:
                sent = await self._send_wave_feedback(state, station_id, "radioStarted")
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

        Accepts both camelCase (``compactImageUrl`` — what /landing-blocks/
        actually returns) and snake_case (``compact_image_url`` — retained
        for safety if MarshalX ever normalises the payload).

        :param item: Wave or mix item dict from the API.
        :return: (cover_uri, bg_color) tuple where bg_color is a hex string or None.
        """
        agent_uri = item.get("agent", {}).get("cover", {}).get("uri", "")
        cover_uri = agent_uri or item.get("compactImageUrl") or item.get("compact_image_url")
        bg_color = item.get("colors", {}).get("average")
        return cover_uri, bg_color

    @use_cache(3600, allow_expired_cache=True)
    async def _get_mixes_waves_cached(self) -> list[dict[str, Any]] | None:
        """
        Get AI Wave Set data from /landing-blocks/mixes-waves, cached for 1 hour.

        :return: List of mix category dicts from the API, or None on error.
        """
        return await self.client.get_mixes_waves()

    @use_cache(3600, allow_expired_cache=True)
    async def _get_waves_landing_cached(self) -> list[dict[str, Any]] | None:
        """
        Get Featured Waves data from /landing-blocks/waves, cached for 1 hour.

        :return: List of wave category dicts from the API, or None on error.
        """
        return await self.client.get_waves_landing()

    async def _browse_waves_landing(
        self, path: str, path_parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse Featured Waves (from /landing-blocks/waves).

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
                    # API returns camelCase (`stationId`); keep snake_case as a
                    # safety net if the payload is ever normalised upstream.
                    station_id = item.get("stationId") or item.get("station_id") or ""
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
        Browse AI Wave Sets (from /landing-blocks/mixes-waves).

        :param path: Full browse path.
        :param path_parts: Split path parts after ://.
        :return: List of folders or tracks.
        """
        mixes_data = await self._get_mixes_waves_cached()
        return await self._browse_wave_categories(
            path, path_parts, mixes_data or [], MY_WAVES_SET_FOLDER_ID
        )

    @use_cache(600, allow_expired_cache=True)
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

    @use_cache(3600 * 24, allow_expired_cache=True)
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """
        Perform search on Yandex Music.

        :param search_query: The search query.
        :param media_types: List of media types to search for.
        :param limit: Maximum number of results per type.
        :return: SearchResults with found items.
        """
        result = SearchResults()

        # Determine search type based on requested media types
        # Map MediaType to Yandex API search type. AUDIOBOOK has no dedicated
        # Yandex type — it maps to "album" and is filtered by classify_album below.
        type_mapping = {
            MediaType.TRACK: "track",
            MediaType.ALBUM: "album",
            MediaType.AUDIOBOOK: "album",
            MediaType.ARTIST: "artist",
            MediaType.PLAYLIST: "playlist",
            MediaType.PODCAST: "podcast",
        }
        requested_types = list(
            dict.fromkeys(type_mapping[mt] for mt in media_types if mt in type_mapping)
        )

        # Use specific type if only one requested, otherwise search all
        search_type = requested_types[0] if len(requested_types) == 1 else "all"

        search_result = await self.client.search(search_query, search_type=search_type)
        if not search_result:
            return result

        # Parse tracks
        if MediaType.TRACK in media_types and search_result.tracks:
            for track in search_result.tracks.results[:limit]:
                try:
                    result.tracks = [*result.tracks, parse_track(self, track)]
                except InvalidDataError as err:
                    self.logger.debug("Error parsing track: %s", err)

        # Parse albums — audiobooks are split into the audiobooks bucket via
        # classify_album. Yandex-returned podcast albums are handled separately
        # through the dedicated `.podcasts` node below. ``limit`` is applied per
        # bucket AFTER classification — slicing first would drop audiobooks when
        # the first ``limit`` results happen to be music albums (or vice versa).
        want_album = MediaType.ALBUM in media_types
        want_audiobook = MediaType.AUDIOBOOK in media_types
        if (want_album or want_audiobook) and search_result.albums:
            album_count = 0
            audiobook_count = 0
            for album in search_result.albums.results:
                album_full = not want_album or album_count >= limit
                audiobook_full = not want_audiobook or audiobook_count >= limit
                if album_full and audiobook_full:
                    break
                kind = classify_album(album)
                try:
                    if kind == "audiobook" and want_audiobook and not audiobook_full:
                        result.audiobooks = [
                            *result.audiobooks,
                            parse_audiobook(self, album),
                        ]
                        audiobook_count += 1
                    elif kind == "music" and want_album and not album_full:
                        result.albums = [*result.albums, parse_album(self, album)]
                        album_count += 1
                except InvalidDataError as err:
                    self.logger.debug("Error parsing %s album: %s", kind, err)

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

        # Parse podcasts (Yandex returns them as albums under .podcasts)
        podcasts_node = getattr(search_result, "podcasts", None)
        if MediaType.PODCAST in media_types and podcasts_node:
            for album in podcasts_node.results[:limit]:
                try:
                    result.podcasts = [*result.podcasts, parse_podcast(self, album)]
                except InvalidDataError as err:
                    self.logger.debug("Error parsing podcast: %s", err)

        return result

    # Get single items

    @use_cache(3600 * 24 * 30, allow_expired_cache=True)
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

    @use_cache(3600 * 24 * 30, allow_expired_cache=True)
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

    @use_cache(3600 * 24, allow_expired_cache=True)
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """
        Get podcast details by ID (backed by a Yandex album).

        :param prov_podcast_id: The provider podcast (album) ID.
        :return: Podcast object.
        :raises MediaNotFoundError: If not found.
        """
        album = await self.client.get_album(prov_podcast_id)
        if not album:
            raise MediaNotFoundError(f"Podcast {prov_podcast_id} not found")
        return parse_podcast(self, album)

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Iterate podcast episodes for a given podcast (album) ID."""
        album = await self.client.get_album_with_tracks(prov_podcast_id)
        if not album:
            raise MediaNotFoundError(f"Podcast {prov_podcast_id} not found")
        podcast = parse_podcast(self, album)
        position = 1
        for disc in album.volumes or []:
            for track_obj in disc:
                try:
                    yield parse_podcast_episode(self, track_obj, podcast, position=position)
                except InvalidDataError as err:
                    self.logger.debug("Error parsing podcast episode: %s", err)
                position += 1

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """
        Get a single podcast episode by ID.

        The parent Podcast is reconstructed from the track's parent album. If
        the album isn't present on the track, the episode cannot be converted
        into a valid MA model and InvalidDataError is raised.
        """
        tracks = await self.client.get_tracks([prov_episode_id])
        if not tracks:
            raise MediaNotFoundError(f"Podcast episode {prov_episode_id} not found")
        track_obj = tracks[0]
        if not track_obj.albums:
            raise InvalidDataError(
                f"Podcast episode {prov_episode_id} is missing parent podcast album data"
            )
        podcast = parse_podcast(self, track_obj.albums[0])
        return parse_podcast_episode(self, track_obj, podcast, position=0)

    @use_cache(3600 * 24, allow_expired_cache=True)
    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """
        Get audiobook details by ID, including chapters built from tracks.

        :param prov_audiobook_id: The provider audiobook (album) ID.
        :return: Audiobook object.
        :raises MediaNotFoundError: If not found.
        """
        album = await self.client.get_album_with_tracks(prov_audiobook_id)
        if not album:
            raise MediaNotFoundError(f"Audiobook {prov_audiobook_id} not found")
        audiobook = parse_audiobook(self, album)

        chapters: list[MediaItemChapter] = []
        start = 0.0
        pos = 1
        for disc in album.volumes or []:
            for track_obj in disc:
                dur_s = (track_obj.duration_ms or 0) / 1000.0
                chapters.append(
                    MediaItemChapter(
                        position=pos,
                        name=track_obj.title or f"Chapter {pos}",
                        start=start,
                        end=start + dur_s,
                    )
                )
                start += dur_s
                pos += 1
        audiobook.metadata.chapters = chapters
        audiobook.duration = int(start)
        return audiobook

    async def get_track(self, prov_track_id: str) -> Track:
        """
        Get track details by ID.

        Supports composite item_id (track_id@station_id) for My Wave tracks;
        only the track_id part is used for the API. Normalizes the ID before
        caching to avoid duplicate cache entries.

        :param prov_track_id: The provider track ID (or track_id@station_id).
        :return: Track object.
        :raises MediaNotFoundError: If track not found.
        """
        track_id, _ = _parse_radio_item_id(prov_track_id)
        return await self._get_track_cached(track_id)

    @use_cache(3600 * 24 * 30, allow_expired_cache=True)
    async def _get_track_cached(self, track_id: str) -> Track:
        """
        Get track details by normalized ID (cached).

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
        """
        Get playlist details by ID.

        Supports virtual playlists MY_WAVE_PLAYLIST_ID (My Wave) and
        LIKED_TRACKS_PLAYLIST_ID (Liked Tracks). Real playlists use format "owner_id:kind".

        :param prov_playlist_id: The provider playlist ID (format: "owner_id:kind",
            my_wave, or liked_tracks).
        :return: Playlist object.
        :raises MediaNotFoundError: If playlist not found.
        """
        # Virtual playlists - constructed locally (no API call); translation_key localizes
        # the name for the connection locale at serialization.
        if prov_playlist_id == MY_WAVE_PLAYLIST_ID:
            return Playlist(
                item_id=MY_WAVE_PLAYLIST_ID,
                provider=self.instance_id,
                name="My Wave",
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

    @use_cache(3600 * 24 * 30, allow_expired_cache=True)
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
        Get My Wave tracks for virtual playlist (uncached; uses cursor for page > 0).

        Fetches MY_WAVE_BATCH_SIZE Rotor API batches per page call to reduce
        the number of round-trips when the player controller paginates through pages.

        :param page: Page number (0 = first batch, 1+ = next batches via queue cursor).
        :return: List of Track objects for this page.
        """
        wave = self._get_wave_state(ROTOR_STATION_MY_WAVE)
        async with wave.lock:
            max_tracks_config = int(
                self.config.get_value(CONF_MY_WAVE_MAX_TRACKS) or 150  # type: ignore[arg-type]
            )

            # Reset seen tracks on first page
            if page == 0:
                wave.seen_track_ids = set()

            queue: str | int | None = None
            if page > 0:
                queue = wave.playlist_next_cursor
                if not queue:
                    return []

            # Check if we've already reached the limit
            if len(wave.seen_track_ids) >= max_tracks_config:
                return []

            tracks: list[Track] = []
            next_cursor: str | None = None

            # Fetch MY_WAVE_BATCH_SIZE Rotor API batches per page to reduce API round-trips
            for _ in range(MY_WAVE_BATCH_SIZE):
                if len(wave.seen_track_ids) >= max_tracks_config:
                    break

                if queue is not None:
                    wave.last_track_id = str(queue)
                yandex_tracks, _ = await self._fetch_rotor_session_batch(
                    wave, ROTOR_STATION_MY_WAVE
                )
                if not wave.radio_started_sent and yandex_tracks:
                    sent = await self._send_wave_feedback(
                        wave, ROTOR_STATION_MY_WAVE, "radioStarted"
                    )
                    if sent:
                        wave.radio_started_sent = True

                if not yandex_tracks:
                    break

                first_track_id_this_batch = None
                for yt in yandex_tracks:
                    if len(wave.seen_track_ids) >= max_tracks_config:
                        break

                    track = self._parse_my_wave_track(yt, wave.seen_track_ids)
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
            wave.playlist_next_cursor = next_cursor
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
            self.config.get_value(CONF_LIKED_TRACKS_MAX_TRACKS) or 200  # type: ignore[arg-type]
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
            # Spread bursts: insert a small jittered pause between batches so
            # a 500-track hydration doesn't look like a bot to Yandex's
            # smart-captcha. Skipped after the last batch.
            if i + batch_size < len(track_ids):
                await asyncio.sleep(
                    LIKED_BATCH_JITTER_MIN_S + random.random() * LIKED_BATCH_JITTER_SPAN_S
                )

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

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """
        Get similar tracks, preferring pre-fetched wave tracks when available.

        Split in two paths with different caching policies:

        - **Wave-drain path** (the seed carries a station suffix and
          ``wave.prefetched`` is non-empty). Uncached by design: it mutates
          state, a cache hit would replay the same drained tracks forever and
          the prefetch buffer would never advance.
        - **Fallback path** (plain track_id, no active wave, or empty buffer).
          Creates a per-seed rotor session under ``track:{id}`` and is cached
          for 3 hours — this is pure and safe to memoise.

        :param prov_track_id: Provider track ID (plain or track_id@station_id).
        :param limit: Maximum number of tracks to return.
        :return: List of similar Track objects.
        """
        track_id, station_key = _parse_radio_item_id(prov_track_id)

        if station_key:
            drained = await self._drain_prefetched_wave_tracks(station_key, limit)
            if drained:
                return drained

        return await self._fetch_similar_tracks_for_seed(track_id, limit)

    async def _drain_prefetched_wave_tracks(self, station_key: str, limit: int) -> list[Track]:
        """
        Pop up to ``limit`` prefetched tracks off the wave state.

        Runs under ``wave.lock`` so it doesn't race with
        ``_prefetch_rotor_session`` which extends the same list under the
        same lock. Returns an empty list when there's no active session or
        nothing prefetched; callers then fall through to the cached fetch.

        This method is intentionally not cached — it mutates wave state.
        """
        wave = self._wave_states.get(station_key)
        if not (wave and wave.session_id and wave.prefetched):
            return []
        async with wave.lock:
            if not wave.prefetched:
                return []
            drained_yt = wave.prefetched[:limit]
            wave.prefetched = wave.prefetched[limit:]
        tracks: list[Track] = []
        for yt in drained_yt:
            try:
                tracks.append(parse_track(self, yt))
            except InvalidDataError as err:
                self.logger.debug("Error parsing prefetched wave track: %s", err)
        return tracks

    @use_cache(3600 * 3, allow_expired_cache=True)
    async def _fetch_similar_tracks_for_seed(self, track_id: str, limit: int) -> list[Track]:
        """
        Create a one-off rotor session for ``track:{id}`` and return up to ``limit`` tracks.

        Stateless by design: similar-tracks results don't participate in
        playback feedback or prefetch, so there is no need to keep a
        ``_WaveState`` entry around. Going through ``_fetch_rotor_session_batch``
        would create one per unique seed and grow ``_wave_states`` without
        bound under normal DSTM usage; call ``rotor_session_new`` directly
        instead.

        Pure function of ``track_id`` / ``limit``, hence safe to memoise
        via ``@use_cache``.
        """
        _, yandex_tracks, _ = await self.client.rotor_session_new(f"track:{track_id}")
        similar_tracks: list[Track] = []
        for yt in yandex_tracks[:limit]:
            try:
                similar_tracks.append(parse_track(self, yt))
            except InvalidDataError as err:
                self.logger.debug("Error parsing similar track: %s", err)
        return similar_tracks

    @use_cache(3600 * 3, allow_expired_cache=True)
    async def get_similar_artists(self, prov_artist_id: str, limit: int = 25) -> list[Artist]:
        """
        Get artists similar to the given one via Yandex artists/similar endpoint.

        :param prov_artist_id: Provider artist ID.
        :param limit: Maximum number of artists to return.
        :return: List of similar Artist objects.
        """
        yandex_artists = await self.client.get_similar_artists(prov_artist_id, limit=limit)
        artists: list[Artist] = []
        for ya in yandex_artists:
            try:
                artists.append(parse_artist(self, ya))
            except InvalidDataError as err:
                self.logger.debug("Error parsing similar artist: %s", err)
        return artists

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """
        Get the available recommendation rows, without items.

        Returns My Wave, Made for You, Chart, New Releases, New Playlists,
        Top Picks, Mood Mix, Activity Mix and Seasonal Mix rows.
        """
        # The seasonal row title carries the current season, derived locally from the month.
        seasonal_tag = TAG_SEASONAL_MAP.get(utc().month, "autumn")
        seasonal_name, _ = self._media_label(
            "folder", _media_label_key(seasonal_tag), seasonal_tag.title()
        )
        return [
            RecommendationFolder(
                item_id=MY_WAVE_PLAYLIST_ID,
                provider=self.instance_id,
                name="My Wave",
                translation_key=MY_WAVE_PLAYLIST_ID,
                icon="mdi-waveform",
            ),
            RecommendationFolder(
                item_id="feed",
                provider=self.instance_id,
                name="Made for You",
                translation_key="feed",
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

    @use_cache(600, allow_expired_cache=True)
    async def _get_my_wave_recommendations(self) -> RecommendationFolder | None:
        """
        Get My Wave recommendation folder with personalized tracks.

        Shares the same `_WaveState(ROTOR_STATION_MY_WAVE)` with browse and
        virtual-playlist flows, so session_id + batch_id established here
        carry into `on_played`/`on_streamed` feedback even when the user
        starts playback from this discovery card.

        :return: RecommendationFolder with My Wave tracks, or None if empty.
        """
        max_tracks_config = int(
            self.config.get_value(CONF_MY_WAVE_MAX_TRACKS) or 150  # type: ignore[arg-type]
        )
        batch_size_config = MY_WAVE_BATCH_SIZE

        wave = self._get_wave_state(ROTOR_STATION_MY_WAVE)
        # Local dedup so the recommendations card stays independent from the
        # browse/virtual-playlist dedup set (which may be larger and stale).
        # Only session_id + batch_id + last_track_id are shared with `wave`.
        seen_track_ids: set[str] = set()
        items: list[Track] = []

        # Hold the wave lock across the whole fetch chain — we mutate shared
        # session_id/batch_id/last_track_id via _fetch_rotor_session_batch,
        # and other call sites (browse, virtual-playlist) guard the same
        # state with this lock. Concurrent calls without the lock would
        # interleave cursor updates and leave the session inconsistent.
        async with wave.lock:
            for _ in range(batch_size_config):
                if len(seen_track_ids) >= max_tracks_config:
                    break

                yandex_tracks, _ = await self._fetch_rotor_session_batch(
                    wave, ROTOR_STATION_MY_WAVE
                )
                if not yandex_tracks:
                    break

                first_track_id_this_batch: str | None = None
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

                if first_track_id_this_batch is None:
                    break
                wave.last_track_id = first_track_id_this_batch

        if not items:
            return None

        initial_tracks_limit = DISCOVERY_INITIAL_TRACKS
        if len(items) > initial_tracks_limit:
            items = items[:initial_tracks_limit]

        return RecommendationFolder(
            item_id=MY_WAVE_PLAYLIST_ID,
            provider=self.instance_id,
            name="My Wave",
            translation_key=MY_WAVE_PLAYLIST_ID,
            items=UniqueList(items),
            icon="mdi-waveform",
        )

    @use_cache(1800, allow_expired_cache=True)
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
                    # Mark feed-generated playlists (Playlist of the Day, DejaVu,
                    # Premiere, Missed Likes) as dynamic — Yandex regenerates them
                    # on a schedule so MA must not long-cache the track list.
                    items.append(parse_playlist(self, gen_playlist.data, is_dynamic=True))
                except InvalidDataError as err:
                    self.logger.debug("Error parsing feed playlist: %s", err)
        if not items:
            return None
        return RecommendationFolder(
            item_id="feed",
            provider=self.instance_id,
            name="Made for You",
            translation_key="feed",
            items=UniqueList(items),
            icon="mdi-account-music",
        )

    @use_cache(3600, allow_expired_cache=True)
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

    @use_cache(3600, allow_expired_cache=True)
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

    @use_cache(3600, allow_expired_cache=True)
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

    @use_cache(3600, allow_expired_cache=True)
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

    @use_cache(1800, allow_expired_cache=True)
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
        tag_name, _ = self._media_label("folder", _media_label_key(mood_tag), mood_tag.title())
        return RecommendationFolder(
            item_id="mood_mix",
            provider=self.instance_id,
            name=f"Mood Mix: {tag_name}",
            translation_key="mood_mix",
            translation_params=[tag_name],
            items=UniqueList(items),
            icon="mdi-emoticon-outline",
        )

    @use_cache(1800, allow_expired_cache=True)
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
        tag_name, _ = self._media_label(
            "folder", _media_label_key(activity_tag), activity_tag.title()
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

    @use_cache(3600 * 6, allow_expired_cache=True)
    async def _get_seasonal_mix_recommendations(self) -> RecommendationFolder | None:
        """
        Get Seasonal Mix recommendation folder (based on current month).

        :return: RecommendationFolder with seasonal playlists, or None if unavailable.
        """
        # Determine current season tag; fall back to autumn if the seasonal
        # endpoint returns nothing (e.g. spring/autumn handover gap).
        current_month = utc().month
        seasonal_tag = TAG_SEASONAL_MAP.get(current_month, "autumn")
        playlists = await self.client.get_tag_playlists(seasonal_tag)
        if not playlists and seasonal_tag != "autumn":
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
        tag_name, _ = self._media_label(
            "folder", _media_label_key(seasonal_tag), seasonal_tag.title()
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
                # Skip this batch but keep going — the terminal guard below
                # raises if every batch comes back empty. Aborting on a single
                # empty batch threw away tracks already fetched from earlier
                # batches and forced a full retry hours later (under the
                # @use_cache TTL above).
                self.logger.warning(
                    "Empty batch %s-%s for playlist %s, skipping",
                    i,
                    i + len(batch) - 1,
                    prov_playlist_id,
                )
                continue
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
        """Retrieve library artists from Yandex Music."""
        artists = await self.client.get_liked_artists()
        for artist in artists:
            try:
                yield parse_artist(self, artist)
            except InvalidDataError as err:
                self.logger.debug("Error parsing library artist: %s", err)

    async def _get_liked_albums_cached(self, ttl: float = 30.0) -> list[YandexAlbum]:
        """
        Return liked albums with a short in-process TTL cache + lock.

        Albums, podcasts and audiobooks are all derived from the same
        ``users/{uid}/likes/albums`` endpoint, so a full library sync would
        otherwise trigger three sequential (or concurrent) identical calls.
        The lock serializes refreshes so only one request hits the API when
        multiple library syncs start together.
        """
        async with self._liked_albums_lock:
            now = asyncio.get_running_loop().time()
            if self._liked_albums_cache is not None:
                cached_at, cached = self._liked_albums_cache
                if now - cached_at < ttl:
                    return cached
            albums = await self.client.get_liked_albums(batch_size=TRACK_BATCH_SIZE)
            self._liked_albums_cache = (now, albums)
            return albums

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """
        Retrieve library albums from Yandex Music.

        Excludes entries classified as podcasts or audiobooks so they don't
        duplicate into the Albums library view.
        """
        for album in await self._get_liked_albums_cached():
            if classify_album(album) != "music":
                continue
            try:
                yield parse_album(self, album)
            except InvalidDataError as err:
                self.logger.debug("Error parsing library album: %s", err)

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Retrieve library podcasts from Yandex Music (filtered liked albums)."""
        for album in await self._get_liked_albums_cached():
            if classify_album(album) != "podcast":
                continue
            try:
                yield parse_podcast(self, album)
            except InvalidDataError as err:
                self.logger.debug("Error parsing library podcast: %s", err)

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """Retrieve library audiobooks from Yandex Music (filtered liked albums)."""
        for album in await self._get_liked_albums_cached():
            if classify_album(album) != "audiobook":
                continue
            try:
                yield parse_audiobook(self, album)
            except InvalidDataError as err:
                self.logger.debug("Error parsing library audiobook: %s", err)

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
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

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """
        Retrieve library playlists from Yandex Music.

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
        """
        Add item to library.

        For tracks carrying a wave station context in the item_id (e.g. when
        the user adds a My Wave track to favourites during playback), also
        fires a rotor ``like`` feedback on the active session so the wave
        algorithm biases toward similar tracks immediately.

        :param item: The media item to add.
        :return: True if successful.
        """
        prov_item_id = self._get_provider_item_id(item)
        if not prov_item_id:
            return False
        track_id, station_key = _parse_radio_item_id(prov_item_id)

        if item.media_type == MediaType.TRACK:
            ok = await self.client.like_track(track_id)
            if ok and station_key:
                wave = self._wave_states.get(station_key)
                if wave and wave.session_id:
                    await self._send_wave_feedback(wave, station_key, "like", track_id=track_id)
            return ok
        if item.media_type in (MediaType.ALBUM, MediaType.PODCAST, MediaType.AUDIOBOOK):
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
        if media_type in (MediaType.ALBUM, MediaType.PODCAST, MediaType.AUDIOBOOK):
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
        Get stream details for a track, podcast episode, or audiobook.

        A podcast episode is a track underneath the Yandex API, so it flows
        through the same per-track streaming path. An audiobook is an album
        with multiple tracks (chapters) — returned as a CUSTOM stream whose
        generator concatenates each chapter's bytes in order.

        :param item_id: The track / episode ID (or track_id@station_id for My Wave),
            or the audiobook (album) ID when ``media_type`` is AUDIOBOOK.
        :param media_type: The media type.
        :return: StreamDetails for the item.
        """
        if media_type == MediaType.AUDIOBOOK:
            return await self._get_audiobook_stream_details(item_id)
        return await self.streaming.get_stream_details(item_id)

    async def _get_audiobook_stream_details(self, audiobook_id: str) -> StreamDetails:
        """
        Build StreamDetails for an audiobook as a chapter-concatenated CUSTOM stream.

        Loads the album's tracks, uses the first chapter to establish the audio
        format, and stores the per-chapter track-IDs + durations in ``data`` so
        ``get_audio_stream`` can iterate them. ``can_seek=True`` so MA routes
        ``seek_position`` into ``get_audio_stream``, where the provider translates
        it into ``(start_chapter, in_chapter_offset)``. In-chapter precision
        requires a byte-seekable chapter codec (raw MP3); otherwise the chapter
        is restarted from its beginning.
        """
        album = await self.client.get_album_with_tracks(audiobook_id)
        if not album or not (album.volumes or []):
            raise MediaNotFoundError(f"Audiobook {audiobook_id} has no chapters")

        chapter_ids, chapter_durations_ms = _extract_chapter_map_from_album(album)
        if not chapter_ids:
            raise MediaNotFoundError(f"Audiobook {audiobook_id} has no chapters")

        self._audiobook_chapter_cache[audiobook_id] = (chapter_ids, chapter_durations_ms)

        # Resolve first-chapter format so MA/ffmpeg know what it's decoding
        first = await self.streaming.get_stream_details(chapter_ids[0])
        total_duration = sum(chapter_durations_ms) // 1000

        return StreamDetails(
            item_id=audiobook_id,
            provider=self.instance_id,
            media_type=MediaType.AUDIOBOOK,
            audio_format=first.audio_format,
            stream_type=StreamType.CUSTOM,
            duration=total_duration,
            data={
                "chapter_ids": chapter_ids,
                "chapter_durations_ms": chapter_durations_ms,
            },
            can_seek=True,
            allow_seek=True,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Return the audio stream for the provider item.

        For tracks and podcast episodes, streams via windowed Range requests
        (raw or AES-CTR encrypted). For audiobooks, iterates chapters: each
        chapter's bytes are streamed through the per-track path and concatenated.

        :param streamdetails: Stream details with URL and optional decryption key.
        :param seek_position: Seek position in seconds (handled by provider for raw transport).
        :return: Async generator yielding audio chunks.
        """
        data = streamdetails.data if isinstance(streamdetails.data, dict) else None
        if streamdetails.media_type == MediaType.AUDIOBOOK and data and "chapter_ids" in data:
            async for chunk in self._stream_audiobook_chapters(data, seek_position):
                yield chunk
            return
        async for chunk in self.streaming.get_audio_stream(streamdetails, seek_position):
            yield chunk

    def _resolve_audiobook_seek(
        self, chapter_durations_ms: list[int], seek_position: int, n_chapters: int
    ) -> tuple[int, int]:
        """Map an audiobook ``seek_position`` (seconds) to (start_idx, chapter_seek)."""
        if seek_position <= 0 or not chapter_durations_ms:
            return 0, 0
        accumulated_ms = 0
        seek_ms = seek_position * 1000
        for idx, dur_ms in enumerate(chapter_durations_ms):
            if accumulated_ms + dur_ms > seek_ms:
                return idx, (seek_ms - accumulated_ms) // 1000
            accumulated_ms += dur_ms
        # Seek past end — start at last chapter from 0
        return max(n_chapters - 1, 0), 0

    async def _resolve_audiobook_chapter_map(
        self, audiobook_id: str
    ) -> tuple[list[str], list[int]]:
        """
        Return (chapter_track_ids, chapter_durations_ms) for an audiobook.

        Served from an in-memory cache populated by ``_get_audiobook_stream_details``.
        On a miss (e.g. ``on_played`` fires before streaming has started), falls back
        to a fresh ``get_album_with_tracks`` call and refills the cache.
        """
        cached = self._audiobook_chapter_cache.get(audiobook_id)
        if cached is not None:
            return cached
        album = await self.client.get_album_with_tracks(audiobook_id)
        if not album or not (album.volumes or []):
            return [], []
        chapter_ids, chapter_durations_ms = _extract_chapter_map_from_album(album)
        self._audiobook_chapter_cache[audiobook_id] = (chapter_ids, chapter_durations_ms)
        return chapter_ids, chapter_durations_ms

    async def _stream_audiobook_chapters(
        self, data: dict[str, Any], seek_position: int
    ) -> AsyncGenerator[bytes]:
        """
        Concatenate per-chapter streams of an audiobook.

        Translates ``seek_position`` into (start_chapter, in_chapter_offset) and
        delegates each chapter to the per-track streaming path. In-chapter offset
        is only applied when the chapter codec is byte-seekable (``can_seek``);
        otherwise the chapter is restarted from its beginning. Tracks consecutive
        chapter failures and raises ``MediaNotFoundError`` once the threshold is
        exceeded, so playback never silently truncates.
        """
        chapter_ids: list[str] = list(data.get("chapter_ids") or [])
        chapter_durations_ms: list[int] = list(data.get("chapter_durations_ms") or [])
        if not chapter_ids:
            return

        start_idx, chapter_seek = self._resolve_audiobook_seek(
            chapter_durations_ms, seek_position, len(chapter_ids)
        )

        max_consecutive_failures = 3
        consecutive_failures = 0
        has_yielded_audio = False
        last_error: Exception | None = None

        for idx in range(start_idx, len(chapter_ids)):
            chapter_id = chapter_ids[idx]
            requested_offset = chapter_seek if idx == start_idx else 0
            chapter_details: StreamDetails | None = None
            try:
                chapter_details = await self.streaming.get_stream_details(chapter_id)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                last_error = err
                self.logger.warning(
                    "Audiobook chapter %d (%s) stream-details failed: %s",
                    idx + 1,
                    chapter_id,
                    err,
                )

            if chapter_details is None:
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    raise MediaNotFoundError(
                        "Unable to stream audiobook: too many consecutive chapter failures"
                    ) from last_error
                continue

            # Apply the in-chapter offset only when the chapter codec supports
            # byte-offset seeking; otherwise restart the chapter from 0 to avoid
            # decoding garbled bytes from mid-file of a container format.
            offset = requested_offset if chapter_details.can_seek else 0
            chapter_had_audio = False
            try:
                async for chunk in self.streaming.get_audio_stream(chapter_details, offset):
                    chapter_had_audio = True
                    has_yielded_audio = True
                    yield chunk
            except asyncio.CancelledError:
                raise
            except Exception as err:
                last_error = err
                self.logger.warning(
                    "Audiobook chapter %d (%s) stream failed mid-play: %s",
                    idx + 1,
                    chapter_id,
                    err,
                )

            if chapter_had_audio:
                consecutive_failures = 0
                last_error = None
            else:
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    raise MediaNotFoundError(
                        "Unable to stream audiobook: too many consecutive chapter failures"
                    ) from last_error

        if not has_yielded_audio:
            raise MediaNotFoundError(
                "Unable to stream audiobook: no playable chapters found"
            ) from last_error

    async def get_rotor_station_tracks(
        self, station_id: str, queue: str | int | None = None
    ) -> tuple[list[Any], str | None]:
        """
        Fetch tracks from a rotor station using the session API.

        Public surface — pinned by the ynison plugin
        (`YandexMusicProviderLike.get_rotor_station_tracks`). The
        ``(tracks, batch_id)`` return contract is kept for that caller even
        though batch_id is now a session-scoped identifier.

        Routes to ``_fetch_rotor_session_batch`` so the wave session state
        (`session_id`, seen tracks, prefetch) is shared with our own Browse /
        on_played / on_streamed flows. ``queue`` is the most recently played
        track ID the external caller observed — we record it as the
        pagination cursor before calling through.

        :param station_id: Rotor station ID (e.g. "user:onyourwave",
            "genre:rock", "mood:calm", "track:1234").
        :param queue: Last-played track ID for pagination. Ignored on the
            very first call (no session yet) but still recorded.
        :return: Tuple of (list of yandex tracks, batch_id or None).
        """
        wave = self._get_wave_state(station_id)
        # Cursor update + batch fetch run under the station's lock, matching
        # the discipline in browse / recommendations / prefetch. Without it,
        # ynison replenish racing with a concurrent MA browse could interleave
        # last_track_id writes and leave session_id / batch_id out of sync.
        async with wave.lock:
            if queue is not None:
                wave.last_track_id = str(queue)
            return await self._fetch_rotor_session_batch(wave, station_id)

    def get_quality(self) -> str:
        """Return the configured audio quality tier (e.g. 'balanced', 'superb')."""
        quality = str(self.config.get_value(CONF_QUALITY) or QUALITY_BALANCED).strip().lower()
        if quality == "lossless":
            quality = QUALITY_SUPERB
        return quality

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Resolve wave cover image with background color fill for transparent PNGs.

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
        Report periodic playback updates.

        - Audiobooks: persist chapter progress via play_audio so Yandex's
          own clients resume at the right point.
        - Wave tracks: send rotor ``trackStarted`` while actively playing and
          kick off a background prefetch so DSTM refill serves wave-curated
          tracks with no extra round-trip. DSTM itself is the user's toggle —
          the provider does not flip it.

        Generic track history reporting is not attempted here — the only
        known channel Yandex writes into ``/handlers/music-history`` is a
        long-lived Ynison WebSocket session, which lives in the sibling
        yandex_ynison plugin. Regular tracks played through MA are therefore
        invisible to Listening History unless that plugin is also active.
        """
        if media_type == MediaType.AUDIOBOOK:
            await self._report_audiobook_progress(prov_item_id, position)
            return
        if media_type != MediaType.TRACK:
            return
        _, station_id = _parse_radio_item_id(prov_item_id)
        if station_id and is_playing:
            track_id, _ = _parse_radio_item_id(prov_item_id)
            wave = self._wave_states.get(station_id) or self._get_wave_state(station_id)
            await self._send_wave_feedback(wave, station_id, "trackStarted", track_id=track_id)
            self.mass.create_task(self._prefetch_rotor_session(station_id))

    async def on_streamed(self, streamdetails: StreamDetails) -> None:
        """
        Report stream completion to Yandex.

        - Audiobooks: a final ``play_audio`` with the absolute stream
          position so the last listening point is preserved across Yandex
          clients. Cleans up session state even when ``data`` was stripped.
        - Wave tracks (composite item_id carries a station suffix): a rotor
          ``trackFinished`` or ``skip`` event with the actual seconds streamed
          so Yandex can improve recommendations.
        """
        data = streamdetails.data if isinstance(streamdetails.data, dict) else None
        if streamdetails.media_type == MediaType.AUDIOBOOK:
            await self._report_audiobook_final(streamdetails, data or {})
            return
        if streamdetails.media_type != MediaType.TRACK:
            return
        track_id, station_id = _parse_radio_item_id(streamdetails.item_id)
        if not station_id:
            return
        seconds = int(streamdetails.seconds_streamed or 0)
        duration = int(streamdetails.duration or 0)
        feedback_type = "trackFinished" if duration and seconds >= max(0, duration - 10) else "skip"
        wave = self._wave_states.get(station_id) or self._get_wave_state(station_id)
        await self._send_wave_feedback(
            wave, station_id, feedback_type, track_id=track_id, total_played_seconds=seconds
        )

    def _audiobook_progress_point(
        self,
        chapter_durations_ms: list[int],
        n_chapters: int,
        absolute_sec: int,
    ) -> tuple[int, int, int]:
        """
        Resolve an absolute book position into a play_audio-ready tuple.

        Returns ``(chapter_idx, track_length_seconds, offset_seconds)``, applying
        two invariants Yandex cares about and that ``_resolve_audiobook_seek``
        alone doesn't guarantee:

        - At/beyond end-of-book, map to end of the last chapter (not start),
          so Yandex's resume point doesn't rewind to the start of the final
          chapter on natural completion.
        - ``track_length_seconds`` is clamped to at least 1 and ``offset`` to
          ``[0, track_length_seconds]`` — a chapter with ``duration_ms=None``
          (coerced to 0 by the chapter-map builder) would otherwise send
          ``track_length_seconds=0`` and block progress from syncing.
        """
        absolute_sec = max(0, absolute_sec)
        total_duration_sec = sum(chapter_durations_ms) // 1000
        last_idx = max(n_chapters - 1, 0)
        if absolute_sec >= total_duration_sec > 0:
            idx = last_idx
            track_length_sec = max(1, chapter_durations_ms[idx] // 1000)
            offset = track_length_sec
        else:
            idx, offset_raw = self._resolve_audiobook_seek(
                chapter_durations_ms, absolute_sec, n_chapters
            )
            track_length_sec = max(1, chapter_durations_ms[idx] // 1000)
            offset = max(0, min(int(offset_raw), track_length_sec))
        return idx, track_length_sec, offset

    async def _report_audiobook_progress(self, audiobook_id: str, position_sec: int) -> None:
        """
        Push current listening position of an audiobook to Yandex.

        Resolves the playing chapter + offset from the cached chapter map, then
        calls play_audio so Yandex persists the position for cross-client resume.

        Best-effort: any non-cancellation failure while resolving the chapter
        map (rate-limit, network blip, auth edge case bubbling out of
        ``_call_with_retry``) must never break pause/stop, so it is swallowed
        here in addition to the errors already absorbed inside
        ``api_client.play_audio``.
        """
        try:
            chapter_ids, chapter_durations_ms = await self._resolve_audiobook_chapter_map(
                audiobook_id
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self.logger.debug(
                "Skipping audiobook progress report for %s (chapter map resolution failed): %s",
                audiobook_id,
                err,
            )
            return
        if not chapter_ids:
            self.logger.debug(
                "Audiobook %s has no chapter map; skipping progress report", audiobook_id
            )
            return
        idx, track_length_sec, offset = self._audiobook_progress_point(
            chapter_durations_ms, len(chapter_ids), int(position_sec)
        )
        play_id = self._audiobook_play_ids.setdefault(audiobook_id, uuid.uuid4().hex)
        await self.client.play_audio(
            track_id=chapter_ids[idx],
            album_id=audiobook_id,
            play_id=play_id,
            track_length_seconds=track_length_sec,
            total_played_seconds=offset,
            end_position_seconds=offset,
        )

    async def _report_audiobook_final(
        self, streamdetails: StreamDetails, data: dict[str, Any]
    ) -> None:
        """
        Send a closing play_audio for an audiobook stream.

        Uses the streamdetails' own ``chapter_ids`` / ``chapter_durations_ms``
        (populated when the StreamDetails was created) to stay consistent with
        what was actually played, then clears the session play_id and drops
        the chapter-map cache entry so long-running instances can't grow the
        cache without bound as users play more audiobooks.
        """
        audiobook_id = streamdetails.item_id
        chapter_ids = data.get("chapter_ids") or []
        chapter_durations_ms = data.get("chapter_durations_ms") or []
        play_id = self._audiobook_play_ids.pop(audiobook_id, None) or uuid.uuid4().hex
        self._audiobook_chapter_cache.pop(audiobook_id, None)
        if not chapter_ids or not chapter_durations_ms:
            return
        absolute_sec = int(streamdetails.seek_position + (streamdetails.seconds_streamed or 0))
        idx, track_length_sec, offset = self._audiobook_progress_point(
            chapter_durations_ms, len(chapter_ids), absolute_sec
        )
        await self.client.play_audio(
            track_id=chapter_ids[idx],
            album_id=audiobook_id,
            play_id=play_id,
            track_length_seconds=track_length_sec,
            total_played_seconds=offset,
            end_position_seconds=offset,
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
        return self._media_label("folder", _media_label_key(tag), tag.title())[0]

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
