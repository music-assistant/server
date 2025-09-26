"""Main Spotify provider implementation."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    AudioFormat,
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

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.app_vars import app_var  # type: ignore[attr-defined]
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.process import check_output
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.helpers.util import lock
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    CONF_CLIENT_ID,
    CONF_REFRESH_TOKEN,
    CONF_SYNC_AUDIOBOOK_PROGRESS,
    CONF_SYNC_PODCAST_PROGRESS,
    LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX,
)
from .helpers import get_librespot_binary
from .parsers import (
    parse_album,
    parse_artist,
    parse_audiobook,
    parse_playlist,
    parse_podcast,
    parse_podcast_episode,
    parse_track,
)
from .streaming import LibrespotStreamer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant


class SpotifyProvider(MusicProvider):
    """Implementation of a Spotify MusicProvider."""

    _auth_info: dict[str, Any] | None = None
    _sp_user: dict[str, Any] | None = None
    _librespot_bin: str | None = None
    custom_client_id_active: bool = False
    throttler: ThrottlerManager

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config)
        self._base_supported_features = supported_features

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.cache_dir = os.path.join(self.mass.cache_path, self.instance_id)
        self.throttler = ThrottlerManager(rate_limit=1, period=2)
        self.streamer = LibrespotStreamer(self)
        if self.config.get_value(CONF_CLIENT_ID):
            # loosen the throttler a bit when a custom client id is used
            self.throttler = ThrottlerManager(rate_limit=45, period=30)
            self.custom_client_id_active = True
        # check if we have a librespot binary for this arch
        self._librespot_bin = await get_librespot_binary()
        # try login which will raise if it fails
        await self.login()
        self._audiobooks_supported = await self._test_audiobook_support()
        if not self._audiobooks_supported:
            self.logger.info(
                "Audiobook support disabled: Audiobooks are not available in your region. "
                "See https://support.spotify.com/us/authors/article/audiobooks-availability/ "
                "for supported countries."
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
        except (MediaNotFoundError, ProviderUnavailableError):
            return False

    @property
    def audiobooks_supported(self) -> bool:
        """Check if audiobooks are supported for this user/region."""
        return getattr(self, "_audiobooks_supported", False)

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
        features = self._base_supported_features.copy()
        # Add audiobook features if enabled
        if self.audiobooks_supported:
            features.add(ProviderFeature.LIBRARY_AUDIOBOOKS)
            features.add(ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT)

        if not self.custom_client_id_active:
            # Spotify has killed the similar tracks api for developers
            # https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api
            return {*features, ProviderFeature.SIMILAR_TRACKS}

        return features

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        if self._sp_user:
            return str(self._sp_user["display_name"])
        return None

    # ruff: noqa: PLR0915
    async def search(
        self, search_query: str, media_types: list[MediaType] | None = None, limit: int = 5
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        searchresult = SearchResults()
        searchtypes = []
        if media_types is None:
            return searchresult
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
        if not searchtypes:
            return searchresult
        searchtype = ",".join(searchtypes)
        search_query = search_query.replace("'", "")
        offset = 0
        page_limit = min(limit, 50)
        while True:
            items_received = 0
            api_result = await self._get_data(
                "search", q=search_query, type=searchtype, limit=page_limit, offset=offset
            )
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
            offset += page_limit
            if offset >= limit:
                break
            if items_received < page_limit:
                break
        return searchresult

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
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

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        async for item in self._get_all_items("me/albums"):
            if item["album"] and item["album"]["id"]:
                yield parse_album(item["album"], self)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        async for item in self._get_all_items("me/tracks"):
            if item and item["track"]["id"]:
                yield parse_track(item["track"], self)

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Retrieve library podcasts from spotify."""
        async for item in self._get_all_items("me/shows"):
            if item["show"] and item["show"]["id"]:
                show_obj = item["show"]
                # Filter out audiobooks - they have a distinctive description format
                description = show_obj.get("description", "")
                if description.startswith("Author(s):") and "Narrator(s):" in description:
                    continue
                yield parse_podcast(show_obj, self)

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook, None]:
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

    def _get_liked_songs_playlist_id(self) -> str:
        return f"{LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX}-{self.instance_id}"

    async def _get_liked_songs_playlist(self) -> Playlist:
        if self._sp_user is None:
            raise LoginFailed("User info not available - not logged in")

        liked_songs = Playlist(
            item_id=self._get_liked_songs_playlist_id(),
            provider=self.lookup_key,
            name=f"Liked Songs {self._sp_user['display_name']}",  # TODO to be translated
            owner=self._sp_user["display_name"],
            provider_mappings={
                ProviderMapping(
                    item_id=self._get_liked_songs_playlist_id(),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url="https://open.spotify.com/collection/tracks",
                )
            },
        )

        liked_songs.is_editable = False  # TODO Editing requires special endpoints

        # Add image to the playlist metadata
        image = MediaItemImage(
            type=ImageType.THUMB,
            path="https://misc.scdn.co/liked-songs/liked-songs-64.png",
            provider=self.lookup_key,
            remotely_accessible=True,
        )
        if liked_songs.metadata.images is None:
            liked_songs.metadata.images = UniqueList([image])
        else:
            liked_songs.metadata.add_image(image)

        liked_songs.cache_checksum = str(time.time())

        return liked_songs

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve playlists from the provider."""
        yield await self._get_liked_songs_playlist()
        async for item in self._get_all_items("me/playlists"):
            if item and item["id"]:
                yield parse_playlist(item, self)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        artist_obj = await self._get_data(f"artists/{prov_artist_id}")
        return parse_artist(artist_obj, self)

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        album_obj = await self._get_data(f"albums/{prov_album_id}")
        return parse_album(album_obj, self)

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        track_obj = await self._get_data(f"tracks/{prov_track_id}")
        return parse_track(track_obj, self)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if prov_playlist_id == self._get_liked_songs_playlist_id():
            return await self._get_liked_songs_playlist()

        playlist_obj = await self._get_data(f"playlists/{prov_playlist_id}")
        return parse_playlist(playlist_obj, self)

    @use_cache(86400)  # 24 hours
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        podcast_obj = await self._get_data(f"shows/{prov_podcast_id}")
        if not podcast_obj:
            raise MediaNotFoundError(f"Podcast not found: {prov_podcast_id}")
        return parse_podcast(podcast_obj, self)

    @use_cache(86400)  # 24 hours
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
        """Get raw episode data from Spotify API (cached).

        Args:
            prov_podcast_id: Spotify podcast ID

        Returns:
            List of episode data dictionaries
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
        """Get raw chapter data from Spotify API (cached).

        Args:
            prov_audiobook_id: Spotify audiobook ID

        Returns:
            List of chapter data dictionaries
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

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get all podcast episodes."""
        # Get podcast object for context if available
        podcast: Podcast | None = None
        try:
            podcast = await self.mass.music.podcasts.get_provider_item(
                prov_podcast_id, self.instance_id
            )
        except MediaNotFoundError:
            # If not in MA library, get it via API (this is cached)
            try:
                podcast = await self.get_podcast(prov_podcast_id)
            except MediaNotFoundError:
                self.logger.warning(
                    "Podcast with ID %s is no longer available on Spotify", prov_podcast_id
                )

        # Get cached episode data
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

                episode.fully_played = fully_played if fully_played else None
                episode.resume_position_ms = position_ms if position_ms > 0 else None

            yield episode

    @use_cache(86400)  # 24 hours
    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get full podcast episode details by id."""
        episode_obj = await self._get_data(f"episodes/{prov_episode_id}", market="from_token")
        if not episode_obj:
            raise MediaNotFoundError(f"Episode not found: {prov_episode_id}")
        return parse_podcast_episode(episode_obj, self)

    async def get_resume_position(self, item_id: str, media_type: MediaType) -> tuple[bool, int]:
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
            return fully_played, position_ms

        elif media_type == MediaType.AUDIOBOOK:
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

                return fully_played, total_position_ms

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

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all album tracks for given album id."""
        return [
            parse_track(item, self)
            async for item in self._get_all_items(f"albums/{prov_album_id}/tracks")
            if item["id"]
        ]

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        uri = (
            "me/tracks"
            if prov_playlist_id == self._get_liked_songs_playlist_id()
            else f"playlists/{prov_playlist_id}/tracks"
        )
        page_size = 50
        offset = page * page_size
        spotify_result = await self._get_data(uri, limit=page_size, offset=offset)
        for index, item in enumerate(spotify_result["items"], 1):
            if not (item and item["track"] and item["track"]["id"]):
                continue
            # use count as position
            track = parse_track(item["track"], self)
            track.position = offset + index
            result.append(track)
        return result

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        return [
            parse_album(item, self)
            async for item in self._get_all_items(
                f"artists/{prov_artist_id}/albums?include_groups=album,single,compilation"
            )
            if (item and item["id"])
        ]

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        artist = await self.get_artist(prov_artist_id)
        endpoint = f"artists/{prov_artist_id}/top-tracks"
        items = await self._get_data(endpoint)
        return [
            parse_track(item, self, artist=artist)
            for item in items["tracks"]
            if (item and item["id"])
        ]

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to library."""
        if item.media_type == MediaType.ARTIST:
            await self._put_data("me/following", {"ids": [item.item_id]}, type="artist")
        elif item.media_type == MediaType.ALBUM:
            await self._put_data("me/albums", {"ids": [item.item_id]})
        elif item.media_type == MediaType.TRACK:
            await self._put_data("me/tracks", {"ids": [item.item_id]})
        elif item.media_type == MediaType.PLAYLIST:
            await self._put_data(f"playlists/{item.item_id}/followers", data={"public": False})
        elif item.media_type == MediaType.PODCAST:
            await self._put_data("me/shows", ids=item.item_id)
        elif item.media_type == MediaType.AUDIOBOOK and self.audiobooks_supported:
            # For audiobooks, we need special handling to ensure chapter metadata is included
            self.logger.info(f"Adding audiobook {item.item_id} to library with chapter metadata")

            # First add to Spotify library
            await self._put_data("me/audiobooks", ids=item.item_id)

            # Then get the full audiobook metadata with chapters by calling our get_audiobook method
            try:
                full_audiobook = await self.get_audiobook(item.item_id)

                # Update the audiobook in MA's database with the full metadata including chapters
                # This ensures when the user plays it from library, it has chapter information
                await self.mass.music.audiobooks.add_item_to_library(full_audiobook)
                self.logger.info(
                    f"Updated audiobook {item.item_id} in MA database with chapter metadata"
                )

            except MediaNotFoundError as e:
                self.logger.warning(
                    f"Audiobook {item.item_id} not found when fetching chapter metadata: {e}"
                )
            except ResourceTemporarilyUnavailable as e:
                self.logger.warning(
                    "Spotify temporarily unavailable when "
                    f"fetching audiobook {item.item_id} metadata: {e}"
                )
            except ProviderUnavailableError as e:
                self.logger.warning(
                    f"Provider unavailable when fetching audiobook {item.item_id} metadata: {e}"
                )
            except Exception as e:
                # Catch any other unexpected errors
                self.logger.warning(
                    f"Unexpected error fetching audiobook {item.item_id} metadata: {e}"
                )
                self.logger.debug(f"Full error details: {e}", exc_info=True)
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from library."""
        if media_type == MediaType.ARTIST:
            await self._delete_data("me/following", {"ids": [prov_item_id]}, type="artist")
        elif media_type == MediaType.ALBUM:
            await self._delete_data("me/albums", {"ids": [prov_item_id]})
        elif media_type == MediaType.TRACK:
            await self._delete_data("me/tracks", {"ids": [prov_item_id]})
        elif media_type == MediaType.PLAYLIST:
            await self._delete_data(f"playlists/{prov_item_id}/followers")
        elif media_type == MediaType.PODCAST:
            await self._delete_data("me/shows", ids=prov_item_id)
        elif media_type == MediaType.AUDIOBOOK and self.audiobooks_supported:
            await self._delete_data("me/audiobooks", ids=prov_item_id)
        return True

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        track_uris = [f"spotify:track:{track_id}" for track_id in prov_track_ids]
        data = {"uris": track_uris}
        await self._post_data(f"playlists/{prov_playlist_id}/tracks", data=data)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        track_uris = []
        for pos in positions_to_remove:
            uri = f"playlists/{prov_playlist_id}/tracks"
            spotify_result = await self._get_data(uri, limit=1, offset=pos - 1)
            for item in spotify_result["items"]:
                if not (item and item["track"] and item["track"]["id"]):
                    continue
                track_uris.append({"uri": f"spotify:track:{item['track']['id']}"})
        data = {"tracks": track_uris}
        await self._delete_data(f"playlists/{prov_playlist_id}/tracks", data=data)

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        if self._sp_user is None:
            raise LoginFailed("User info not available - not logged in")
        data = {"name": name, "public": False}
        new_playlist = await self._post_data(f"users/{self._sp_user['id']}/playlists", data=data)
        self._fix_create_playlist_api_bug(new_playlist)
        return parse_playlist(new_playlist, self)

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        endpoint = "recommendations"
        items = await self._get_data(endpoint, seed_tracks=prov_track_id, limit=limit)
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
                chapter_uri = f"spotify://episode:{chapter_id}"
                chapter_uris.append(chapter_uri)

            return StreamDetails(
                item_id=item_id,
                provider=self.lookup_key,
                media_type=MediaType.AUDIOBOOK,
                audio_format=AudioFormat(content_type=ContentType.OGG, bit_rate=320),
                stream_type=StreamType.CUSTOM,
                allow_seek=True,
                can_seek=True,
                duration=duration_seconds,
                data={"chapters": chapter_uris, "chapters_data": chapters_data},
            )

        # For all other media types (tracks, podcast episodes)
        return StreamDetails(
            item_id=item_id,
            provider=self.lookup_key,
            media_type=media_type,
            audio_format=AudioFormat(content_type=ContentType.OGG, bit_rate=320),
            stream_type=StreamType.CUSTOM,
            allow_seek=True,
            can_seek=True,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
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
            for i in range(start_chapter, len(chapter_uris)):
                chapter_uri = chapter_uris[i]
                chapter_seek = current_seek_seconds if i == start_chapter else 0

                try:
                    async for chunk in self.streamer.stream_spotify_uri(chapter_uri, chapter_seek):
                        yield chunk
                except Exception as e:
                    self.logger.error(f"Chapter {i + 1} streaming failed: {e}")
                    continue
        else:
            # Handle normal tracks and podcast episodes
            async for chunk in self.streamer.get_audio_stream(streamdetails, seek_position):
                yield chunk

    @lock
    async def login(self, force_refresh: bool = False) -> dict[str, Any]:
        """Log-in Spotify and return Auth/token info."""
        # return existing token if we have one in memory
        if (
            not force_refresh
            and self._auth_info
            and (self._auth_info["expires_at"] > (time.time() - 600))
        ):
            return self._auth_info
        # request new access token using the refresh token
        if not (refresh_token := self.config.get_value(CONF_REFRESH_TOKEN)):
            raise LoginFailed("Authentication required")

        client_id = self.config.get_value(CONF_CLIENT_ID) or app_var(2)
        params = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        for _ in range(2):
            async with self.mass.http_session.post(
                "https://accounts.spotify.com/api/token", data=params
            ) as response:
                if response.status != 200:
                    err = await response.text()
                    if "revoked" in err:
                        err_msg = f"Failed to refresh access token: {err}"
                        # clear refresh token if it's invalid
                        self.update_config_value(CONF_REFRESH_TOKEN, None)
                        if self.available:
                            # If we're already loaded, we need to unload and set an error
                            self.unload_with_error(err_msg)
                        raise LoginFailed(err_msg)
                    # the token failed to refresh, we allow one retry
                    await asyncio.sleep(2)
                    continue
                # if we reached this point, the token has been successfully refreshed
                auth_info: dict[str, Any] = await response.json()
                auth_info["expires_at"] = int(auth_info["expires_in"] + time.time())
                self.logger.debug("Successfully refreshed access token")
                break
        else:
            if self.available:
                self.mass.create_task(
                    self.mass.unload_provider_with_error(
                        self.instance_id, f"Failed to refresh access token: {err}"
                    )
                )
            raise LoginFailed(f"Failed to refresh access token: {err}")

        # make sure that our updated creds get stored in memory + config
        self._auth_info = auth_info
        self.update_config_value(CONF_REFRESH_TOKEN, auth_info["refresh_token"], encrypted=True)
        # check if librespot still has valid auth
        if self._librespot_bin is None:
            raise LoginFailed("Librespot binary not available")
        args = [
            self._librespot_bin,
            "--cache",
            self.cache_dir,
            "--check-auth",
        ]
        ret_code, stdout = await check_output(*args)
        if ret_code != 0:
            # cached librespot creds are invalid, re-authenticate
            # we can use the check-token option to send a new token to librespot
            # librespot will then get its own token from spotify (somehow) and cache that.
            args += [
                "--access-token",
                auth_info["access_token"],
            ]
            ret_code, stdout = await check_output(*args)
            if ret_code != 0:
                # this should not happen, but guard it just in case
                err = stdout.decode("utf-8").strip()
                raise LoginFailed(f"Failed to verify credentials on Librespot: {err}")

        # get logged-in user info
        if not self._sp_user:
            self._sp_user = userinfo = await self._get_data("me", auth_info=auth_info)
            self.mass.metadata.set_default_preferred_language(userinfo["country"])
            self.logger.info("Successfully logged in to Spotify as %s", userinfo["display_name"])
        return auth_info

    async def _get_all_items(
        self, endpoint: str, key: str = "items", **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Get all items from a paged list."""
        limit = 50
        offset = 0
        while True:
            kwargs["limit"] = limit
            kwargs["offset"] = offset
            result = await self._get_data(endpoint, **kwargs)
            offset += limit
            if not result or key not in result or not result[key]:
                break
            for item in result[key]:
                yield item
            if len(result[key]) < limit:
                break

    @throttle_with_retries
    async def _get_data(self, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """Get data from api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        kwargs["market"] = "from_token"
        kwargs["country"] = "from_token"
        if not (auth_info := kwargs.pop("auth_info", None)):
            auth_info = await self.login()
        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
        locale = self.mass.metadata.locale.replace("_", "-")
        language = locale.split("-")[0]
        headers["Accept-Language"] = f"{locale}, {language};q=0.9, *;q=0.5"
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
                raise ResourceTemporarilyUnavailable(
                    "Spotify Rate Limiter", backoff_time=backoff_time
                )
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)

            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                self._auth_info = None
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=1)

            # handle 404 not found, convert to MediaNotFoundError
            if response.status == 404:
                raise MediaNotFoundError(f"{endpoint} not found")
            response.raise_for_status()
            result: dict[str, Any] = await response.json(loads=json_loads)
            return result

    @throttle_with_retries
    async def _delete_data(self, endpoint: str, data: Any = None, **kwargs: Any) -> None:
        """Delete data from api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        auth_info = kwargs.pop("auth_info", await self.login())
        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
        async with self.mass.http_session.delete(
            url, headers=headers, params=kwargs, json=data, ssl=True
        ) as response:
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise ResourceTemporarilyUnavailable(
                    "Spotify Rate Limiter", backoff_time=backoff_time
                )
            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                self._auth_info = None
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=1)
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()

    @throttle_with_retries
    async def _put_data(self, endpoint: str, data: Any = None, **kwargs: Any) -> None:
        """Put data on api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        auth_info = kwargs.pop("auth_info", await self.login())
        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
        async with self.mass.http_session.put(
            url, headers=headers, params=kwargs, json=data, ssl=True
        ) as response:
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise ResourceTemporarilyUnavailable(
                    "Spotify Rate Limiter", backoff_time=backoff_time
                )
            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                self._auth_info = None
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=1)

            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()

    @throttle_with_retries
    async def _post_data(self, endpoint: str, data: Any = None, **kwargs: Any) -> dict[str, Any]:
        """Post data on api."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        auth_info = kwargs.pop("auth_info", await self.login())
        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
        async with self.mass.http_session.post(
            url, headers=headers, params=kwargs, json=data, ssl=True
        ) as response:
            # handle spotify rate limiter
            if response.status == 429:
                backoff_time = int(response.headers["Retry-After"])
                raise ResourceTemporarilyUnavailable(
                    "Spotify Rate Limiter", backoff_time=backoff_time
                )
            # handle token expired, raise ResourceTemporarilyUnavailable
            # so it will be retried (and the token refreshed)
            if response.status == 401:
                self._auth_info = None
                raise ResourceTemporarilyUnavailable("Token expired", backoff_time=1)
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()
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
