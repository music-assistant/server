"""Main Spotify provider implementation."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import aiohttp
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    AudioError,
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    RateLimited,
    ResourceTemporarilyUnavailable,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.media_items.metadata import MediaItemChapter
from music_assistant_models.streamdetails import StreamDetails
from orjson import JSONDecodeError

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.app_vars import app_var
from music_assistant.helpers.json import SerializableType, json_loads
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.helpers.util import lock
from music_assistant.models.music_provider import MusicProvider

from .backends import LibrespotBackend, SoloistSingleTrackBackend, SpotifyPlaybackBackend
from .constants import (
    BACKEND_SOLOIST,
    CONF_CLIENT_ID,
    CONF_PLAYBACK_BACKEND,
    CONF_REFRESH_TOKEN_DEV,
    CONF_REFRESH_TOKEN_GLOBAL,
    CONF_SYNC_AUDIOBOOK_PROGRESS,
    CONF_SYNC_PODCAST_PROGRESS,
    LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX,
    SOLOIST_DATA_DIR_NAME,
)
from .helpers import get_spotify_token
from .parsers import (
    parse_album,
    parse_artist,
    parse_audiobook,
    parse_playlist,
    parse_podcast,
    parse_podcast_episode,
    parse_track,
)

_PLAYLIST_PAGINATION_STATE_LIMIT = 32


class NotModifiedError(Exception):
    """Exception raised when a resource has not been modified."""


@dataclass(slots=True)
class _PlaylistPaginationState:
    """Hold the synchronization and metadata snapshot for one playlist endpoint."""

    lock: asyncio.Lock
    snapshot: dict[str, Any] | None = None


class SpotifyProvider(MusicProvider):
    """Implementation of a Spotify MusicProvider."""

    # Global session (MA's client ID) - always present
    _auth_info_global: dict[str, Any] | None = None
    # Developer session (user's custom client ID) - optional
    _auth_info_dev: dict[str, Any] | None = None
    _sp_user: dict[str, Any] | None = None
    _audiobooks_supported = False
    _playlist_pagination_states: OrderedDict[tuple[str, bool], _PlaylistPaginationState]
    # True if user has configured a custom client ID with valid authentication
    dev_session_active: bool = False
    throttler: ThrottlerManager
    backend: SpotifyPlaybackBackend

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return Config entries to setup this provider.

        Authentication is handled by the setup flow (see setup_flow.py); only the genuine
        options are configurable here.
        """
        # audiobook progress sync is only offered where the account's region supports audiobooks
        audiobooks_supported = bool(getattr(self, "audiobooks_supported", False))
        return (
            CONF_ENTRY_UNOFFICIAL_PROVIDER,
            ConfigEntry(
                key=CONF_SYNC_PODCAST_PROGRESS,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                category="sync_options",
            ),
            ConfigEntry(
                key=CONF_SYNC_AUDIOBOOK_PROGRESS,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                category="sync_options",
                hidden=not audiobooks_supported,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.cache_dir = os.path.join(self.mass.cache_path, self.instance_id)
        self._playlist_pagination_states = OrderedDict()
        # Default throttler for global session (heavy rate limited)
        self.throttler = ThrottlerManager(rate_limit=1, period=2)

        # playback authorization is independent of the Web API tokens
        self.backend = self._create_backend()
        await self.backend.setup()
        try:
            # try login which will raise if it fails (logs in global session)
            await self.login()

            # Check if user has a custom client ID with valid dev token
            client_id = self.get_setup_value(CONF_CLIENT_ID)
            dev_token = self.get_setup_value(CONF_REFRESH_TOKEN_DEV)

            if client_id and dev_token and self._sp_user:
                await self.login_dev()
                # Verify user matches
                userinfo = await self._get_data("me", use_global_session=False)
                if userinfo["id"] != self._sp_user["id"]:
                    raise LoginFailed(
                        "Developer session must use the same Spotify account as the main session."
                    )
                # loosen the throttler when a custom client id is used
                self.throttler = ThrottlerManager(rate_limit=45, period=30)
                self.dev_session_active = True
                self.logger.info("Developer Spotify session active.")

            self._audiobooks_supported = await self._test_audiobook_support()
            if not self._audiobooks_supported:
                self.logger.info(
                    "Audiobook support disabled: Audiobooks are not available in your region. "
                    "See https://support.spotify.com/us/authors/article/audiobooks-availability/ "
                    "for supported countries."
                )
            if not isinstance(self.backend, SoloistSingleTrackBackend):
                # a paired soloist session left behind by a backend switch holds
                # login material and is of no further use: remove it — only now
                # that the load succeeded, so a failed load (and its config
                # rollback) still has the working session
                await asyncio.to_thread(self._remove_soloist_session_dir)
        except BaseException:
            # a failed load is never registered, so unload() will not run:
            # release whatever the backend acquired (e.g. the shared pulse
            # capture server) before propagating
            with suppress(Exception):
                await self.backend.unload()
            raise

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        if (backend := getattr(self, "backend", None)) is not None:
            await backend.unload()
        if is_removed:
            # the storage dir holds the soloist login session; never keep it
            # around for a removed instance
            await asyncio.to_thread(self._remove_instance_storage)

    @property
    def max_concurrent_streams(self) -> int:
        """
        Return how many items the configured playback backend can fetch concurrently.

        Read from the stored config (not the backend instance): this property is
        already consulted while the provider object is being constructed.
        """
        # tolerate a bare instance: the stream-limit declaration tests read this
        # without constructing the provider
        if getattr(self, "mass", None) is not None and (
            self.get_setup_value(CONF_PLAYBACK_BACKEND) == BACKEND_SOLOIST
        ):
            # a Spotify account supports a single active Soloist session
            return 1
        # Spotify accounts tolerate two concurrent sessions (main + librespot)
        return 2

    @property
    def audiobooks_supported(self) -> bool:
        """Check if audiobooks are supported for this user/region."""
        return self._audiobooks_supported

    @property
    def audiobook_progress_sync_enabled(self) -> bool:
        """Check if audiobook progress sync is enabled."""
        return bool(self.config.get_value(CONF_SYNC_AUDIOBOOK_PROGRESS, False))

    @property
    def podcast_progress_sync_enabled(self) -> bool:
        """Check if played status sync is enabled."""
        value = self.config.get_value(CONF_SYNC_PODCAST_PROGRESS, True)
        return bool(value) if value is not None else True

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        features = self._supported_features.copy()
        # Add audiobook features if enabled
        if self.audiobooks_supported:
            features.add(ProviderFeature.LIBRARY_AUDIOBOOKS)
            features.add(ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT)
        return features

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        if self._sp_user:
            return str(self._sp_user["display_name"])
        return None

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this provider to include in diagnostics reports."""
        return {
            "logged_in": self._sp_user is not None,
            "token_expires_in_sec": (
                round(self._auth_info_global["expires_at"] - time.time())
                if self._auth_info_global
                else None
            ),
            "dev_session_active": self.dev_session_active,
            "playback_backend": str(self.get_setup_value(CONF_PLAYBACK_BACKEND) or "librespot"),
            "audiobooks_supported": self._audiobooks_supported,
            **(await self.backend.get_diagnostics() if hasattr(self, "backend") else {}),
        }

    ## Library retrieval methods (generators)
    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve library artists from spotify."""
        endpoint = "me/following"
        while True:
            spotify_artists = await self._get_data(
                endpoint,
                type="artist",
                limit=50,
            )
            for item in spotify_artists["artists"]["items"]:
                if item and item["id"]:
                    yield parse_artist(item, self)
            if spotify_artists["artists"]["next"]:
                endpoint = spotify_artists["artists"]["next"]
                endpoint = endpoint.replace("https://api.spotify.com/v1/", "")
            else:
                break

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve library albums from the provider."""
        async for item in self._get_all_items("me/albums"):
            if item["album"] and item["album"]["id"]:
                yield parse_album(item["album"], self)

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve library tracks from the provider."""
        async for item in self._get_all_items("me/tracks"):
            if item and item["track"] and item["track"]["id"]:
                yield parse_track(item["track"], self)

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Retrieve library podcasts from spotify."""
        async for item in self._get_all_items("me/shows"):
            if item["show"] and item["show"]["id"]:
                show_obj = item["show"]
                # Filter out audiobooks - they have a distinctive description format
                description = show_obj.get("description", "")
                if description.startswith("Author(s):") and "Narrator(s):" in description:
                    continue
                yield parse_podcast(show_obj, self)

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """Retrieve library audiobooks from spotify."""
        if not self.audiobooks_supported:
            return
        async for item in self._get_all_items("me/audiobooks"):
            if item and item["id"]:
                # Parse the basic audiobook
                audiobook = parse_audiobook(item, self)
                # Add chapters from Spotify API data
                await self._add_audiobook_chapters(audiobook)
                yield audiobook

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """
        Retrieve playlists from the provider.

        Note: We use the global session here because playlists like "Daily Mix"
        are only returned when using the non-dev (global) token.
        """
        yield await self._get_liked_songs_playlist()
        async for item in self._get_all_items("me/playlists", use_global_session=True):
            if item and item["id"]:
                yield parse_playlist(item, self)

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse Spotify items, including curated sections (new releases, genres & moods).

        :param path: The path to browse (e.g. provider_id:// or provider_id://new-releases).
        """
        path_parts = path.split("://")[1].split("/") if "://" in path else []
        subpath = path_parts[0] if path_parts else None
        sub_subpath = path_parts[1] if len(path_parts) > 1 else None
        locale = self.mass.metadata.locale

        if subpath == "new-releases":
            return await self._get_new_releases()

        if subpath == "categories" and sub_subpath:
            return await self._get_category_playlists(sub_subpath, locale)

        if subpath == "categories":
            return await self._get_categories(locale)

        # For root path, add curated folders on top of standard library folders.
        # At the root the path always ends in "://", so curated paths can be appended directly.
        if not subpath:
            curated: list[BrowseFolder] = [
                BrowseFolder(
                    item_id="new-releases",
                    provider=self.instance_id,
                    path=f"{path}new-releases",
                    name="New Releases",
                    translation_key="new_releases",
                    is_playable=True,
                ),
                BrowseFolder(
                    item_id="categories",
                    provider=self.instance_id,
                    path=f"{path}categories",
                    name="Genres & Moods",
                    translation_key="genres_and_moods",
                    is_playable=False,
                ),
            ]
            standard = await super().browse(path)
            return [*curated, *standard]

        return await super().browse(path)

    @use_cache()
    async def search(
        self, search_query: str, media_types: list[MediaType] | None = None, limit: int = 5
    ) -> SearchResults:
        """
        Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        searchresult = SearchResults()
        if media_types is None:
            return searchresult

        searchtype = self._build_search_types(media_types)
        if not searchtype:
            return searchresult

        search_query = search_query.replace("'", "")
        offset = 0
        page_limit = min(limit, 10)

        while True:
            api_result = await self._get_data(
                "search", q=search_query, type=searchtype, limit=page_limit, offset=offset
            )
            items_received = self._process_search_results(api_result, searchresult)

            offset += page_limit
            if offset >= limit or items_received < page_limit:
                break

        return searchresult

    @use_cache()
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        artist_obj = await self._get_data(f"artists/{prov_artist_id}")
        return parse_artist(artist_obj, self)

    @use_cache()
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        album_obj = await self._get_data(f"albums/{prov_album_id}")
        return parse_album(album_obj, self)

    @use_cache()
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        track_obj = await self._get_data(f"tracks/{prov_track_id}")
        return parse_track(track_obj, self)

    @use_cache()
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if prov_playlist_id == self._get_liked_songs_playlist_id():
            return await self._get_liked_songs_playlist()

        # Check cache to see if this playlist requires global token
        use_global = await self._playlist_requires_global_token(prov_playlist_id)
        if use_global:
            playlist_obj = await self._get_data(
                f"playlists/{prov_playlist_id}", use_global_session=True
            )
            return parse_playlist(playlist_obj, self)

        # Try with dev token first (if available), fallback to global on 400 error
        # Some playlists like Spotify-owned (Daily Mix) or Liked Songs only work with global token
        try:
            playlist_obj = await self._get_data(f"playlists/{prov_playlist_id}")
            return parse_playlist(playlist_obj, self)
        except MediaNotFoundError:
            if self.dev_session_active:
                # Remember that this playlist requires global token
                await self._set_playlist_requires_global_token(prov_playlist_id)
                playlist_obj = await self._get_data(
                    f"playlists/{prov_playlist_id}", use_global_session=True
                )
                return parse_playlist(playlist_obj, self)
            raise

    @use_cache()
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        podcast_obj = await self._get_data(f"shows/{prov_podcast_id}")
        if not podcast_obj:
            raise MediaNotFoundError(f"Podcast not found: {prov_podcast_id}")
        return parse_podcast(podcast_obj, self)

    @use_cache()
    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id."""
        if not self.audiobooks_supported:
            raise UnsupportedFeaturedException("Audiobooks are not supported with this account")

        audiobook_obj = await self._get_data(f"audiobooks/{prov_audiobook_id}")
        if not audiobook_obj:
            raise MediaNotFoundError(f"Audiobook not found: {prov_audiobook_id}")

        # Parse basic audiobook without chapters first
        audiobook = parse_audiobook(audiobook_obj, self)

        # Add chapters from Spotify API data
        await self._add_audiobook_chapters(audiobook)

        # Note: Resume position will be handled by MA's internal system
        # which calls get_resume_position() when needed

        return audiobook

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Get all podcast episodes."""
        podcast = await self.get_podcast(prov_podcast_id)

        # Get (cached) episode data
        episodes_data = await self._get_podcast_episodes_data(prov_podcast_id)

        # Parse and yield episodes with position
        for idx, episode_data in enumerate(episodes_data):
            episode = parse_podcast_episode(episode_data, self, podcast)
            episode.position = idx + 1

            # Set played status if sync is enabled and resume data exists
            if self.podcast_progress_sync_enabled and "resume_point" in episode_data:
                resume_point = episode_data["resume_point"]
                fully_played = resume_point.get("fully_played", False)
                position_ms = resume_point.get("resume_position_ms", 0)

                episode.fully_played = fully_played or None
                episode.resume_position_ms = position_ms if position_ms > 0 else None

            yield episode

    @use_cache(86400)  # 24 hours
    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get full podcast episode details by id."""
        episode_obj = await self._get_data(f"episodes/{prov_episode_id}", market="from_token")
        if not episode_obj:
            raise MediaNotFoundError(f"Episode not found: {prov_episode_id}")
        return parse_podcast_episode(episode_obj, self)

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """Get resume position for episode/audiobook from Spotify."""
        if media_type == MediaType.PODCAST_EPISODE:
            if not self.podcast_progress_sync_enabled:
                raise NotImplementedError("Spotify podcast resume sync disabled in settings")

            try:
                episode_obj = await self._get_data(f"episodes/{item_id}", market="from_token")
            except MediaNotFoundError:
                raise NotImplementedError("Episode not found on Spotify")
            except (ResourceTemporarilyUnavailable, aiohttp.ClientError) as e:
                self.logger.debug(f"Error fetching episode {item_id}: {e}")
                raise NotImplementedError("Unable to fetch episode data from Spotify")

            if (
                not episode_obj
                or "resume_point" not in episode_obj
                or not episode_obj["resume_point"]
            ):
                raise NotImplementedError("No resume point data from Spotify")

            resume_point = episode_obj["resume_point"]
            fully_played = resume_point.get("fully_played", False)
            position_ms = resume_point.get("resume_position_ms", 0)
            return fully_played, position_ms, None

        if media_type == MediaType.AUDIOBOOK:
            if not self.audiobooks_supported:
                raise NotImplementedError("Audiobook support is disabled")
            if not self.audiobook_progress_sync_enabled:
                raise NotImplementedError("Spotify audiobook resume sync disabled in settings")

            try:
                chapters_data = await self._get_audiobook_chapters_data(item_id)
                if not chapters_data:
                    raise NotImplementedError("No chapters data available")

                total_position_ms = 0
                fully_played = True

                for chapter in chapters_data:
                    resume_point = chapter.get("resume_point", {})
                    chapter_fully_played = resume_point.get("fully_played", False)
                    chapter_position_ms = resume_point.get("resume_position_ms", 0)

                    if chapter_fully_played:
                        total_position_ms += chapter.get("duration_ms", 0)
                    elif chapter_position_ms > 0:
                        total_position_ms += chapter_position_ms
                        fully_played = False
                        break
                    else:
                        fully_played = False
                        break

                return fully_played, total_position_ms, None

            except (MediaNotFoundError, ResourceTemporarilyUnavailable, aiohttp.ClientError) as e:
                self.logger.debug(f"Failed to get audiobook resume position for {item_id}: {e}")
                raise NotImplementedError("Unable to get audiobook resume position from Spotify")

        else:
            raise NotImplementedError(f"Resume position not supported for {media_type}")

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
        Call when an episode/audiobook is played in MA.

        MA automatically handles internal position tracking - this method is for
        provider-specific actions like syncing to external services.
        """
        if media_type == MediaType.PODCAST_EPISODE:
            if not isinstance(media_item, PodcastEpisode):
                return

            # Log the playback for monitoring/debugging
            safe_position = position or 0
            if media_item.duration > 0:
                completion_percentage = (safe_position / media_item.duration) * 100
            else:
                completion_percentage = 0

            self.logger.debug(
                f"Episode played: {prov_item_id} at {safe_position}s "
                f"({completion_percentage:.1f}%, fully_played: {fully_played})"
            )

            # Note: No API exists to sync playback position back to Spotify for episodes
            # MA handles all internal position tracking automatically

        elif media_type == MediaType.AUDIOBOOK:
            if not isinstance(media_item, Audiobook):
                return

            # Log the playback for monitoring/debugging
            safe_position = position or 0
            if media_item.duration > 0:
                completion_percentage = (safe_position / media_item.duration) * 100
            else:
                completion_percentage = 0

            self.logger.debug(
                f"Audiobook played: {prov_item_id} at {safe_position}s "
                f"({completion_percentage:.1f}%, fully_played: {fully_played})"
            )

            # Note: No API exists to sync playback position back to Spotify for audiobooks
            # MA handles all internal position tracking automatically

            # The resume position will be automatically updated by MA's internal tracking
            # and will be retrieved via get_audiobook() which combines MA + Spotify positions

    @use_cache(86400 * 365, allow_expired_cache=True)  # 1 year - album track listings are immutable
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all album tracks for given album id."""
        return [
            parse_track(item, self)
            async for item in self._get_all_items(f"albums/{prov_album_id}/tracks")
            if item["id"]
        ]

    @use_cache(3600 * 3, allow_expired_cache=True)  # 3 hours
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        is_liked_songs = prov_playlist_id == self._get_liked_songs_playlist_id()
        uri = "me/tracks" if is_liked_songs else f"playlists/{prov_playlist_id}/items"

        # Liked songs always require global session
        # For other playlists, call get_playlist first to trigger the fallback logic
        # and populate the cache for which token to use
        if is_liked_songs:
            use_global = True
        else:
            # This call is cached and will determine/cache if global token is needed
            await self.get_playlist(prov_playlist_id)
            use_global = await self._playlist_requires_global_token(prov_playlist_id)

        page_size = 50
        offset = page * page_size
        known_global = use_global

        while True:
            try:
                meta = await self._get_playlist_pagination_meta(uri, page, use_global)
                cache_checksum = meta["etag"]
                total = meta["total"]

                # Spotify has started returning 5xx for offset >= total on some
                # playlists (notably algorithmic ones like Daily Mix). The retry
                # storm that follows surfaces as "No playable items found".
                if total and offset >= total:
                    spotify_result = {"total": total, "items": []}
                else:
                    spotify_result = await self._get_data_with_caching(
                        uri,
                        cache_checksum,
                        limit=page_size,
                        offset=offset,
                        use_global_session=use_global,
                    )
                break
            except MediaNotFoundError:
                if use_global or not self.dev_session_active:
                    raise
                # Development Mode exposes metadata but restricts items for non-owned playlists.
                use_global = True

        if use_global and not known_global:
            await self._set_playlist_requires_global_token(prov_playlist_id)

        result: list[Track] = []
        total = spotify_result.get("total", 0)
        items = spotify_result.get("items", [])
        # playlists/{id}/items is transitioning from item["track"] to item["item"]
        # during Spotify's Feb 2026 rollout, so accept either shape.
        for index, item in enumerate(items, 1):
            # Spotify wraps/recycles items for offsets beyond the playlist size,
            # so we need to break when we've reached the total.
            if (offset + index) > total:
                break
            track_data = item and (item.get("item") or item.get("track"))
            if not (track_data and track_data.get("id")):
                continue
            track = parse_track(track_data, self)
            track.position = offset + index
            result.append(track)
        return result

    @use_cache(86400 * 14, allow_expired_cache=True)  # 14 days
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        try:
            return [
                parse_album(item, self)
                async for item in self._get_all_items(
                    f"artists/{prov_artist_id}/albums?include_groups=album,single,compilation",
                    limit=10,
                )
                if (item and item["id"])
            ]
        except MediaNotFoundError:
            self.logger.warning("Unable to fetch albums for artist %s", prov_artist_id)
            return []

    @use_cache(86400 * 14, allow_expired_cache=True)  # 14 days
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        try:
            artist = await self.get_artist(prov_artist_id)
            endpoint = f"artists/{prov_artist_id}/top-tracks"
            items = await self._get_data(endpoint)
            return [
                parse_track(item, self, artist=artist)
                for item in items["tracks"]
                if (item and item["id"])
            ]
        except MediaNotFoundError:
            self.logger.warning(
                "Top tracks search for artist %s appears to have been removed by Spotify for this account.",
                prov_artist_id,
            )
            return []

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to library."""
        uri_type_map = {
            MediaType.ARTIST: "artist",
            MediaType.ALBUM: "album",
            MediaType.TRACK: "track",
            MediaType.PLAYLIST: "playlist",
            MediaType.PODCAST: "show",
            MediaType.AUDIOBOOK: "audiobook",
        }
        if item.media_type == MediaType.AUDIOBOOK and not self.audiobooks_supported:
            return False
        uri_type = uri_type_map.get(item.media_type)
        if not uri_type:
            return False
        uri = f"spotify:{uri_type}:{item.item_id}"
        await self._put_data("me/library", uris=uri)
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from library."""
        uri_type_map = {
            MediaType.ARTIST: "artist",
            MediaType.ALBUM: "album",
            MediaType.TRACK: "track",
            MediaType.PLAYLIST: "playlist",
            MediaType.PODCAST: "show",
            MediaType.AUDIOBOOK: "audiobook",
        }
        if media_type == MediaType.AUDIOBOOK and not self.audiobooks_supported:
            return False
        uri_type = uri_type_map.get(media_type)
        if not uri_type:
            return False
        uri = f"spotify:{uri_type}:{prov_item_id}"
        await self._delete_data("me/library", uris=uri)
        return True

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        track_uris = [f"spotify:track:{track_id}" for track_id in prov_track_ids]
        data = {"uris": track_uris}
        await self._post_data(f"playlists/{prov_playlist_id}/items", data=data)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        track_uris = []
        for pos in positions_to_remove:
            uri = f"playlists/{prov_playlist_id}/items"
            spotify_result = await self._get_data(uri, limit=1, offset=pos - 1)
            for item in spotify_result["items"]:
                track_data = item and (item.get("item") or item.get("track"))
                if not (track_data and track_data.get("id")):
                    continue
                track_uris.append({"uri": f"spotify:track:{track_data['id']}"})
        data = {"items": track_uris}
        await self._delete_data(f"playlists/{prov_playlist_id}/items", data=data)

    async def create_playlist(self, name: str, media_types: set[MediaType]) -> Playlist:
        """Create a new playlist on provider with given name."""
        data = {"name": name, "public": False}
        new_playlist = await self._post_data("me/playlists", data=data)
        self._fix_create_playlist_api_bug(new_playlist)
        return parse_playlist(new_playlist, self)

    @use_cache(86400 * 14, allow_expired_cache=True)  # 14 days
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        # Recommendations endpoint is only available on global session (not developer API)
        # https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api
        endpoint = "recommendations"
        items = await self._get_data(
            endpoint, seed_tracks=prov_track_id, limit=limit, use_global_session=True
        )
        return [parse_track(item, self) for item in items["tracks"] if (item and item["id"])]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return content details for the given track/episode/audiobook when it will be streamed."""
        if media_type == MediaType.AUDIOBOOK and self.audiobooks_supported:
            chapters_data = await self._get_audiobook_chapters_data(item_id)
            if not chapters_data:
                raise MediaNotFoundError(f"No chapters found for audiobook {item_id}")

            # Calculate total duration and convert to seconds for StreamDetails
            total_duration_ms = sum(chapter.get("duration_ms", 0) for chapter in chapters_data)
            duration_seconds = total_duration_ms // 1000

            # Create chapter URIs for streaming
            chapter_uris = []
            for chapter in chapters_data:
                chapter_id = chapter["id"]
                chapter_uri = f"spotify:episode:{chapter_id}"
                chapter_uris.append(chapter_uri)

            return StreamDetails(
                item_id=item_id,
                provider=self.instance_id,
                media_type=MediaType.AUDIOBOOK,
                audio_format=self.backend.audio_format,
                stream_type=StreamType.CUSTOM,
                allow_seek=True,
                can_seek=True,
                duration=duration_seconds,
                data={"chapters": chapter_uris, "chapters_data": chapters_data},
            )

        # For all other media types (tracks, podcast episodes)
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            media_type=media_type,
            audio_format=self.backend.audio_format,
            stream_type=StreamType.CUSTOM,
            allow_seek=True,
            can_seek=True,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Get audio stream from Spotify via librespot."""
        if streamdetails.media_type == MediaType.AUDIOBOOK and isinstance(streamdetails.data, dict):
            chapter_uris = streamdetails.data.get("chapters", [])
            chapters_data = streamdetails.data.get("chapters_data", [])

            # Calculate which chapter to start from based on seek_position
            seek_position_ms = seek_position * 1000
            current_seek_ms = seek_position_ms
            start_chapter = 0

            if seek_position > 0 and chapters_data:
                accumulated_duration_ms = 0

                for i, chapter_data in enumerate(chapters_data):
                    chapter_duration_ms = chapter_data.get("duration_ms", 0)

                    if accumulated_duration_ms + chapter_duration_ms > seek_position_ms:
                        start_chapter = i
                        current_seek_ms = seek_position_ms - accumulated_duration_ms
                        break
                    accumulated_duration_ms += chapter_duration_ms
                else:
                    start_chapter = len(chapter_uris) - 1
                    current_seek_ms = 0

            # Convert back to seconds for librespot
            current_seek_seconds = int(current_seek_ms // 1000)

            # Stream chapters starting from the calculated position
            consecutive_failures = 0
            for i in range(start_chapter, len(chapter_uris)):
                chapter_uri = chapter_uris[i]
                chapter_seek = current_seek_seconds if i == start_chapter else 0

                try:
                    chunk_count = 0
                    async for chunk in self.backend.stream_spotify_uri(chapter_uri, chapter_seek):
                        yield chunk
                        chunk_count += 1
                    if chunk_count > 0:
                        consecutive_failures = 0
                except Exception as e:
                    self.logger.warning("Chapter %s streaming failed", i + 1)
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        raise AudioError("Audiobook streaming failed") from e
                    continue
        else:
            # Handle normal tracks and podcast episodes
            media_type = (
                "episode" if streamdetails.media_type == MediaType.PODCAST_EPISODE else "track"
            )
            spotify_uri = f"spotify:{media_type}:{streamdetails.item_id}"
            async for chunk in self.backend.stream_spotify_uri(spotify_uri, seek_position):
                yield chunk

    @lock
    async def login(self, force_refresh: bool = False) -> dict[str, Any]:
        """
        Log-in Spotify global session and return Auth/token info.

        This uses MA's global client ID which has full API access but heavy rate limits.
        """
        # return the cached access token while it is still valid (refreshed before expiry)
        if (
            not force_refresh
            and self._auth_info_global
            and (self._auth_info_global["expires_at"] > (time.time() + 600))
        ):
            return self._auth_info_global
        # read the refresh token from the persisted store rather than the in-memory config copy,
        # which can lag a rotation and would make us refresh with a stale (revoked) token
        if not (refresh_token := self._stored_refresh_token(CONF_REFRESH_TOKEN_GLOBAL)):
            raise LoginFailed("Authentication required")

        try:
            auth_info = await get_spotify_token(
                self.mass.http_session,
                app_var("spotify_client_id"),  # Always use MA's global client ID
                refresh_token,
                "global",
            )
            self.logger.debug("Successfully refreshed global access token")
        except LoginFailed as err:
            if "revoked" in str(err) or "invalid_grant" in str(err):
                # Spotify rotates the refresh token on refresh and revokes the previous one.
                # If the stored token was rotated while this refresh was in flight, the token
                # we tried is merely stale, so keep the newer one instead of forcing re-auth.
                if not self._refresh_token_superseded(CONF_REFRESH_TOKEN_GLOBAL, refresh_token):
                    self._update_setup_data(CONF_REFRESH_TOKEN_GLOBAL, None)
                    if self.available:
                        self.unload_with_error(err)
            elif self.available:
                self.mass.create_task(self.mass.unload_provider_with_error(self.instance_id, err))
            raise

        # make sure that our updated creds get stored in memory + config
        self._auth_info_global = auth_info
        # Spotify revokes the previous refresh token only when it rotates one, so on rotation
        # persist immediately to ensure the new token survives a crash within the debounced-save
        # window and avoids a forced re-auth; an unchanged token uses the normal debounced save.
        token_rotated = auth_info["refresh_token"] != refresh_token
        self._update_setup_data(
            CONF_REFRESH_TOKEN_GLOBAL,
            auth_info["refresh_token"],
            immediate=token_rotated,
        )

        # get logged-in user info
        if not self._sp_user:
            self._sp_user = userinfo = await self._get_data(
                "me", auth_info=auth_info, use_global_session=True
            )
            if country := userinfo.get("country"):
                self.mass.metadata.set_default_preferred_language(country)
            self.logger.info("Successfully logged in to Spotify as %s", userinfo["display_name"])
        return auth_info

    @lock
    async def login_dev(self, force_refresh: bool = False) -> dict[str, Any]:
        """
        Log-in Spotify developer session and return Auth/token info.

        This uses the user's custom client ID which has less rate limits but limited API access.
        """
        # return the cached access token while it is still valid (refreshed before expiry)
        if (
            not force_refresh
            and self._auth_info_dev
            and (self._auth_info_dev["expires_at"] > (time.time() + 600))
        ):
            return self._auth_info_dev
        # read the refresh token from the persisted store rather than the in-memory config copy,
        # which can lag a rotation and would make us refresh with a stale (revoked) token
        refresh_token = self._stored_refresh_token(CONF_REFRESH_TOKEN_DEV)
        client_id = self.get_setup_value(CONF_CLIENT_ID)
        if not refresh_token or not client_id:
            raise LoginFailed("Developer authentication not configured")

        try:
            auth_info = await get_spotify_token(
                self.mass.http_session,
                cast("str", client_id),
                refresh_token,
                "developer",
            )
            self.logger.debug("Successfully refreshed developer access token")
        except LoginFailed as err:
            if "revoked" in str(err) or "invalid_grant" in str(err):
                # Spotify rotates the refresh token on refresh and revokes the previous one.
                # If the stored token was rotated while this refresh was in flight, the token
                # we tried is merely stale, so keep the newer one instead of forcing re-auth.
                if not self._refresh_token_superseded(CONF_REFRESH_TOKEN_DEV, refresh_token):
                    self._update_setup_data(CONF_REFRESH_TOKEN_DEV, None)
                    self._update_setup_data(CONF_CLIENT_ID, None)
            # Don't unload - we can still use the global session
            self.dev_session_active = False
            self.logger.warning(str(err))
            raise

        # make sure that our updated creds get stored in memory + config
        self._auth_info_dev = auth_info
        # Spotify revokes the previous refresh token only when it rotates one, so on rotation
        # persist immediately to ensure the new token survives a crash within the debounced-save
        # window and avoids a forced re-auth; an unchanged token uses the normal debounced save.
        token_rotated = auth_info["refresh_token"] != refresh_token
        self._update_setup_data(
            CONF_REFRESH_TOKEN_DEV,
            auth_info["refresh_token"],
            immediate=token_rotated,
        )

        self.logger.info("Successfully logged in to Spotify developer session")
        return auth_info

    def _build_search_types(self, media_types: list[MediaType]) -> str:
        """Build comma-separated search types string from media types."""
        searchtypes = []
        if MediaType.ARTIST in media_types:
            searchtypes.append("artist")
        if MediaType.ALBUM in media_types:
            searchtypes.append("album")
        if MediaType.TRACK in media_types:
            searchtypes.append("track")
        if MediaType.PLAYLIST in media_types:
            searchtypes.append("playlist")
        if MediaType.PODCAST in media_types:
            searchtypes.append("show")
        if MediaType.AUDIOBOOK in media_types and self.audiobooks_supported:
            searchtypes.append("audiobook")
        return ",".join(searchtypes)

    def _process_search_results(
        self, api_result: dict[str, Any], searchresult: SearchResults
    ) -> int:
        """
        Process API search results and update searchresult object.

        Returns the total number of items received.
        """
        items_received = 0

        if "artists" in api_result:
            artists = [
                parse_artist(item, self)
                for item in api_result["artists"]["items"]
                if (item and item["id"] and item["name"])
            ]
            searchresult.artists = [*searchresult.artists, *artists]
            items_received += len(api_result["artists"]["items"])

        if "albums" in api_result:
            albums = [
                parse_album(item, self)
                for item in api_result["albums"]["items"]
                if (item and item["id"])
            ]
            searchresult.albums = [*searchresult.albums, *albums]
            items_received += len(api_result["albums"]["items"])

        if "tracks" in api_result:
            tracks = [
                parse_track(item, self)
                for item in api_result["tracks"]["items"]
                if (item and item["id"])
            ]
            searchresult.tracks = [*searchresult.tracks, *tracks]
            items_received += len(api_result["tracks"]["items"])

        if "playlists" in api_result:
            playlists = [
                parse_playlist(item, self)
                for item in api_result["playlists"]["items"]
                if (item and item["id"])
            ]
            searchresult.playlists = [*searchresult.playlists, *playlists]
            items_received += len(api_result["playlists"]["items"])

        if "shows" in api_result:
            podcasts = []
            for item in api_result["shows"]["items"]:
                if not (item and item["id"]):
                    continue
                # Filter out audiobooks - they have a distinctive description format
                description = item.get("description", "")
                if description.startswith("Author(s):") and "Narrator(s):" in description:
                    continue
                podcasts.append(parse_podcast(item, self))
            searchresult.podcasts = [*searchresult.podcasts, *podcasts]
            items_received += len(api_result["shows"]["items"])

        if "audiobooks" in api_result and self.audiobooks_supported:
            audiobooks = [
                parse_audiobook(item, self)
                for item in api_result["audiobooks"]["items"]
                if (item and item["id"])
            ]
            searchresult.audiobooks = [*searchresult.audiobooks, *audiobooks]
            items_received += len(api_result["audiobooks"]["items"])

        return items_received

    def _create_backend(self) -> SpotifyPlaybackBackend:
        """Return the playback backend selected by this instance's configuration."""
        if self.get_setup_value(CONF_PLAYBACK_BACKEND) == BACKEND_SOLOIST:
            return SoloistSingleTrackBackend(self)
        return LibrespotBackend(self)

    def _remove_soloist_session_dir(self) -> None:
        """Remove a leftover paired soloist session (blocking)."""
        session_dir = self._instance_storage_dir / SOLOIST_DATA_DIR_NAME
        if session_dir.is_dir():
            self.logger.debug("Removing leftover soloist session at %s", session_dir)
            shutil.rmtree(session_dir, ignore_errors=True)

    def _remove_instance_storage(self) -> None:
        """Remove this instance's storage dir (blocking)."""
        shutil.rmtree(self._instance_storage_dir, ignore_errors=True)

    @property
    def _instance_storage_dir(self) -> Path:
        """Return this instance's private storage directory."""
        return Path(self.mass.storage_path) / "spotify" / self.instance_id

    async def _get_auth_info(self, use_global_session: bool = False) -> dict[str, Any]:
        """
        Get auth info for API requests, preferring dev session if available.

        :param use_global_session: Force use of global session (for features not available on dev).
        """
        if use_global_session or not self.dev_session_active:
            return await self.login()

        # Try dev session first
        try:
            return await self.login_dev()
        except LoginFailed:
            # Fall back to global session
            self.logger.debug("Falling back to global session after dev session failure")
            return await self.login()

    def _get_liked_songs_playlist_id(self) -> str:
        return f"{LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX}-{self.instance_id}"

    @use_cache(86400, allow_expired_cache=True)  # 24h; serve stale + refresh in background
    async def _get_new_releases(self) -> list[Album]:
        """Get Spotify's curated 'new releases' albums."""
        try:
            result = await self._get_data("browse/new-releases", limit=50)
        except MediaNotFoundError:
            return []
        return [
            parse_album(item, self)
            for item in result.get("albums", {}).get("items", [])
            if item and item.get("id")
        ]

    @use_cache(86400 * 7, allow_expired_cache=True)  # 7d; serve stale + refresh in background
    async def _get_categories(self, locale: str) -> list[BrowseFolder]:
        """Get Spotify's curated browse categories (genres & moods) as browse folders."""
        try:
            result = await self._get_data("browse/categories", locale=locale, limit=50)
        except MediaNotFoundError:
            return []
        return [
            BrowseFolder(
                item_id=cat["id"],
                provider=self.instance_id,
                path=f"{self.instance_id}://categories/{cat['id']}",
                name=cat["name"],
                is_playable=False,
            )
            for cat in result.get("categories", {}).get("items", [])
            if cat and cat.get("id") and cat.get("name")
        ]

    @use_cache(86400, allow_expired_cache=True)  # 24h; serve stale + refresh in background
    async def _get_category_playlists(self, category_id: str, locale: str) -> list[Playlist]:
        """Get the playlists for a single Spotify browse category."""
        try:
            result = await self._get_data(
                f"browse/categories/{category_id}/playlists",
                locale=locale,
                limit=50,
                use_global_session=True,
            )
        except MediaNotFoundError:
            return []
        return [
            parse_playlist(item, self)
            for item in result.get("playlists", {}).get("items", [])
            if item and item.get("id") and item.get("name")
        ]

    async def _get_liked_songs_playlist(self) -> Playlist:
        if self._sp_user is None:
            raise LoginFailed("User info not available - not logged in")

        liked_songs = Playlist(
            item_id=self._get_liked_songs_playlist_id(),
            provider=self.instance_id,
            name=f"Liked Songs {self._sp_user['display_name']}",
            translation_key="liked_songs",
            translation_params=[self._sp_user["display_name"]],
            owner=self._sp_user["display_name"],
            provider_mappings={
                ProviderMapping(
                    item_id=self._get_liked_songs_playlist_id(),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url="https://open.spotify.com/collection/tracks",
                    is_unique=True,  # liked songs is user-specific
                )
            },
        )

        liked_songs.is_editable = False  # TODO Editing requires special endpoints

        # Add image to the playlist metadata
        image = MediaItemImage(
            type=ImageType.THUMB,
            path="https://misc.scdn.co/liked-songs/liked-songs-64.png",
            provider=self.instance_id,
            remotely_accessible=True,
        )
        if liked_songs.metadata.images is None:
            liked_songs.metadata.images = UniqueList([image])
        else:
            liked_songs.metadata.add_image(image)

        return liked_songs

    async def _get_playlist_pagination_meta(
        self, endpoint: str, page: int, use_global_session: bool
    ) -> dict[str, Any]:
        """
        Return pagination metadata for a Spotify playlist traversal.

        :param endpoint: Spotify API endpoint for the playlist items.
        :param page: Requested playlist page.
        :param use_global_session: Whether the global Spotify session is required.
        """
        state_key = (endpoint, use_global_session)
        if state := self._playlist_pagination_states.get(state_key):
            self._playlist_pagination_states.move_to_end(state_key)
        else:
            state = _PlaylistPaginationState(lock=asyncio.Lock())
            self._playlist_pagination_states[state_key] = state
            while len(self._playlist_pagination_states) > _PLAYLIST_PAGINATION_STATE_LIMIT:
                self._playlist_pagination_states.popitem(last=False)

        observed_snapshot = state.snapshot
        async with state.lock:
            snapshot = state.snapshot
            # A concurrent page may have populated this snapshot while this call waited.
            if snapshot and (page > 0 or snapshot is not observed_snapshot):
                return snapshot

            if page == 0:
                state.snapshot = None
            meta = await self._get_paginated_meta(
                endpoint,
                limit=1,
                offset=0,
                use_global_session=use_global_session,
            )
            state.snapshot = meta
            return meta

    async def _playlist_requires_global_token(self, prov_playlist_id: str) -> bool:
        """
        Check if a playlist requires global token (cached).

        :param prov_playlist_id: The Spotify playlist ID.
        :returns: True if the playlist requires global token.
        """
        cache_key = f"playlist_global_token_{prov_playlist_id}"
        return bool(await self.mass.cache.get(cache_key, provider=self.instance_id))

    async def _set_playlist_requires_global_token(self, prov_playlist_id: str) -> None:
        """
        Mark a playlist as requiring global token in cache.

        :param prov_playlist_id: The Spotify playlist ID.
        """
        cache_key = f"playlist_global_token_{prov_playlist_id}"
        # Cache for 90 days - playlist ownership doesn't change
        await self.mass.cache.set(cache_key, True, provider=self.instance_id, expiration=86400 * 90)

    async def _add_audiobook_chapters(self, audiobook: Audiobook) -> None:
        """Add chapter metadata to an audiobook from Spotify API data."""
        try:
            chapters_data = await self._get_audiobook_chapters_data(audiobook.item_id)
            if chapters_data:
                chapters = []
                total_duration_seconds = 0.0

                for idx, chapter in enumerate(chapters_data):
                    duration_ms = chapter.get("duration_ms", 0)
                    duration_seconds = duration_ms / 1000.0

                    chapter_obj = MediaItemChapter(
                        position=idx + 1,
                        name=chapter.get("name", f"Chapter {idx + 1}"),
                        start=total_duration_seconds,
                        end=total_duration_seconds + duration_seconds,
                    )
                    chapters.append(chapter_obj)
                    total_duration_seconds += duration_seconds

                audiobook.metadata.chapters = chapters
                audiobook.duration = int(total_duration_seconds)

        except (MediaNotFoundError, ResourceTemporarilyUnavailable, ProviderUnavailableError) as e:
            self.logger.warning(f"Failed to get chapters for audiobook {audiobook.item_id}: {e}")

    @use_cache(43200)  # 12 hours - balances freshness with performance
    async def _get_podcast_episodes_data(self, prov_podcast_id: str) -> list[dict[str, Any]]:
        """
        Get raw episode data from Spotify API (cached).

        :param prov_podcast_id: Spotify podcast ID.
        """
        episodes_data: list[dict[str, Any]] = []

        try:
            async for item in self._get_all_items(
                f"shows/{prov_podcast_id}/episodes", market="from_token"
            ):
                if item and item.get("id"):
                    episodes_data.append(item)
        except MediaNotFoundError:
            self.logger.warning("Podcast %s not found", prov_podcast_id)
            return []
        except ResourceTemporarilyUnavailable as err:
            self.logger.warning(
                "Temporary error fetching episodes for %s: %s", prov_podcast_id, err
            )
            raise

        return episodes_data

    @use_cache(7200)  # 2 hours - shorter cache for resume point data
    async def _get_audiobook_chapters_data(self, prov_audiobook_id: str) -> list[dict[str, Any]]:
        """
        Get raw chapter data from Spotify API (cached).

        :param prov_audiobook_id: Spotify audiobook ID.
        """
        chapters_data: list[dict[str, Any]] = []

        try:
            async for item in self._get_all_items(
                f"audiobooks/{prov_audiobook_id}/chapters", market="from_token"
            ):
                if item and item.get("id"):
                    chapters_data.append(item)
        except MediaNotFoundError:
            self.logger.warning("Audiobook %s not found", prov_audiobook_id)
            return []
        except ResourceTemporarilyUnavailable as err:
            self.logger.warning(
                "Temporary error fetching chapters for %s: %s", prov_audiobook_id, err
            )
            raise

        return chapters_data

    async def _get_all_items(
        self, endpoint: str, key: str = "items", limit: int = 50, **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any]]:
        """Get all items from a paged list."""
        offset = 0
        # single request to fetch the etag (used as cache checksum) and total
        meta = await self._get_cached_paginated_meta(endpoint, limit=1, offset=0, **kwargs)
        cache_checksum = meta["etag"]
        total = meta["total"]
        while True:
            # Avoid requesting beyond the known end. Spotify can return 5xx
            # for offset >= total on some endpoints (e.g. algorithmic playlists).
            if total and offset >= total:
                break
            result = await self._get_data_with_caching(
                endpoint, cache_checksum=cache_checksum, limit=limit, offset=offset, **kwargs
            )
            offset += limit
            if not result or key not in result or not result[key]:
                break
            for item in result[key]:
                yield item
            if len(result[key]) < limit:
                break

    async def _get_data_with_caching(
        self, endpoint: str, cache_checksum: str | None, **kwargs: Any
    ) -> dict[str, Any]:
        """Get data from api with caching."""
        cache_key_parts = [endpoint]
        for key in sorted(kwargs.keys()):
            cache_key_parts.append(f"{key}{kwargs[key]}")
        cache_key = ".".join(map(str, cache_key_parts))
        if cached := await self.mass.cache.get(
            cache_key, provider=self.instance_id, checksum=cache_checksum, allow_bypass=False
        ):
            return cast("dict[str, Any]", cached)
        result = await self._get_data(endpoint, **kwargs)
        await self.mass.cache.set(
            cache_key, result, provider=self.instance_id, checksum=cache_checksum
        )
        return result

    @use_cache(120, allow_bypass=False)  # short cache: repeated traversals reuse metadata
    async def _get_cached_paginated_meta(self, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """Get cached pagination metadata for a paginated API endpoint."""
        return await self._get_paginated_meta(endpoint, **kwargs)

    async def _get_paginated_meta(self, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """Get etag and total item count for a paginated api endpoint."""
        _res = await self._get_data(endpoint, **kwargs)
        return {"etag": _res.get("etag"), "total": _res.get("total", 0)}

    @throttle_with_retries
    async def _get_data(self, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """
        Get data from api.

        :param endpoint: API endpoint to call.
        :param use_global_session: Force use of global session (for features not available on dev).
        """
        url = f"https://api.spotify.com/v1/{endpoint}"
        kwargs["market"] = "from_token"
        kwargs["country"] = "from_token"
        use_global_session = kwargs.pop("use_global_session", False)
        if not (auth_info := kwargs.pop("auth_info", None)):
            auth_info = await self._get_auth_info(use_global_session=use_global_session)
        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
        locale = self.mass.metadata.locale.replace("_", "-")
        language = locale.split("-")[0]
        headers["Accept-Language"] = f"{locale}, {language};q=0.9, *;q=0.5"
        self.logger.debug("handling get data %s with kwargs %s", url, kwargs)
        async with (
            self.mass.http_session.get(
                url,
                headers=headers,
                params=kwargs,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as response,
        ):
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise RateLimited("Spotify Rate Limiter", backoff_time=backoff_time)
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)

            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                if use_global_session or not self.dev_session_active:
                    self._auth_info_global = None
                else:
                    self._auth_info_dev = None
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=1)

            if response.status in (400, 403, 404):
                try:
                    error = await response.json(loads=json_loads)
                    message = error.get("error", {}).get("message") or response.reason
                except aiohttp.ContentTypeError, JSONDecodeError:
                    message = (await response.text()) or response.reason

                self.logger.debug(
                    "Spotify API error: endpoint=%s, status=%s, reason=%s, message=%s",
                    endpoint,
                    response.status,
                    response.reason,
                    message,
                )

                raise MediaNotFoundError(f"{endpoint} not found")

            response.raise_for_status()
            result: dict[str, Any] = await response.json(loads=json_loads)
            if etag := response.headers.get("ETag"):
                result["etag"] = etag
            return result

    @throttle_with_retries
    async def _delete_data(self, endpoint: str, data: Any = None, **kwargs: Any) -> None:
        """Delete data from api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        use_global_session = kwargs.pop("use_global_session", False)
        if not (auth_info := kwargs.pop("auth_info", None)):
            auth_info = await self._get_auth_info(use_global_session=use_global_session)
        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
        async with self.mass.http_session.delete(
            url, headers=headers, params=kwargs, json=data, ssl=True
        ) as response:
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise RateLimited("Spotify Rate Limiter", backoff_time=backoff_time)
            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                if use_global_session or not self.dev_session_active:
                    self._auth_info_global = None
                else:
                    self._auth_info_dev = None
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=1)
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()

    @throttle_with_retries
    async def _put_data(self, endpoint: str, data: Any = None, **kwargs: Any) -> None:
        """Put data on api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        use_global_session = kwargs.pop("use_global_session", False)
        if not (auth_info := kwargs.pop("auth_info", None)):
            auth_info = await self._get_auth_info(use_global_session=use_global_session)
        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
        async with self.mass.http_session.put(
            url, headers=headers, params=kwargs, json=data, ssl=True
        ) as response:
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise RateLimited("Spotify Rate Limiter", backoff_time=backoff_time)
            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                if use_global_session or not self.dev_session_active:
                    self._auth_info_global = None
                else:
                    self._auth_info_dev = None
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=1)

            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()

    @throttle_with_retries
    async def _post_data(
        self, endpoint: str, data: Any = None, want_result: bool = True, **kwargs: Any
    ) -> dict[str, Any]:
        """Post data on api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        use_global_session = kwargs.pop("use_global_session", False)
        if not (auth_info := kwargs.pop("auth_info", None)):
            auth_info = await self._get_auth_info(use_global_session=use_global_session)
        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
        async with self.mass.http_session.post(
            url, headers=headers, params=kwargs, json=data, ssl=True
        ) as response:
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise RateLimited("Spotify Rate Limiter", backoff_time=backoff_time)
            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                if use_global_session or not self.dev_session_active:
                    self._auth_info_global = None
                else:
                    self._auth_info_dev = None
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=1)
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()
            if not want_result:
                return {}
            result: dict[str, Any] = await response.json(loads=json_loads)
            return result

    def _fix_create_playlist_api_bug(self, playlist_obj: dict[str, Any]) -> None:
        """Fix spotify API bug where incorrect owner id is returned from Create Playlist."""
        if self._sp_user is None:
            raise LoginFailed("User info not available - not logged in")

        if playlist_obj["owner"]["id"] != self._sp_user["id"]:
            playlist_obj["owner"]["id"] = self._sp_user["id"]
            playlist_obj["owner"]["display_name"] = self._sp_user["display_name"]
        else:
            self.logger.warning(
                "FIXME: Spotify have fixed their Create Playlist API, this fix can be removed."
            )

    async def _test_audiobook_support(self) -> bool:
        """Test if audiobooks are supported in user's region."""
        try:
            await self._get_data("me/audiobooks", limit=1)
            return True
        except aiohttp.ClientResponseError as e:
            if e.status == 403:
                return False  # Not available
            raise  # Re-raise other HTTP errors
        except MediaNotFoundError, ProviderUnavailableError:
            return False

    def _stored_refresh_token(self, key: str) -> str | None:
        """
        Return the currently persisted refresh token, or None if not set.

        Reads through the live setup_data (kept in sync with a just-rotated token) so a
        refresh never uses a stale, revoked token from a lagging in-memory config copy.

        :param key: Setup data key of the refresh token to read.
        """
        token = self.get_setup_value(key)
        return cast("str", token) if token else None

    def _refresh_token_superseded(self, key: str, used_token: str) -> bool:
        """
        Return whether the stored refresh token differs from the one just used.

        :param key: Config key of the refresh token to check.
        :param used_token: The refresh token value that was just used to refresh.
        """
        stored_token = self._stored_refresh_token(key)
        if not stored_token:
            return False
        return stored_token != used_token
