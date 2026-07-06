"""Plex musicprovider support for MusicAssistant."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import warnings
from asyncio import Task, TaskGroup
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast
from urllib.parse import urlencode
from uuid import uuid4

import plexapi.exceptions
import plexapi.utils
import requests
import urllib3.exceptions
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ProviderConfig,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    SetupFailedError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItem,
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
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails
from plexapi.audio import Album as PlexAlbum
from plexapi.audio import Artist as PlexArtist
from plexapi.audio import Track as PlexTrack
from plexapi.base import PlexObject
from plexapi.myplex import MyPlexAccount
from plexapi.playlist import Playlist as PlexPlaylist
from plexapi.server import PlexServer

from music_assistant.constants import DB_TABLE_PROVIDER_MAPPINGS, UNKNOWN_ARTIST
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.tags import async_parse_tags
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.recommendation_payload import RecommendationPayloadMixin
from music_assistant.providers.plex.constants import (
    AUTH_TOKEN_UNAUTH,
    COLLECTION_ID_PREFIX,
    CONF_AUTH_TOKEN,
    CONF_COLLECTION_PREFIX,
    CONF_EXTENDED_RECOMMENDATIONS,
    CONF_HUB_ITEMS_LIMIT,
    CONF_IMPORT_COLLECTIONS,
    CONF_LIBRARY_ID,
    CONF_LOCAL_SERVER_IP,
    CONF_LOCAL_SERVER_PORT,
    CONF_LOCAL_SERVER_SSL,
    CONF_LOCAL_SERVER_VERIFY_CERT,
    CONF_PLEX_FAVORITE_THRESHOLD,
    CONF_PLEX_LIKE_RATING,
    CONF_PLEX_UNLIKE_RATING,
    CONF_STREAM_QUALITY,
    ERR_ARTIST_INVALID_ID,
    ERR_ARTIST_NOT_FOUND,
    ERR_AUTH_FAILED,
    ERR_INVALID_CREDENTIALS,
    ERR_ITEM_NOT_FOUND,
    ERR_NO_ARTIST_FOR_TRACK,
    ERR_TRACK_NOT_FOUND,
    FAKE_ARTIST_PREFIX,
    MIX_CACHE_EXPIRATION,
    MIX_ITEM_PREFIX,
    RECOMMENDATIONS_HUB_PARAMS,
    STREAM_QUALITY_96,
    STREAM_QUALITY_128,
    STREAM_QUALITY_192,
    STREAM_QUALITY_320,
    STREAM_QUALITY_ORIGINAL,
)
from music_assistant.providers.plex.helpers import (
    AUDIOBOOK_FEATURES,
    CONF_LIBRARY_TYPE,
    LIBRARY_TYPE_AUDIOBOOKS,
    LIBRARY_TYPE_MUSIC,
    LIBRARY_TYPE_PODCASTS,
    LIBRARY_TYPE_TO_MEDIA_TYPES,
    PODCAST_FEATURES,
    SUPPORTED_FEATURES,
    extract_library_name,
    get_explicit,
    get_favorite_from_rating,
    get_musicbrainz_id,
    get_thumbnail_images,
    parse_plex_lyrics_payload,
)

# Public surface of the provider package. With mypy's no_implicit_reexport,
# names imported into this module (e.g. CONF_LIBRARY_ID from .constants) are
# only re-exported when listed here.
__all__ = [
    "CONF_LIBRARY_ID",
    "PlexProvider",
    "setup",
]

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine

    from music_assistant_models.provider import ProviderManifest
    from plexapi.library import LibraryMediaTag as PlexCollection
    from plexapi.library import MusicSection as PlexMusicSection
    from plexapi.media import AudioStream as PlexAudioStream
    from plexapi.media import Media as PlexMedia
    from plexapi.media import MediaPart as PlexMediaPart

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

_LOGGER = logging.getLogger(__name__)

UNKNOWN_NAME = "[Unknown]"
PODCAST_PREFIX = "podcast:"
PODCAST_EPISODE_PREFIX = "podcast_episode:"
AUDIOBOOK_PREFIX = "audiobook:"
CHAPTER_PREFIX = "Chapter"
EPISODE_PREFIX = "Episode"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # the token lives in setup_data for new installs, or (pre-flow) in the legacy config values
    if not (config.setup_data.get(CONF_AUTH_TOKEN) or config.get_value(CONF_AUTH_TOKEN)):
        raise LoginFailed(ERR_INVALID_CREDENTIALS)

    return PlexProvider(mass, manifest, config, SUPPORTED_FEATURES)


Param = ParamSpec("Param")
RetType = TypeVar("RetType")
PlexObjectT = TypeVar("PlexObjectT", bound=PlexObject)
MediaItemT = TypeVar("MediaItemT", bound=MediaItem)


class PlexProvider(RecommendationPayloadMixin, MusicProvider):
    """Provider for a plex music library."""

    # keep the pre-refactor 3h refresh interval for the hubs payload
    recommendation_payload_ttl = 3600 * 3

    _plex_server: PlexServer = None
    _plex_library: PlexMusicSection = None
    _myplex_account: MyPlexAccount = None
    _baseurl: str

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a postfix with the library name and type."""
        library_name = extract_library_name(str(self.get_setup_value(CONF_LIBRARY_ID) or ""))
        library_type = self._get_library_type()
        if library_type in (LIBRARY_TYPE_AUDIOBOOKS, LIBRARY_TYPE_PODCASTS):
            type_label = library_type.title()
            # Avoid duplication when the library name already indicates its type
            if library_name.lower() == type_label.lower():
                return library_name
            return f"{library_name} - {type_label}"
        if library_name:
            return library_name
        return None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return Config entries to configure this provider.

        Server connection, authentication and library selection are handled by the setup flow
        (see setup_flow.py); only the genuine options are configurable here.
        """
        entries: list[ConfigEntry] = []

        # Collection import options (advanced settings)
        entries.append(
            ConfigEntry(
                key=CONF_IMPORT_COLLECTIONS,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                advanced=True,
            )
        )
        entries.append(
            ConfigEntry(
                key=CONF_COLLECTION_PREFIX,
                type=ConfigEntryType.STRING,
                default_value="Collection: ",
                depends_on=CONF_IMPORT_COLLECTIONS,
                advanced=True,
            )
        )

        entries.append(
            ConfigEntry(
                key=CONF_STREAM_QUALITY,
                type=ConfigEntryType.STRING,
                default_value=STREAM_QUALITY_ORIGINAL,
                options=[
                    ConfigValueOption(STREAM_QUALITY_ORIGINAL),
                    ConfigValueOption(STREAM_QUALITY_96),
                    ConfigValueOption(STREAM_QUALITY_128),
                    ConfigValueOption(STREAM_QUALITY_192),
                    ConfigValueOption(STREAM_QUALITY_320),
                ],
            )
        )

        # rating/favorite sync configuration
        entries.append(
            ConfigEntry(
                key=CONF_PLEX_LIKE_RATING,
                type=ConfigEntryType.FLOAT,
                default_value=10.0,
                range=(0, 10),
                category="sync_options",
            )
        )
        entries.append(
            ConfigEntry(
                key=CONF_PLEX_FAVORITE_THRESHOLD,
                type=ConfigEntryType.FLOAT,
                default_value=10.0,
                range=(0, 10),
                category="sync_options",
            )
        )
        entries.append(
            ConfigEntry(
                key=CONF_PLEX_UNLIKE_RATING,
                type=ConfigEntryType.FLOAT,
                default_value=0.0,
                range=(0, 10),
                category="sync_options",
            )
        )

        # Recommendation settings (advanced)
        entries.append(
            ConfigEntry(
                key=CONF_HUB_ITEMS_LIMIT,
                type=ConfigEntryType.INTEGER,
                default_value=10,
                advanced=True,
                range=(1, 100),
            )
        )
        entries.append(
            ConfigEntry(
                key=CONF_EXTENDED_RECOMMENDATIONS,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                advanced=True,
            )
        )

        # return all config entries
        return tuple(entries)

    async def handle_async_init(self) -> None:
        """Set up the music provider by connecting to the server."""
        # silence loggers
        logging.getLogger("plexapi").setLevel(self.logger.level + 10)

        library_name = extract_library_name(str(self.get_setup_value(CONF_LIBRARY_ID)))

        def connect() -> PlexServer:
            try:
                session = requests.Session()
                session.verify = (
                    bool(self.get_setup_value(CONF_LOCAL_SERVER_VERIFY_CERT))
                    if self.get_setup_value(CONF_LOCAL_SERVER_SSL)
                    else False
                )
                # Add Music Assistant client identification headers
                session.headers.update(
                    {
                        "X-Plex-Client-Identifier": self.instance_id,
                        "X-Plex-Product": "Music Assistant",
                        "X-Plex-Platform": "Music Assistant",
                        "X-Plex-Version": self.mass.version,
                    }
                )
                local_server_protocol = (
                    "https" if self.get_setup_value(CONF_LOCAL_SERVER_SSL) else "http"
                )
                token = self.get_setup_value(CONF_AUTH_TOKEN)
                plex_url = (
                    f"{local_server_protocol}://{self.get_setup_value(CONF_LOCAL_SERVER_IP)}"
                    f":{self.get_setup_value(CONF_LOCAL_SERVER_PORT)}"
                )
                # silence urllib3 InsecureRequestWarning from Plex connections
                # using wildcard certificates that don't validate against LAN IPs
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        category=urllib3.exceptions.InsecureRequestWarning,
                    )
                    if token == AUTH_TOKEN_UNAUTH:
                        # Doing local connection, not via plex.tv.
                        plex_server = PlexServer(plex_url, session=session)
                    else:
                        plex_server = PlexServer(
                            plex_url,
                            token,
                            session=session,
                        )
                # I don't think PlexAPI intends for this to be accessible, but we need it.
                self._baseurl = plex_server._baseurl

            except plexapi.exceptions.BadRequest as err:
                if "Invalid token" in str(err):
                    # the stored token is invalid; surface an auth failure so the user is
                    # sent through the reconfigure (reauth) flow, which overwrites the token
                    raise LoginFailed(ERR_AUTH_FAILED)
                raise LoginFailed from err
            return plex_server

        self._myplex_account = await self.get_myplex_account_and_refresh_token(
            str(self.get_setup_value(CONF_AUTH_TOKEN))
        )
        try:
            self._plex_server = await self._run_async(connect)
            self._plex_library = await self._run_async(
                self._plex_server.library.section, library_name
            )
        except requests.exceptions.ConnectionError as err:
            raise SetupFailedError from err
        # the library type is collected by the setup flow (setup_data), so a change now
        # arrives via a full reload rather than update_config; clean up any mappings left
        # behind by a previous type on load (idempotent - a no-op once nothing is stale)
        await self._cleanup_stale_library_mappings()

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
        return False

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        library_type = self._get_library_type()
        if library_type == LIBRARY_TYPE_AUDIOBOOKS:
            return AUDIOBOOK_FEATURES.copy()
        if library_type == LIBRARY_TYPE_PODCASTS:
            return PODCAST_FEATURES.copy()
        return self._supported_features.copy()

    async def resolve_image(self, path: str) -> str | bytes:
        """Return the full image URL including the auth token."""
        return str(self._plex_server.url(path, True))

    @use_cache(3600)  # Cache for 1 hour
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 20,
    ) -> SearchResults:
        """
        Perform search on the plex library.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        artists = None
        albums = None
        tracks = None
        playlists = None

        async with TaskGroup() as tg:
            if MediaType.ARTIST in media_types:
                artists = tg.create_task(
                    self._search_and_parse(
                        self._search_artist(search_query, limit), self._parse_artist
                    )
                )

            if MediaType.ALBUM in media_types:
                albums = tg.create_task(
                    self._search_and_parse(
                        self._search_album(search_query, limit), self._parse_album
                    )
                )

            if MediaType.TRACK in media_types:
                tracks = tg.create_task(
                    self._search_and_parse(
                        self._search_track(search_query, limit), self._parse_track
                    )
                )

            if MediaType.PLAYLIST in media_types:
                playlists = tg.create_task(
                    self._search_and_parse(
                        self._search_playlist(search_query, limit),
                        self._parse_playlist,
                    )
                )

        search_results = SearchResults()

        if artists:
            search_results.artists = artists.result()

        if albums:
            search_results.albums = albums.result()

        if tracks:
            search_results.tracks = tracks.result()

        if playlists:
            search_results.playlists = playlists.result()

        return search_results

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve all library artists from Plex Music."""
        artists_obj = await self._run_async(self._plex_library.all)
        for artist in artists_obj:
            yield await self._parse_artist(artist)

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve all library albums from Plex Music."""
        albums_obj = await self._run_async(self._plex_library.albums)
        for album in albums_obj:
            yield await self._parse_album(album)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve all library playlists from the provider."""
        playlists_obj = await self._run_async(self._plex_library.playlists)
        for playlist in playlists_obj:
            yield await self._parse_playlist(playlist)

        # Import collections as playlists if enabled
        if self.config.get_value(CONF_IMPORT_COLLECTIONS):
            collections_obj = await self._run_async(self._plex_library.collections)
            for collection in collections_obj:
                yield await self._parse_collection(collection)

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve library tracks from Plex Music."""
        page_size = 500
        offset = 0
        while True:
            # maxresults caps a single page; without it container_size is only the HTTP
            # batch size and plexapi keeps fetching until the end of the library, so every
            # iteration would return all remaining tracks (an O(n^2) re-scan of the library).
            batch = cast(
                "list[PlexTrack]",
                await self._run_async(
                    self._plex_library.searchTracks,
                    title=None,
                    maxresults=page_size,
                    container_size=page_size,
                    container_start=offset,
                ),
            )
            if not batch:
                break
            for plex_track in batch:
                yield await self._parse_track(plex_track)
            offset += page_size

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """Retrieve all library audiobooks from the configured Plex audiobook section."""
        if self._get_library_type() != LIBRARY_TYPE_AUDIOBOOKS:
            return
        try:
            albums_obj = await self._run_async(self._plex_library.albums)
        except Exception:
            self.logger.exception("Failed to list albums from audiobook library")
            return
        self.logger.debug(
            "Found %d albums in audiobook library '%s'",
            len(albums_obj),
            self._plex_library.title,
        )
        for album in albums_obj:
            try:
                yield await self._parse_audiobook(album, include_chapters=False)
            except Exception:
                self.logger.warning(
                    "Failed to parse audiobook album '%s' (key=%s); skipping",
                    getattr(album, "title", "[unknown]"),
                    getattr(album, "key", "[no key]"),
                    exc_info=True,
                )

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details (including chapters) by id."""
        if self._get_library_type() != LIBRARY_TYPE_AUDIOBOOKS:
            msg = "Audiobook library not configured"
            raise MediaNotFoundError(msg)
        album_key = prov_audiobook_id.removeprefix(AUDIOBOOK_PREFIX)
        try:
            plex_album = cast(
                "PlexAlbum",
                await self._run_async(self._plex_library.fetchItem, album_key, PlexAlbum),
            )
        except plexapi.exceptions.NotFound:
            msg = f"Audiobook {prov_audiobook_id} not found"
            raise MediaNotFoundError(msg)
        return await self._parse_audiobook(plex_album, include_chapters=True)

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Retrieve all library podcasts from the configured Plex podcast section."""
        if self._get_library_type() != LIBRARY_TYPE_PODCASTS:
            return
        try:
            albums_obj = await self._run_async(self._plex_library.albums)
        except Exception:
            self.logger.exception("Failed to list albums from podcast library")
            return
        for album in albums_obj:
            try:
                yield await self._parse_podcast(album, include_episodes=False)
            except Exception:
                self.logger.warning(
                    "Failed to parse podcast album '%s' (key=%s); skipping",
                    getattr(album, "title", "[unknown]"),
                    getattr(album, "key", "[no key]"),
                    exc_info=True,
                )

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details (including episodes) by id."""
        if self._get_library_type() != LIBRARY_TYPE_PODCASTS:
            msg = "Podcast library not configured"
            raise MediaNotFoundError(msg)
        album_key = prov_podcast_id.removeprefix(PODCAST_PREFIX)
        try:
            plex_album = cast(
                "PlexAlbum",
                await self._run_async(self._plex_library.fetchItem, album_key, PlexAlbum),
            )
        except plexapi.exceptions.NotFound:
            msg = f"Podcast {prov_podcast_id} not found"
            raise MediaNotFoundError(msg)
        return await self._parse_podcast(plex_album, include_episodes=True)

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Get all PodcastEpisodes for given podcast id."""
        if self._get_library_type() != LIBRARY_TYPE_PODCASTS:
            return
        album_key = prov_podcast_id.removeprefix(PODCAST_PREFIX)
        try:
            plex_album = cast(
                "PlexAlbum",
                await self._run_async(self._plex_library.fetchItem, album_key, PlexAlbum),
            )
        except plexapi.exceptions.NotFound:
            msg = f"Podcast {prov_podcast_id} not found"
            raise MediaNotFoundError(msg)
        for episode in await self._build_podcast_episodes(plex_album):
            yield episode

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get full podcast episode details by id."""
        if self._get_library_type() != LIBRARY_TYPE_PODCASTS:
            msg = "Podcast library not configured"
            raise MediaNotFoundError(msg)
        track_key = prov_episode_id.removeprefix(PODCAST_EPISODE_PREFIX)
        try:
            plex_track = cast(
                "PlexTrack",
                await self._run_async(self._plex_library.fetchItem, track_key, PlexTrack),
            )
        except plexapi.exceptions.NotFound:
            msg = f"Podcast episode {prov_episode_id} not found"
            raise MediaNotFoundError(msg)
        return await self._parse_podcast_episode(plex_track)

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """
        Get progress (resume point) details for the given audiobook or podcast.

        :param item_id: provider item id (e.g. "audiobook:<plex_key>").
        :param media_type: the media type (AUDIOBOOK or PODCAST).
        :return: (fully_played, position_ms, timestamp)
        """
        library_type = self._get_library_type()
        if media_type == MediaType.AUDIOBOOK and library_type == LIBRARY_TYPE_AUDIOBOOKS:
            album_key = item_id.removeprefix(AUDIOBOOK_PREFIX)
        elif media_type == MediaType.PODCAST and library_type == LIBRARY_TYPE_PODCASTS:
            album_key = item_id.removeprefix(PODCAST_PREFIX)
        elif media_type == MediaType.PODCAST_EPISODE and library_type == LIBRARY_TYPE_PODCASTS:
            episode_key = item_id.removeprefix(PODCAST_EPISODE_PREFIX)
            try:
                plex_track = cast(
                    "PlexTrack",
                    await self._run_async(self._plex_library.fetchItem, episode_key, PlexTrack),
                )
            except plexapi.exceptions.NotFound:
                msg = f"Podcast episode {episode_key} not found"
                raise MediaNotFoundError(msg)
            # For podcast episodes, progress lives on each individual track.
            # lastViewedAt may be on the parent album; fall back to the track.
            fully_played = bool(getattr(plex_track, "viewCount", 0) > 0)
            timestamp = getattr(plex_track, "lastViewedAt", None)
            if timestamp is not None and timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            resume_position_ms = getattr(plex_track, "viewOffset", 0) or 0
            return fully_played, resume_position_ms, timestamp
        else:
            raise NotImplementedError
        try:
            plex_album = cast(
                "PlexAlbum",
                await self._run_async(self._plex_library.fetchItem, album_key, PlexAlbum),
            )
        except plexapi.exceptions.NotFound:
            msg = f"Item {item_id} not found"
            raise MediaNotFoundError(msg)

        try:
            await self._run_async(plex_album.reload)
        except plexapi.exceptions.PlexApiException, requests.exceptions.RequestException:
            self.logger.warning(
                "Failed to reload metadata for position check (%s), using cached metadata",
                item_id,
            )

        fully_played = bool(getattr(plex_album, "viewCount", 0) > 0)
        timestamp = getattr(plex_album, "lastViewedAt", None)
        if timestamp is not None and timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)

        resume_position_ms = await self._calc_resume_position_ms(plex_album, fully_played)
        return fully_played, resume_position_ms, timestamp

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
        Handle callback when an audiobook or podcast has been played.

        Syncs progress back to the Plex server using the timeline/progress API.

        :param media_type: The media type (AUDIOBOOK or PODCAST).
        :param prov_item_id: The provider-specific item id.
        :param fully_played: True when the item has been played to the end.
        :param position: Last known position in seconds.
        :param media_item: The full media item details.
        :param is_playing: True when currently playing.
        """
        library_type = self._get_library_type()
        if media_type == MediaType.AUDIOBOOK and library_type == LIBRARY_TYPE_AUDIOBOOKS:
            album_key = prov_item_id.removeprefix(AUDIOBOOK_PREFIX)
        elif media_type == MediaType.PODCAST and library_type == LIBRARY_TYPE_PODCASTS:
            album_key = prov_item_id.removeprefix(PODCAST_PREFIX)
        elif media_type == MediaType.PODCAST_EPISODE and library_type == LIBRARY_TYPE_PODCASTS:
            episode_key = prov_item_id.removeprefix(PODCAST_EPISODE_PREFIX)
            plex_track = cast(
                "PlexTrack",
                await self._run_async(self._plex_library.fetchItem, episode_key, PlexTrack),
            )
            album_key = str(plex_track.parentKey)
        else:
            return

        try:
            plex_album = cast(
                "PlexAlbum",
                await self._run_async(self._plex_library.fetchItem, album_key, PlexAlbum),
            )
        except plexapi.exceptions.NotFound:
            self.logger.warning(
                "Failed to fetch %s %s for played sync", media_type.value, prov_item_id
            )
            return
        except Exception:
            self.logger.warning(
                "Failed to fetch %s %s for played sync",
                media_type.value,
                prov_item_id,
                exc_info=True,
            )
            return

        if fully_played:
            await self._run_async(plex_album.markPlayed)
            self.logger.debug("Marked %s %s as played in Plex", media_type.value, prov_item_id)
            return

        if position <= 0:
            await self._run_async(plex_album.markUnplayed)
            self.logger.debug("Marked %s %s as unplayed in Plex", media_type.value, prov_item_id)
            return

        try:
            target_track, target_offset_ms = await self._find_track_for_position(
                plex_album, position
            )
            if target_track is None:
                return

            state = "playing" if is_playing else "paused"
            # updateTimeline expects time in milliseconds (Plex native unit)
            await self._run_async(
                target_track.updateTimeline,
                target_offset_ms,
                state=state,
                duration=getattr(target_track, "duration", None),
            )
            self.logger.debug(
                "Synced %s %s progress to Plex: track %s at %dms (%s)",
                media_type.value,
                prov_item_id,
                target_track.title,
                target_offset_ms,
                state,
            )
        except Exception:
            self.logger.warning(
                "Failed to sync %s %s progress to Plex",
                media_type.value,
                prov_item_id,
                exc_info=True,
            )

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        plex_album = await self._get_data(prov_album_id, PlexAlbum)
        return await self._parse_album(plex_album)

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        plex_album: PlexAlbum = await self._get_data(prov_album_id, PlexAlbum)
        tracks = []
        for plex_track in await self._run_async(plex_album.tracks):
            track = await self._parse_track(
                plex_track,
            )
            tracks.append(track)
        return tracks

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        if prov_artist_id.startswith(FAKE_ARTIST_PREFIX):
            # This artist does not exist in plex, so we can just load it from DB.

            if db_artist := await self.mass.music.artists.get_library_item_by_prov_id(
                prov_artist_id, self.instance_id
            ):
                return db_artist
            raise MediaNotFoundError(ERR_ARTIST_NOT_FOUND.format(item_id=prov_artist_id))

        plex_artist = await self._get_data(prov_artist_id, PlexArtist)
        return await self._parse_artist(plex_artist)

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        plex_track = await self._get_data(prov_track_id, PlexTrack)
        track = await self._parse_track(plex_track)
        await self._add_track_lyrics(plex_track, track)
        return track

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        # Check if this is a collection (collections have the format "collection:<key>")
        if prov_playlist_id.startswith(COLLECTION_ID_PREFIX):
            collection_key = prov_playlist_id.removeprefix(COLLECTION_ID_PREFIX)
            plex_collection: PlexObject = await self._get_data(collection_key)
            return await self._parse_collection(plex_collection)

        # "Mixes For You" items use a MIX_ITEM_PREFIX (see _build_mix_playlist).
        if prov_playlist_id.startswith(MIX_ITEM_PREFIX):
            mix_key = prov_playlist_id.removeprefix(MIX_ITEM_PREFIX)
            fields = await self._find_mix_by_key(mix_key)
            if fields is None:
                msg = f"Mix {prov_playlist_id} not found"
                raise MediaNotFoundError(msg)
            _, title, thumb = fields
            # Cache title/artwork on interaction so replay from recently-played
            # still renders after Plex rotates the mix out of the hub.
            if mix_key:
                await self.mass.cache.set(
                    key=mix_key,
                    data={"title": title, "thumb": thumb},
                    provider=self.instance_id,
                    expiration=MIX_CACHE_EXPIRATION,
                )
            return self._build_mix_playlist(mix_key, title, thumb)

        plex_playlist = await self._get_data(prov_playlist_id, PlexPlaylist)
        return await self._parse_playlist(plex_playlist)

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        if page > 0:
            # paging not supported, we always return the whole list at once
            return []

        # Check if this is a collection (collections have the format "collection:<key>")
        if prov_playlist_id.startswith(COLLECTION_ID_PREFIX):
            collection_key = prov_playlist_id.removeprefix(COLLECTION_ID_PREFIX)
            plex_collection: PlexObject = await self._get_data(collection_key)
            if not (collection_items := await self._run_async(plex_collection.items)):
                return result
            # Collections can contain tracks, albums, or artists - we only want tracks
            for item in collection_items:
                if item.type == "track":
                    if track := await self._parse_track(item):
                        track.position = len(result) + 1
                        result.append(track)
                elif item.type == "album":
                    # If the collection contains albums, get all tracks from each album
                    album_tracks = await self.get_album_tracks(item.key)
                    for album_track in album_tracks:
                        album_track.position = len(result) + 1
                        result.append(album_track)
            return result

        # "Mixes For You" items use a MIX_ITEM_PREFIX. Strip it to recover
        # the Plex section-query key, append the track type filter to expand
        # albums into tracks, then shuffle — Plexamp randomizes mix playback
        # client-side.
        if prov_playlist_id.startswith(MIX_ITEM_PREFIX):
            mix_key = prov_playlist_id.removeprefix(MIX_ITEM_PREFIX)
            tracks_key = f"{mix_key}&type={plexapi.utils.searchType('track')}"
            plex_tracks = await self._run_async(self._plex_library.fetchItems, tracks_key)
            random.shuffle(plex_tracks)
            for index, plex_track in enumerate(plex_tracks, 1):
                if track := await self._parse_track(plex_track):
                    track.position = index
                    result.append(track)
            return result

        plex_playlist: PlexPlaylist = await self._get_data(prov_playlist_id, PlexPlaylist)
        if not (playlist_items := await self._run_async(plex_playlist.items)):
            return result
        for index, plex_track in enumerate(playlist_items, 1):
            if track := await self._parse_track(plex_track):
                track.position = index
                result.append(track)
        return result

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of albums for the given artist."""
        if not prov_artist_id.startswith(FAKE_ARTIST_PREFIX):
            plex_artist = await self._get_data(prov_artist_id, PlexArtist)
            try:
                plex_albums = cast("list[PlexAlbum]", await self._run_async(plex_artist.albums))
            except plexapi.exceptions.NotFound:
                # PlexArtist.albums() relies on Plex's advanced filters API.
                # Some Plex servers return no filtering metadata, making plexapi
                # raise 'Unknown libtype "artist"'. Fall back to the artist's
                # /children endpoint, which does not depend on the filters API.
                albums_key = f"{plex_artist.key}/children"
                plex_albums = cast(
                    "list[PlexAlbum]",
                    await self._run_async(plex_artist.fetchItems, albums_key, PlexAlbum),
                )
            if plex_albums:
                albums = []
                for album_obj in plex_albums:
                    albums.append(await self._parse_album(album_obj))
                return albums
        return []

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top tracks for the given artist using Plex artist radio/station."""
        if prov_artist_id.startswith(FAKE_ARTIST_PREFIX):
            return []

        try:
            plex_artist = await self._get_data(prov_artist_id, PlexArtist)
            # Get the artist radio station which contains top/popular tracks
            if station := await self._run_async(plex_artist.station):
                # Get tracks from the station
                station_tracks = await self._run_async(station.items)
                tracks = []
                for plex_track in station_tracks[:25]:  # Limit to 25 top tracks
                    if track := await self._parse_track(plex_track):
                        tracks.append(track)
                self.logger.debug(
                    "Retrieved %d top tracks for artist %s", len(tracks), prov_artist_id
                )
                return tracks
            self.logger.warning("No station available for artist %s", prov_artist_id)
        except Exception as err:
            self.logger.warning("Error getting top tracks for artist %s: %s", prov_artist_id, err)
        return []

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Get similar tracks using Plex's sonicallySimilar feature."""
        try:
            plex_track = await self._get_data(prov_track_id, PlexTrack)
            # Get sonically similar tracks
            similar_tracks = await self._run_async(plex_track.sonicallySimilar, limit=limit)
            tracks = []
            for similar_track in similar_tracks:
                if track := await self._parse_track(similar_track):
                    tracks.append(track)
            self.logger.debug(
                "Retrieved %d similar tracks for track %s", len(tracks), prov_track_id
            )
            return tracks
        except Exception as err:
            self.logger.warning("Error getting similar tracks for %s: %s", prov_track_id, err)
        return []

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get this provider's available recommendation rows, without items."""
        return await self._recommendation_rows_from_payload()

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        return await self._recommendation_items_from_payload(item_id)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track/audiobook/podcast episode."""
        if media_type == MediaType.AUDIOBOOK:
            return await self._get_audiobook_stream_details(item_id)
        if media_type == MediaType.PODCAST_EPISODE:
            return await self._get_podcast_episode_stream_details(item_id)

        plex_track = await self._get_data(item_id, PlexTrack)
        if not plex_track.media:
            raise MediaNotFoundError(ERR_TRACK_NOT_FOUND.format(item_id=item_id))

        media: PlexMedia = plex_track.media[0]

        content_type = (
            ContentType.try_parse(media.container) if media.container else ContentType.UNKNOWN
        )
        media_part: PlexMediaPart = media.parts[0]
        audio_streams = media_part.audioStreams()
        audio_stream: PlexAudioStream | None = audio_streams[0] if audio_streams else None

        stream_details = StreamDetails(
            item_id=plex_track.key,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=content_type,
                channels=media.audioChannels,
            ),
            stream_type=StreamType.HTTP,
            # plex reports duration in milliseconds, streamdetails expect seconds
            duration=int(plex_track.duration / 1000) if plex_track.duration else None,
            data=plex_track,
            can_seek=True,
            allow_seek=True,
        )

        if quality_bitrate := self._get_stream_quality_bitrate():
            stream_details.path = self._get_transcode_url(plex_track, quality_bitrate)
            stream_details.stream_type = StreamType.HLS
            stream_details.audio_format.content_type = ContentType.OPUS
            stream_details.audio_format.bit_rate = quality_bitrate
            return stream_details

        download_url = self._plex_server.url(f"{media_part.key}?download=1", True)
        if content_type != ContentType.M4A:
            stream_details.path = download_url
            if audio_stream and audio_stream.samplingRate:
                stream_details.audio_format.sample_rate = audio_stream.samplingRate
            if audio_stream and audio_stream.bitDepth:
                stream_details.audio_format.bit_depth = audio_stream.bitDepth

        else:
            media_info = await async_parse_tags(download_url)
            stream_details.path = download_url
            stream_details.audio_format.channels = media_info.channels
            stream_details.audio_format.content_type = ContentType.try_parse(media_info.format)
            stream_details.audio_format.sample_rate = media_info.sample_rate
            stream_details.audio_format.bit_depth = media_info.bits_per_sample

        return stream_details

    async def get_myplex_account_and_refresh_token(self, auth_token: str) -> MyPlexAccount:
        """Get a MyPlexAccount object and refresh the token if needed."""
        if auth_token == AUTH_TOKEN_UNAUTH:
            return self._myplex_account

        def _refresh_plex_token() -> MyPlexAccount:
            if self._myplex_account is None:
                myplex_account = MyPlexAccount(token=auth_token)
                self._myplex_account = myplex_account
            self._myplex_account.ping()
            return self._myplex_account

        return await asyncio.to_thread(_refresh_plex_token)

    async def set_favorite(self, prov_item_id: str, media_type: MediaType, favorite: bool) -> None:
        """Set favorite status by setting rating in Plex."""
        if favorite:
            # Set like rating
            rating = cast("float", self.config.get_value(CONF_PLEX_LIKE_RATING))
        else:
            # Set unlike rating
            rating = cast("float", self.config.get_value(CONF_PLEX_UNLIKE_RATING))

        if media_type == MediaType.TRACK:
            plex_item: PlexTrack | PlexAlbum = await self._get_data(prov_item_id, PlexTrack)
        elif media_type == MediaType.ALBUM:
            plex_item = await self._get_data(prov_item_id, PlexAlbum)
        else:
            return
        await self._run_async(plex_item.rate, rating)
        self.logger.debug(
            "Set Plex rating to %s for %s with ID %s (ratingKey: %s)",
            rating,
            media_type.value,
            prov_item_id,
            plex_item.ratingKey,
        )

    def _get_library_type(self) -> str:
        """Return the configured library type, defaulting to music."""
        return str(self.get_setup_value(CONF_LIBRARY_TYPE) or LIBRARY_TYPE_MUSIC)

    async def _cleanup_stale_library_mappings(self) -> None:
        """Remove provider mappings that do not belong to the current library type."""
        if not self.mass.music.database:
            return
        valid_types = set(LIBRARY_TYPE_TO_MEDIA_TYPES.get(self._get_library_type(), ()))
        all_types = {t for types in LIBRARY_TYPE_TO_MEDIA_TYPES.values() for t in types}
        for media_type in all_types - valid_types:
            controller = self.mass.music.get_controller(media_type)
            query = (
                f"SELECT item_id FROM {DB_TABLE_PROVIDER_MAPPINGS} "
                f"WHERE media_type = '{media_type.value}' "
                f"AND provider_instance = '{self.instance_id}'"
            )
            rows = await self.mass.music.database.get_rows_from_query(query, limit=100000)
            if rows:
                self.logger.info(
                    "Cleaning up %d stale %s provider mapping(s)", len(rows), media_type.value
                )
            for db_row in rows:
                try:
                    await controller.remove_provider_mappings(db_row["item_id"], self.instance_id)
                except Exception as err:
                    self.logger.warning(
                        "Failed to remove stale %s provider mapping for %s: %s",
                        media_type.value,
                        db_row["item_id"],
                        err,
                    )

    def _get_stream_quality_bitrate(self) -> int | None:
        """Return the configured Plex transcode bitrate, if enabled."""
        quality = str(self.config.get_value(CONF_STREAM_QUALITY) or STREAM_QUALITY_ORIGINAL)
        if quality == STREAM_QUALITY_ORIGINAL:
            return None
        if quality in {
            STREAM_QUALITY_96,
            STREAM_QUALITY_128,
            STREAM_QUALITY_192,
            STREAM_QUALITY_320,
        }:
            return int(quality)
        self.logger.warning("Invalid Plex stream quality configured: %s", quality)
        return None

    def _get_transcode_url(self, plex_track: PlexTrack, quality_bitrate: int) -> str:
        """Return a Plex transcode URL for the requested bitrate."""
        protocol = "hls"
        audio_codec = "opus"
        profile_extra = (
            f"add-transcode-target(type=musicProfile&context=streaming&protocol={protocol}"
            f"&container=mpegts&audioCodec={audio_codec})"
            f"+add-limitation(scope=musicCodec&scopeName={audio_codec}&type=upperBound"
            f"&name=audio.bitrate&value={quality_bitrate}&replace=true)"
            f"+add-limitation(scope=musicCodec&scopeName={audio_codec}&type=lowerBound"
            f"&name=audio.bitrate&value={quality_bitrate}&replace=true)"
        )
        params = {
            "path": plex_track.key,
            "mediaIndex": 0,
            "partIndex": 0,
            "minAudioBitrate": quality_bitrate,
            "maxAudioBitrate": quality_bitrate,
            "musicBitrate": quality_bitrate,
            "directStreamAudio": 0,
            "mediaBufferSize": 12288,
            "session": str(uuid4()),
            "protocol": protocol,
            "directPlay": 0,
            "directStream": 0,
            "hasMDE": 1,
            "X-Plex-Platform": "Chrome",
            "X-Plex-Client-Profile-Extra": profile_extra,
        }
        return str(
            self._plex_server.url(
                f"/music/:/transcode/universal/start.m3u8?{urlencode(params)}",
                True,
            )
        )

    async def _run_async(
        self, call: Callable[Param, RetType], *args: Param.args, **kwargs: Param.kwargs
    ) -> RetType:
        return await asyncio.to_thread(call, *args, **kwargs)

    async def _get_data(self, key: str, cls: type[PlexObjectT] | None = None) -> PlexObjectT:
        try:
            results = await self._run_async(self._plex_library.fetchItem, key, cls)
        except plexapi.exceptions.NotFound as err:
            raise MediaNotFoundError(ERR_ITEM_NOT_FOUND.format(item_id=key)) from err
        return cast("PlexObjectT", results)

    def _get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        """Get item mapping for a given media type, key, and name."""
        if not name:
            self.logger.info(
                "Received None or empty name for media item. Media type: %s, Key: %s",
                media_type,
                key,
            )
            name = UNKNOWN_NAME

        mapped_name, mapped_version = parse_title_and_version(name)

        if not mapped_name:
            self.logger.info(
                "Failed to map name for media item. Media type: %s, Key: %s, Original name: %s",
                media_type,
                key,
                name,
            )
            mapped_name = UNKNOWN_NAME
        if not mapped_version and media_type not in (MediaType.ALBUM, MediaType.TRACK):
            mapped_version = ""

        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.instance_id,
            name=mapped_name,
            version=mapped_version,
        )

    async def _get_or_create_artist_by_name(self, artist_name: str) -> Artist | ItemMapping:
        if library_items := await self.mass.music.artists.get_library_items_by_query(
            search=artist_name, provider_filter=[self.instance_id]
        ):
            return ItemMapping.from_item(library_items[0])

        artist_id = FAKE_ARTIST_PREFIX + artist_name
        return Artist(
            item_id=artist_id,
            name=artist_name or UNKNOWN_ARTIST,
            provider=self.instance_id,
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    async def _parse(self, plex_media: PlexObject) -> MediaItem | None:
        if plex_media.type == "artist":
            return await self._parse_artist(plex_media)
        if plex_media.type == "album":
            return await self._parse_album(plex_media)
        if plex_media.type == "track":
            return await self._parse_track(plex_media)
        if plex_media.type == "playlist":
            return await self._parse_playlist(plex_media)
        return None

    async def _search_track(self, search_query: str, limit: int) -> list[PlexTrack]:
        return cast(
            "list[PlexTrack]",
            await self._run_async(self._plex_library.searchTracks, title=search_query, limit=limit),
        )

    async def _search_album(self, search_query: str, limit: int) -> list[PlexAlbum]:
        return cast(
            "list[PlexAlbum]",
            await self._run_async(self._plex_library.searchAlbums, title=search_query, limit=limit),
        )

    async def _search_artist(self, search_query: str, limit: int) -> list[PlexArtist]:
        return cast(
            "list[PlexArtist]",
            await self._run_async(
                self._plex_library.searchArtists, title=search_query, limit=limit
            ),
        )

    async def _search_playlist(self, search_query: str, limit: int) -> list[PlexPlaylist]:
        return cast(
            "list[PlexPlaylist]",
            await self._run_async(self._plex_library.playlists, title=search_query, limit=limit),
        )

    async def _search_and_parse(
        self,
        search_coro: Awaitable[list[PlexObjectT]],
        parse_coro: Callable[[PlexObjectT], Coroutine[Any, Any, MediaItemT]],
    ) -> list[MediaItemT]:
        task_results: list[Task[MediaItemT]] = []
        async with TaskGroup() as tg:
            for item in await search_coro:
                task_results.append(tg.create_task(parse_coro(item)))

        results: list[MediaItemT] = []
        for task in task_results:
            results.append(task.result())

        return results

    async def _parse_album(self, plex_album: PlexAlbum) -> Album:
        """Parse a Plex Album response to an Album model object."""
        album_id = plex_album.key
        album = Album(
            item_id=album_id,
            provider=self.instance_id,
            name=plex_album.title or UNKNOWN_NAME,
            provider_mappings={
                ProviderMapping(
                    item_id=str(album_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=plex_album.getWebURL(self._baseurl),
                )
            },
        )
        # Check if album rating meets the configured threshold for favorites
        favorite_threshold = cast("float", self.config.get_value(CONF_PLEX_FAVORITE_THRESHOLD))
        if (favorite := get_favorite_from_rating(plex_album, favorite_threshold)) is not None:
            album.favorite = favorite

        if plex_album.year:
            album.year = plex_album.year
        if images := get_thumbnail_images(plex_album, self.instance_id):
            album.metadata.images = images
        if plex_album.summary:
            album.metadata.description = plex_album.summary
        if plex_album.genres:
            album.metadata.genres = {genre.tag for genre in plex_album.genres if genre.tag}
        if plex_album.moods:
            album.metadata.mood = next((mood.tag for mood in plex_album.moods if mood.tag), None)
        if plex_album.styles:
            album.metadata.style = next(
                (style.tag for style in plex_album.styles if style.tag), None
            )
        if plex_album.originallyAvailableAt:
            album.metadata.release_date = plex_album.originallyAvailableAt
        if (explicit := get_explicit(plex_album)) is not None:
            album.metadata.explicit = explicit
        if mbid := get_musicbrainz_id(plex_album):
            with contextlib.suppress(InvalidDataError):
                album.mbid = mbid

        album.artists.append(
            self._get_item_mapping(
                MediaType.ARTIST,
                plex_album.parentKey,
                plex_album.parentTitle or UNKNOWN_ARTIST,
            )
        )
        return album

    async def _parse_artist(self, plex_artist: PlexArtist) -> Artist:
        """Parse a Plex Artist response to Artist model object."""
        artist_id = plex_artist.key
        if not artist_id:
            raise InvalidDataError(ERR_ARTIST_INVALID_ID)
        artist = Artist(
            item_id=artist_id,
            name=plex_artist.title or UNKNOWN_ARTIST,
            provider=self.instance_id,
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=plex_artist.getWebURL(self._baseurl),
                )
            },
        )
        if plex_artist.summary:
            artist.metadata.description = plex_artist.summary
        if images := get_thumbnail_images(plex_artist, self.instance_id):
            artist.metadata.images = images
        if plex_artist.genres:
            artist.metadata.genres = {genre.tag for genre in plex_artist.genres if genre.tag}
        if plex_artist.moods:
            artist.metadata.mood = next((mood.tag for mood in plex_artist.moods if mood.tag), None)
        if plex_artist.styles:
            artist.metadata.style = next(
                (style.tag for style in plex_artist.styles if style.tag), None
            )
        if mbid := get_musicbrainz_id(plex_artist):
            with contextlib.suppress(InvalidDataError):
                artist.mbid = mbid
        return artist

    async def _parse_playlist(self, plex_playlist: PlexPlaylist) -> Playlist:
        """Parse a Plex Playlist response to a Playlist object."""
        playlist = Playlist(
            item_id=plex_playlist.key,
            provider=self.instance_id,
            name=plex_playlist.title or UNKNOWN_NAME,
            provider_mappings={
                ProviderMapping(
                    item_id=plex_playlist.key,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=plex_playlist.getWebURL(self._baseurl),
                )
            },
        )
        if plex_playlist.summary:
            playlist.metadata.description = plex_playlist.summary
        if images := get_thumbnail_images(plex_playlist, self.instance_id):
            playlist.metadata.images = images
        playlist.is_editable = not plex_playlist.smart
        return playlist

    async def _parse_collection(self, plex_collection: PlexCollection) -> Playlist:
        """Parse a Plex Collection response to a Playlist object."""
        # Get the configured collection prefix
        collection_prefix = str(self.config.get_value(CONF_COLLECTION_PREFIX) or "")

        # Collections are imported as playlists with the configured prefix
        playlist = Playlist(
            item_id=f"{COLLECTION_ID_PREFIX}{plex_collection.key}",
            provider=self.instance_id,
            name=f"{collection_prefix}{plex_collection.title}",
            provider_mappings={
                ProviderMapping(
                    item_id=f"{COLLECTION_ID_PREFIX}{plex_collection.key}",
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        # Add collection poster/thumbnail if available
        if images := get_thumbnail_images(
            plex_collection, self.instance_id, ("thumb", "composite")
        ):
            playlist.metadata.images = images
        # Collections are not editable in Music Assistant
        playlist.is_editable = False
        return playlist

    def _mix_playlist_fields(self, plex_mix: PlexPlaylist) -> tuple[str, str, str | None]:
        """
        Extract (smart-query key, title, centroid thumb) from a 'Mix For You' item.

        :param plex_mix: A Plex Playlist parsed from the 'Mixes For You' hub.
        """
        # Read straight from the parsed XML element. These synthetic mix playlists
        # carry a centroid-derived ratingKey rather than their own, so touching any
        # attribute that triggers a reload (e.g. .thumb) re-fetches the wrong object
        # and corrupts it. The smart-query key, title, and centroid artist thumb are
        # all present on the partial element itself.
        data = plex_mix._data
        mix_key = data.get("key") or ""
        title = data.get("title") or "[Unknown Mix]"
        thumb = next(
            (child.get("thumb") for child in data if child.get("centroid") and child.get("thumb")),
            None,
        )
        return mix_key, title, thumb

    def _build_mix_playlist(self, mix_key: str, title: str, thumb: str | None) -> Playlist:
        """
        Build a MA Playlist from a Plex 'Mix For You' hub item.

        :param mix_key: The Plex smart-query key identifying the mix.
        :param title: The mix title.
        :param thumb: The centroid artist thumb path, if any.
        """
        item_id = f"{MIX_ITEM_PREFIX}{mix_key}"
        playlist = Playlist(
            item_id=item_id,
            provider=self.instance_id,
            name=title,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if thumb:
            playlist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=thumb,
                        provider=self.instance_id,
                        remotely_accessible=False,
                    )
                ]
            )
        playlist.is_editable = False
        playlist.is_dynamic = True
        return playlist

    async def _get_mix_playlists(self, count: int) -> list[PlexPlaylist]:
        """
        Fetch the 'Mixes For You' hub items as Plex Playlist objects.

        :param count: Maximum number of items per hub.
        """
        key = f"/hubs/sections/{self._plex_library.key}?count={count}&{RECOMMENDATIONS_HUB_PARAMS}"
        hubs = await self._run_async(self._plex_library.fetchItems, key)
        for hub in hubs:
            if "music.mixes" in (hub.hubIdentifier or ""):
                return list(hub._partialItems)
        return []

    async def _find_mix_by_key(self, mix_key: str) -> tuple[str, str, str | None] | None:
        """Find a 'Mix For You' by its smart-query key, falling back to cache."""
        limit_value = self.config.get_value(CONF_HUB_ITEMS_LIMIT)
        limit = int(limit_value) if isinstance(limit_value, (int, float, str)) else 10
        for plex_mix in await self._get_mix_playlists(limit):
            fields = self._mix_playlist_fields(plex_mix)
            if fields[0] == mix_key:
                return fields
        # Plex rotates mixes out of the hub, but the smart-query key remains a
        # valid section query, so replay from recently-played still works — we
        # only need the cache to restore the title and artwork.
        cached = await self.mass.cache.get(key=mix_key, provider=self.instance_id)
        if isinstance(cached, dict):
            return mix_key, cached.get("title") or "[Unknown Mix]", cached.get("thumb")
        return None

    async def _parse_track(self, plex_track: PlexTrack) -> Track:
        """Parse a Plex Track response to a Track model object."""
        content = plex_track.media[0].container if plex_track.media else None
        track = Track(
            item_id=plex_track.key,
            provider=self.instance_id,
            name=plex_track.title or UNKNOWN_NAME,
            provider_mappings={
                ProviderMapping(
                    item_id=plex_track.key,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    # For Plex (local library provider), assume tracks are available by default
                    # even if media attribute is not populated in the initial response.
                    # This prevents tracks from being skipped during library sync.
                    available=True,
                    audio_format=AudioFormat(
                        content_type=(
                            ContentType.try_parse(content) if content else ContentType.UNKNOWN
                        ),
                    ),
                    url=plex_track.getWebURL(self._baseurl),
                )
            },
            disc_number=plex_track.parentIndex or 0,
            track_number=plex_track.trackNumber or 0,
        )
        # Check if track rating meets the configured threshold for favorites
        favorite_threshold = cast("float", self.config.get_value(CONF_PLEX_FAVORITE_THRESHOLD))
        if (favorite := get_favorite_from_rating(plex_track, favorite_threshold)) is not None:
            track.favorite = favorite

        if plex_track.originalTitle and plex_track.originalTitle != plex_track.grandparentTitle:
            # The artist of the track if different from the album's artist.
            # For this kind of artist, we just know the name, so we create a fake artist,
            # if it does not already exist.
            track.artists.append(
                await self._get_or_create_artist_by_name(plex_track.originalTitle or UNKNOWN_ARTIST)
            )
        elif plex_track.grandparentKey:
            track.artists.append(
                self._get_item_mapping(
                    MediaType.ARTIST,
                    plex_track.grandparentKey,
                    plex_track.grandparentTitle or UNKNOWN_ARTIST,
                )
            )
        else:
            raise InvalidDataError(ERR_NO_ARTIST_FOR_TRACK)

        if images := get_thumbnail_images(plex_track, self.instance_id):
            track.metadata.images = images
        if plex_track.genres:
            track.metadata.genres = {genre.tag for genre in plex_track.genres if genre.tag}
        if plex_track.moods:
            track.metadata.mood = next((mood.tag for mood in plex_track.moods if mood.tag), None)
        if (explicit := get_explicit(plex_track)) is not None:
            track.metadata.explicit = explicit
        if mbid := get_musicbrainz_id(plex_track):
            with contextlib.suppress(InvalidDataError):
                track.mbid = mbid
        if plex_track.parentKey:
            track.album = self._get_item_mapping(
                MediaType.ALBUM, plex_track.parentKey, plex_track.parentTitle
            )
        if plex_track.duration:
            track.duration = int(plex_track.duration / 1000)

        return track

    async def _add_track_lyrics(self, plex_track: PlexTrack, track: Track) -> None:
        """
        Fetch the track's lyric stream from Plex and attach it to the metadata.

        :param plex_track: The fully loaded Plex track to read lyric streams from.
        :param track: The Music Assistant track to populate with lyrics.
        """

        def _fetch() -> str | None:
            stream = next((stream for stream in plex_track.lyricStreams() if stream.key), None)
            if stream is None:
                return None
            url = plex_track._server.url(stream.key, includeToken=True)
            response: requests.Response = plex_track._server._session.get(
                url, headers={"Accept": "application/json"}, timeout=30
            )
            response.raise_for_status()
            # plexapi's untyped session makes the response Any for mypy; force str
            return str(response.text)

        try:
            content = await self._run_async(_fetch)
        except (requests.RequestException, plexapi.exceptions.PlexApiException) as err:
            self.logger.debug("Failed to fetch lyrics for %s: %s", plex_track.key, err)
            return
        if not content or (parsed := parse_plex_lyrics_payload(content)) is None:
            return
        lyrics, synced = parsed
        if synced:
            track.metadata.lrc_lyrics = lyrics
        else:
            track.metadata.lyrics = lyrics

    async def _parse_audiobook(
        self, plex_album: PlexAlbum, *, include_chapters: bool = False
    ) -> Audiobook:
        """Parse a Plex Album from the audiobook library into an Audiobook model."""
        audiobook_id = f"{AUDIOBOOK_PREFIX}{plex_album.key}"
        audiobook = Audiobook(
            item_id=audiobook_id,
            provider=self.instance_id,
            name=plex_album.title or UNKNOWN_NAME,
            provider_mappings={
                ProviderMapping(
                    item_id=audiobook_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=plex_album.getWebURL(self._baseurl),
                )
            },
        )
        # Author: parentTitle is the album artist; grandparentTitle is the album
        # artist parent (for multi-level nesting in Plex). Some setups vary.
        if author_name := plex_album.parentTitle or plex_album.grandparentTitle:
            audiobook.authors = UniqueList([author_name])
        if plex_album.summary:
            audiobook.metadata.description = plex_album.summary
        if plex_album.year:
            audiobook.metadata.release_date = datetime(plex_album.year, 1, 1, tzinfo=UTC)
        if images := get_thumbnail_images(plex_album, self.instance_id):
            audiobook.metadata.images = images
        # minified path: use album-level duration if Plex exposes it
        if album_duration := getattr(plex_album, "duration", None):
            audiobook.duration = int(album_duration / 1000)

        if include_chapters:
            chapters = await self._build_audiobook_chapters(plex_album)
            audiobook.metadata.chapters = chapters
            if chapters and chapters[-1].end is not None:
                audiobook.duration = int(chapters[-1].end)

        return audiobook

    async def _build_audiobook_chapters(self, plex_album: PlexAlbum) -> list[MediaItemChapter]:
        """Build chapter list from Plex tracks, skipping tracks without playable media."""
        plex_tracks = cast("list[PlexTrack]", await self._run_async(plex_album.tracks))
        plex_tracks.sort(key=lambda t: (t.parentIndex or 0, t.trackNumber or 0))
        chapters: list[MediaItemChapter] = []
        cumulative = 0.0
        chapter_num = 0
        for plex_track in plex_tracks:
            if not plex_track.media or not plex_track.media[0].parts:
                continue
            chapter_num += 1
            # plex_track.duration is in milliseconds (Plex native unit)
            duration_s = (plex_track.duration or 0) / 1000.0
            chapters.append(
                MediaItemChapter(
                    position=chapter_num,
                    name=plex_track.title or f"{CHAPTER_PREFIX} {chapter_num}",
                    start=cumulative,
                    end=cumulative + duration_s,
                )
            )
            cumulative += duration_s
        return chapters

    async def _parse_podcast(
        self, plex_album: PlexAlbum, *, include_episodes: bool = False
    ) -> Podcast:
        """Parse a Plex Album from the podcast library into a Podcast model."""
        podcast_id = f"{PODCAST_PREFIX}{plex_album.key}"
        podcast = Podcast(
            item_id=podcast_id,
            provider=self.instance_id,
            name=plex_album.title or UNKNOWN_NAME,
            provider_mappings={
                ProviderMapping(
                    item_id=podcast_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=plex_album.getWebURL(self._baseurl),
                )
            },
        )
        publisher = plex_album.studio or plex_album.parentTitle or plex_album.grandparentTitle
        if publisher:
            podcast.publisher = publisher
        if plex_album.summary:
            podcast.metadata.description = plex_album.summary
        if plex_album.year:
            podcast.metadata.release_date = datetime(plex_album.year, 1, 1, tzinfo=UTC)
        if images := get_thumbnail_images(plex_album, self.instance_id):
            podcast.metadata.images = images
        if include_episodes:
            podcast.total_episodes = await self._count_podcast_episodes(plex_album)
        return podcast

    async def _count_podcast_episodes(self, plex_album: PlexAlbum) -> int:
        """Count playable tracks without building full PodcastEpisode objects."""
        plex_tracks = cast("list[PlexTrack]", await self._run_async(plex_album.tracks))
        return sum(1 for t in plex_tracks if t.media and t.media[0].parts)

    async def _build_podcast_episodes(self, plex_album: PlexAlbum) -> list[PodcastEpisode]:
        """Build episode list from Plex tracks, skipping tracks without playable media."""
        plex_tracks = cast("list[PlexTrack]", await self._run_async(plex_album.tracks))
        plex_tracks.sort(key=lambda t: (t.parentIndex or 0, t.trackNumber or 0))
        episodes: list[PodcastEpisode] = []
        episode_num = 0
        for plex_track in plex_tracks:
            if not plex_track.media or not plex_track.media[0].parts:
                continue
            episode_num += 1
            duration_s = (plex_track.duration or 0) / 1000.0
            episode = PodcastEpisode(
                item_id=f"{PODCAST_EPISODE_PREFIX}{plex_track.key}",
                provider=self.instance_id,
                name=plex_track.title or f"{EPISODE_PREFIX} {episode_num}",
                position=episode_num,
                duration=int(duration_s),
                podcast=ItemMapping(
                    media_type=MediaType.PODCAST,
                    item_id=f"{PODCAST_PREFIX}{plex_album.key}",
                    provider=self.instance_id,
                    name=plex_album.title or UNKNOWN_NAME,
                ),
                provider_mappings={
                    ProviderMapping(
                        item_id=f"{PODCAST_EPISODE_PREFIX}{plex_track.key}",
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        url=plex_track.getWebURL(self._baseurl),
                        audio_format=AudioFormat(
                            content_type=(
                                ContentType.try_parse(plex_track.media[0].container)
                                if plex_track.media[0].container
                                else ContentType.UNKNOWN
                            )
                        ),
                    )
                },
            )
            if images := get_thumbnail_images(plex_track, self.instance_id):
                episode.metadata.images = images
            episodes.append(episode)
        return episodes

    async def _parse_podcast_episode(self, plex_track: PlexTrack) -> PodcastEpisode:
        """Parse a Plex Track from the podcast library into a PodcastEpisode model."""
        duration_s = (plex_track.duration or 0) / 1000.0
        content_type = ContentType.UNKNOWN
        if plex_track.media and plex_track.media[0].container:
            content_type = ContentType.try_parse(plex_track.media[0].container)
        episode = PodcastEpisode(
            item_id=f"{PODCAST_EPISODE_PREFIX}{plex_track.key}",
            provider=self.instance_id,
            name=plex_track.title or UNKNOWN_NAME,
            position=plex_track.trackNumber or 0,
            duration=int(duration_s),
            podcast=ItemMapping(
                media_type=MediaType.PODCAST,
                item_id=f"{PODCAST_PREFIX}{plex_track.parentKey}",
                provider=self.instance_id,
                name=plex_track.parentTitle or UNKNOWN_NAME,
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=f"{PODCAST_EPISODE_PREFIX}{plex_track.key}",
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=plex_track.getWebURL(self._baseurl),
                    audio_format=AudioFormat(content_type=content_type),
                )
            },
        )
        if images := get_thumbnail_images(plex_track, self.instance_id):
            episode.metadata.images = images
        return episode

    async def _calc_resume_position_ms(self, plex_album: PlexAlbum, fully_played: bool) -> int:
        """Calculate resume position from per-track viewOffset values."""
        plex_tracks = cast("list[PlexTrack]", await self._run_async(plex_album.tracks))
        plex_tracks.sort(key=lambda t: (t.parentIndex or 0, t.trackNumber or 0))

        # Per-track durations and viewOffset are in milliseconds (Plex native).
        resume_position_ms = 0
        cumulative_ms = 0
        for plex_track in plex_tracks:
            track_offset = getattr(plex_track, "viewOffset", 0) or 0
            if track_offset > 0:
                # Use the last non-zero offset — for sequential listening this
                # is the final playback position; it also handles non-linear
                # skipping better than first-match.
                resume_position_ms = cumulative_ms + track_offset
            cumulative_ms += getattr(plex_track, "duration", 0) or 0

        if resume_position_ms == 0 and fully_played:
            album_duration = getattr(plex_album, "duration", 0) or 0
            resume_position_ms = int(album_duration)

        return resume_position_ms

    async def _find_track_for_position(
        self, plex_album: PlexAlbum, position: int
    ) -> tuple[PlexTrack | None, int]:
        """Find the track and offset (ms) corresponding to the given position (s)."""
        plex_tracks = cast("list[PlexTrack]", await self._run_async(plex_album.tracks))
        plex_tracks.sort(key=lambda t: (t.parentIndex or 0, t.trackNumber or 0))

        position_ms = position * 1000
        cumulative_ms = 0
        for plex_track in plex_tracks:
            track_duration = getattr(plex_track, "duration", 0) or 0
            if cumulative_ms + track_duration > position_ms:
                return plex_track, position_ms - cumulative_ms
            cumulative_ms += track_duration

        if plex_tracks:
            # Position is past all tracks — clamp to end of the last track.
            last_track = plex_tracks[-1]
            last_duration = getattr(last_track, "duration", 0) or 0
            return last_track, last_duration

        return None, 0

    async def _get_audiobook_stream_details(self, item_id: str) -> StreamDetails:
        """Build multi-part StreamDetails for an audiobook (one part per Plex track)."""
        if self._get_library_type() != LIBRARY_TYPE_AUDIOBOOKS:
            msg = "Library not configured for audiobooks"
            raise MediaNotFoundError(msg)
        album_key = item_id.removeprefix(AUDIOBOOK_PREFIX)
        try:
            plex_album = cast(
                "PlexAlbum",
                await self._run_async(self._plex_library.fetchItem, album_key, PlexAlbum),
            )
        except plexapi.exceptions.NotFound:
            msg = f"Audiobook {item_id} not found"
            raise MediaNotFoundError(msg)

        plex_tracks = cast("list[PlexTrack]", await self._run_async(plex_album.tracks))
        plex_tracks.sort(key=lambda t: (t.parentIndex or 0, t.trackNumber or 0))

        parts, total_duration, first_container = self._build_stream_parts(plex_tracks, item_id)
        if not parts:
            self.logger.error(
                "Audiobook %s (%s) has no playable parts (%d tracks checked)",
                item_id,
                plex_album.title,
                len(plex_tracks),
            )
            msg = f"Audiobook {item_id} has no playable parts"
            raise MediaNotFoundError(msg)

        self.logger.debug(
            "Built StreamDetails for audiobook %s with %d parts, total_duration=%.1fs",
            item_id,
            len(parts),
            total_duration,
        )

        content_type = (
            ContentType.try_parse(first_container) if first_container else ContentType.UNKNOWN
        )
        if (
            (quality_bitrate := self._get_stream_quality_bitrate())
            and len(parts) == 1
            and (plex_track := self._get_single_playable_track(plex_tracks, item_id))
        ):
            return StreamDetails(
                provider=self.instance_id,
                item_id=item_id,
                media_type=MediaType.AUDIOBOOK,
                audio_format=AudioFormat(
                    content_type=ContentType.OPUS,
                    bit_rate=quality_bitrate,
                ),
                stream_type=StreamType.HLS,
                duration=int(total_duration),
                path=self._get_transcode_url(plex_track, quality_bitrate),
                can_seek=True,
                allow_seek=True,
            )

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            media_type=MediaType.AUDIOBOOK,
            audio_format=AudioFormat(content_type=content_type),
            stream_type=StreamType.HTTP,
            duration=int(total_duration),
            path=parts[0].path if len(parts) == 1 else parts,
            can_seek=True,
            allow_seek=True,
        )

    async def _fetch_recommendation_payload(self) -> list[RecommendationFolder]:
        """Fetch the full recommendations payload (folders with items) from the Plex hubs."""
        # Let fetch errors propagate: the payload mixin serves the last cached payload
        # on a failed refresh, and returning [] here would be cached as a valid empty
        # result for the full TTL.
        # Get the configured limit for items per hub
        limit_value = self.config.get_value(CONF_HUB_ITEMS_LIMIT)
        limit = int(limit_value) if isinstance(limit_value, (int, float, str)) else 10

        # Build the hubs key manually because plexapi's hubs() method
        # doesn't accept a count parameter to limit items per hub.
        extended = self.config.get_value(CONF_EXTENDED_RECOMMENDATIONS)
        hub_params = RECOMMENDATIONS_HUB_PARAMS if extended else "includeStations=1"
        key = f"/hubs/sections/{self._plex_library.key}?count={limit}&{hub_params}"
        hubs = await self._run_async(self._plex_library.fetchItems, key)

        if not hubs:
            self.logger.debug("No hubs available from Plex")
            return []

        self.logger.debug(
            "Fetching %d hubs (limit: %d items per hub)",
            len(hubs),
            limit,
        )

        folders = []
        for hub in hubs:
            # Create a recommendation folder for each hub
            folder = RecommendationFolder(
                name=hub.title,
                item_id=f"{self.instance_id}_{hub.hubIdentifier}",
                provider=self.instance_id,
                icon="mdi-music",
            )

            # Mixes For You are synthetic smart playlists; build them from
            # their partial hub items (see _mix_playlist_fields).
            if "music.mixes" in (hub.hubIdentifier or ""):
                folder.items.extend(
                    self._build_mix_playlist(*self._mix_playlist_fields(plex_mix))
                    for plex_mix in hub._partialItems
                )
                if folder.items:
                    folders.append(folder)
                continue

            # Parse each item based on its type (limit to configured max)
            # Use _partialItems to respect the count limit from the hubs() call
            # rather than hub.items() which fetches ALL items if more is True
            # _partialItems is a cached property that's already loaded, so no need for async
            hub_items = hub._partialItems
            self.logger.debug(
                "Processing hub '%s' (%s) with %d partial items",
                hub.title,
                hub.hubIdentifier,
                len(hub_items),
            )
            for item in hub_items:
                try:
                    # Skip items without type attribute
                    if not hasattr(item, "type"):
                        self.logger.debug(
                            "Skipping item in hub '%s': no type attribute",
                            hub.title,
                        )
                        continue

                    if parsed_item := await self._parse(item):
                        folder.items.append(parsed_item)  # type: ignore[arg-type]
                    else:
                        self.logger.debug(
                            "Skipping unsupported item type '%s' in hub '%s'",
                            item.type,
                            hub.title,
                        )
                except Exception as err:
                    self.logger.debug(
                        "Failed to parse item (type: %s) in hub '%s': %s",
                        getattr(item, "type", "unknown"),
                        hub.title,
                        str(err),
                    )
                    continue

            # Only add folder if it has items
            if folder.items:
                folders.append(folder)
                self.logger.debug(
                    "Added hub '%s' (%s) with %d items",
                    hub.title,
                    hub.hubIdentifier,
                    len(folder.items),
                )
            else:
                self.logger.debug(
                    "Skipping hub '%s' (%s): no items after parsing",
                    hub.title,
                    hub.hubIdentifier,
                )

        self.logger.debug("Retrieved %d recommendation folders from Plex", len(folders))
        return folders

    def _build_stream_parts(
        self, plex_tracks: list[PlexTrack], item_id: str
    ) -> tuple[list[MultiPartPath], float, str | None]:
        """Convert Plex tracks to MultiPartPath entries for streaming."""
        parts: list[MultiPartPath] = []
        total_duration = 0.0
        first_container: str | None = None
        for plex_track in plex_tracks:
            media = self._track_media_or_log(plex_track, item_id)
            if media is None:
                continue
            if first_container is None and media.container:
                first_container = media.container
            media_part: PlexMediaPart = media.parts[0]
            url = self._plex_server.url(f"{media_part.key}?download=1", True)
            duration_s = (plex_track.duration or 0) / 1000.0
            parts.append(MultiPartPath(path=url, duration=duration_s))
            total_duration += duration_s
            self.logger.debug(
                "Added audiobook part: track '%s' (%s) duration=%.1fs url=%s",
                plex_track.title,
                plex_track.key,
                duration_s,
                url,
            )
        return parts, total_duration, first_container

    def _get_single_playable_track(
        self, plex_tracks: list[PlexTrack], item_id: str
    ) -> PlexTrack | None:
        """Return the only playable Plex track, if there is exactly one."""
        playable_tracks: list[PlexTrack] = []
        for plex_track in plex_tracks:
            if self._track_media_or_log(plex_track, item_id) is not None:
                playable_tracks.append(plex_track)
        return playable_tracks[0] if len(playable_tracks) == 1 else None

    def _track_media_or_log(self, plex_track: PlexTrack, item_id: str) -> PlexMedia | None:
        """Return the first PlexMedia for a track, or log and return None if unavailable."""
        if not plex_track.media:
            self.logger.debug(
                "Skipping track '%s' (key=%s) in audiobook %s: no media",
                plex_track.title,
                plex_track.key,
                item_id,
            )
            return None
        media: PlexMedia = plex_track.media[0]
        if not media.parts:
            self.logger.debug(
                "Skipping track '%s' (key=%s) in audiobook %s: media has no parts",
                plex_track.title,
                plex_track.key,
                item_id,
            )
            return None
        return media

    async def _get_podcast_episode_stream_details(self, item_id: str) -> StreamDetails:
        """Build streamdetails for a single podcast episode from a Plex track."""
        if self._get_library_type() != LIBRARY_TYPE_PODCASTS:
            msg = "Library not configured for podcasts"
            raise MediaNotFoundError(msg)
        track_key = item_id.removeprefix(PODCAST_EPISODE_PREFIX)
        try:
            plex_track = cast(
                "PlexTrack",
                await self._run_async(self._plex_library.fetchItem, track_key, PlexTrack),
            )
        except plexapi.exceptions.NotFound:
            msg = f"Podcast episode {item_id} not found"
            raise MediaNotFoundError(msg)

        if not plex_track.media:
            msg = f"Podcast episode {item_id} has no media"
            raise MediaNotFoundError(msg)

        media: PlexMedia = plex_track.media[0]
        if not media.parts:
            msg = f"Podcast episode {item_id} has no playable media parts"
            raise MediaNotFoundError(msg)
        content_type = (
            ContentType.try_parse(media.container) if media.container else ContentType.UNKNOWN
        )
        media_part: PlexMediaPart = media.parts[0]
        download_url = self._plex_server.url(f"{media_part.key}?download=1", True)
        stream_type = StreamType.HTTP
        audio_format = AudioFormat(content_type=content_type)
        if quality_bitrate := self._get_stream_quality_bitrate():
            download_url = self._get_transcode_url(plex_track, quality_bitrate)
            stream_type = StreamType.HLS
            audio_format.content_type = ContentType.OPUS
            audio_format.bit_rate = quality_bitrate

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            media_type=MediaType.PODCAST_EPISODE,
            audio_format=audio_format,
            stream_type=stream_type,
            duration=plex_track.duration,
            path=download_url,
            can_seek=True,
            allow_seek=True,
        )
