"""Spotify Provider class implementation."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import (
    ImageType,
    MediaType,
    ProviderFeature,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
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

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.app_vars import app_var  # type: ignore[attr-defined]
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.process import check_output
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.helpers.util import lock
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    BASE_FEATURES,
    CACHE_CATEGORY_EPISODES,
    CACHE_CATEGORY_OTHER,
    CACHE_CATEGORY_PODCASTS,
    CACHE_CATEGORY_RECOMMENDATIONS,
    CACHE_KEY_USER_INFO,
    CONF_CLIENT_ID,
    CONF_ENABLE_PODCASTS,
    CONF_PLAYED_THRESHOLD,
    CONF_REFRESH_TOKEN,
    CONF_SYNC_PLAYED_STATUS,
    LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX,
    MEDIA_TYPE_TO_SEARCH,
    PODCAST_FEATURES,
)
from .helpers import get_librespot_binary
from .parsers import parse_album, parse_artist, parse_playlist, parse_podcast, parse_track
from .podcast_helpers import PodcastManager
from .streaming import LibrespotStreamer

if TYPE_CHECKING:
    from aiohttp import ClientResponse
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant import MusicAssistant


class SpotifyProvider(MusicProvider):
    """Implementation of a Spotify MusicProvider with Podcast support - Modular Architecture."""

    _auth_info: dict[str, Any] | None = None
    _sp_user: dict[str, Any] | None = None
    _librespot_bin: str | None = None
    custom_client_id_active: bool = False
    throttler: ThrottlerManager
    podcasts_enabled: bool = True

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
    ) -> None:
        """Initialize the provider and its managers."""
        super().__init__(mass, manifest, config)
        # Initialize managers
        self.podcast_manager = PodcastManager(self)
        self.streamer = LibrespotStreamer(self)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.cache_dir = os.path.join(self.mass.cache_path, self.instance_id)

        # Cast the config value to bool
        self.podcasts_enabled = bool(self.config.get_value(CONF_ENABLE_PODCASTS, True))

        # Check client ID and create throttler with appropriate settings
        if self.config.get_value(CONF_CLIENT_ID):
            # Create throttler with looser limits when custom client id is used
            self.throttler = ThrottlerManager(rate_limit=45, period=30)
            self.custom_client_id_active = True
        else:
            # Default throttler settings
            self.throttler = ThrottlerManager(rate_limit=1, period=2)

        # check if we have a librespot binary for this arch
        self._librespot_bin = await get_librespot_binary()
        # try login which will raise if it fails
        await self.login()

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        features = BASE_FEATURES.copy()

        if not self.custom_client_id_active:
            features.add(ProviderFeature.SIMILAR_TRACKS)

        if self.podcasts_enabled:
            features.update(PODCAST_FEATURES)

        return features

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        return self._sp_user["display_name"] if self._sp_user else None

    @property
    def sync_played_status_enabled(self) -> bool:
        """Check if played status sync is enabled."""
        value = self.config.get_value(CONF_SYNC_PLAYED_STATUS, True)
        if isinstance(value, bool):
            return value
        # Handle other types that could be truthy/falsy
        return bool(value) if value is not None else True

    @property
    def played_threshold(self) -> float:
        """Get the played threshold percentage."""
        value = self.config.get_value(CONF_PLAYED_THRESHOLD, 90)
        if isinstance(value, (int, float)):
            # Convert from 1-100 percentage to 0.0-1.0 decimal
            return float(value) / 100.0
        elif isinstance(value, str):
            try:
                return float(value) / 100.0
            except ValueError:
                return 0.9  # fallback to default (90%)
        else:
            return 0.9  # fallback to default for any other type

    # Streaming methods (delegate to streamer)
    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track/episode when it will be streamed."""
        return self.streamer.get_stream_details(item_id, media_type)

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        async for chunk in self.streamer.get_audio_stream(streamdetails, seek_position):
            yield chunk

    # Search functionality
    async def search(
        self, search_query: str, media_types: list[MediaType] | None = None, limit: int = 5
    ) -> SearchResults:
        """Perform search on musicprovider with caching considerations."""
        if media_types is None:
            media_types = list(MediaType)  # Default to all media types

        searchresult = SearchResults()

        # Build search types based on enabled features and requested media types
        searchtypes = []
        for media_type in media_types:
            media_type_str = media_type.value.upper()
            if media_type_str in MEDIA_TYPE_TO_SEARCH:
                if media_type == MediaType.PODCAST and not self.podcasts_enabled:
                    continue
                searchtypes.append(MEDIA_TYPE_TO_SEARCH[media_type_str])

        if not searchtypes:
            return searchresult

        searchtype = ",".join(searchtypes)
        search_query = search_query.replace("'", "")

        # Paginated search
        offset = 0
        page_limit = min(limit, 50)
        while True:
            items_received = 0
            api_result = await self._get_data(
                "search", q=search_query, type=searchtype, limit=page_limit, offset=offset
            )

            if not api_result:
                # No data returned, break out of pagination loop
                break

            # Process results using mapping
            if "artists" in api_result and api_result["artists"]["items"]:
                searchresult.artists = list(searchresult.artists) + [
                    parse_artist(item, self)
                    for item in api_result["artists"]["items"]
                    if item and item.get("id")
                ]
                items_received += len(api_result["artists"]["items"])

            if "albums" in api_result and api_result["albums"]["items"]:
                searchresult.albums = list(searchresult.albums) + [
                    parse_album(item, self)
                    for item in api_result["albums"]["items"]
                    if item and item.get("id")
                ]
                items_received += len(api_result["albums"]["items"])

            if "tracks" in api_result and api_result["tracks"]["items"]:
                searchresult.tracks = list(searchresult.tracks) + [
                    parse_track(item, self)
                    for item in api_result["tracks"]["items"]
                    if item and item.get("id")
                ]
                items_received += len(api_result["tracks"]["items"])

            if "playlists" in api_result and api_result["playlists"]["items"]:
                searchresult.playlists = list(searchresult.playlists) + [
                    parse_playlist(item, self)
                    for item in api_result["playlists"]["items"]
                    if item and item.get("id")
                ]
                items_received += len(api_result["playlists"]["items"])

            if self.podcasts_enabled and "shows" in api_result and api_result["shows"]["items"]:
                podcasts_to_add: list[Podcast | ItemMapping] = []
                for item in api_result["shows"]["items"]:
                    if item and item.get("id"):
                        try:
                            podcast = parse_podcast(item, self)
                            podcasts_to_add.append(podcast)
                        except Exception as e:
                            self.logger.warning(f"Failed to parse podcast {item.get('id')}: {e}")

                searchresult.podcasts = list(searchresult.podcasts) + podcasts_to_add
                items_received += len(api_result["shows"]["items"])

            # These lines MUST be inside the while loop and at this indentation level
            offset += page_limit
            if offset >= limit or items_received < page_limit:
                break

        return searchresult

    # Library management methods
    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to library with cache invalidation."""
        self.logger.info(f"Adding {item.media_type} with ID {item.item_id} to library")

        endpoint_mapping = {
            MediaType.ARTIST: ("me/following", {"ids": [item.item_id]}, {"type": "artist"}),
            MediaType.ALBUM: ("me/albums", {"ids": [item.item_id]}, {}),
            MediaType.TRACK: ("me/tracks", {"ids": [item.item_id]}, {}),
            MediaType.PLAYLIST: (f"playlists/{item.item_id}/followers", {"public": False}, {}),
        }

        # Handle podcast separately to avoid None issues
        if item.media_type == MediaType.PODCAST:
            if not self.podcasts_enabled:
                raise ValueError("Podcast support is disabled")
            endpoint_mapping[MediaType.PODCAST] = ("me/shows", {"ids": [item.item_id]}, {})

        if item.media_type not in endpoint_mapping:
            raise ValueError(f"Unsupported media type for library add: {item.media_type}")

        endpoint, data, params = endpoint_mapping[item.media_type]

        try:
            await self._put_data(endpoint, data, **params)
            self.logger.info(f"Successfully added {item.media_type} {item.item_id} to library")

            # Invalidate cache for podcasts to refresh library
            if item.media_type == MediaType.PODCAST:
                await self.podcast_manager._cache_invalidate_podcast(item.item_id)
                # Warm cache for the newly added podcast
                self.mass.create_task(self.podcast_manager.warm_podcast_cache(item.item_id))

            return True
        except Exception as e:
            self.logger.error(f"Failed to add {item.media_type} {item.item_id}: {e}")
            raise

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from library with cache invalidation."""
        endpoint_mapping = {
            MediaType.ARTIST: ("me/following", {"ids": [prov_item_id]}, {"type": "artist"}),
            MediaType.ALBUM: ("me/albums", {"ids": [prov_item_id]}, {}),
            MediaType.TRACK: ("me/tracks", {"ids": [prov_item_id]}, {}),
            MediaType.PLAYLIST: (f"playlists/{prov_item_id}/followers", {}, {}),
        }

        # Handle podcast separately
        if media_type == MediaType.PODCAST:
            if not self.podcasts_enabled:
                raise ValueError("Podcast support is disabled")
            endpoint_mapping[MediaType.PODCAST] = ("me/shows", {}, {"ids": prov_item_id})

        if media_type not in endpoint_mapping:
            raise ValueError(f"Unsupported media type for library remove: {media_type}")

        endpoint, data, params = endpoint_mapping[media_type]
        await self._delete_data(endpoint, data if data else None, **params)

        # Invalidate cache for podcasts
        if media_type == MediaType.PODCAST:
            await self.podcast_manager._cache_invalidate_podcast(prov_item_id)

        return True

    # Library retrieval methods
    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from spotify."""
        endpoint = "me/following"
        while True:
            spotify_artists = await self._get_data(endpoint, type="artist", limit=50)

            # Check if result is None or doesn't have the expected structure
            if not spotify_artists or "artists" not in spotify_artists:
                break

            for item in spotify_artists["artists"]["items"]:
                if item and item["id"]:
                    yield parse_artist(item, self)

            if spotify_artists["artists"]["next"]:
                endpoint = spotify_artists["artists"]["next"].replace(
                    "https://api.spotify.com/v1/", ""
                )
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

    def _get_liked_songs_playlist_id(self) -> str:
        return f"{LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX}-{self.instance_id}"

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Retrieve library podcasts from spotify with metadata caching."""
        if not self.podcasts_enabled:
            return

        async for item in self._get_all_items("me/shows"):
            if item["show"] and item["show"]["id"]:
                podcast_id = item["show"]["id"]

                # Try to get from cache first
                cached_podcast = await self.podcast_manager._cache_get_podcast(podcast_id)
                if cached_podcast:
                    yield parse_podcast(cached_podcast, self)
                else:
                    # Parse and cache
                    podcast = parse_podcast(item["show"], self)
                    await self.podcast_manager._cache_set_podcast(podcast_id, item["show"])
                    yield podcast

    async def _get_liked_songs_playlist(self) -> Playlist:
        display_name = self._sp_user["display_name"] if self._sp_user else "Unknown User"

        liked_songs = Playlist(
            item_id=self._get_liked_songs_playlist_id(),
            provider=self.lookup_key,
            name=f"Liked Songs {display_name}",
            owner=display_name,
            provider_mappings={
                ProviderMapping(
                    item_id=self._get_liked_songs_playlist_id(),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url="https://open.spotify.com/collection/tracks",
                )
            },
        )

        liked_songs.is_editable = False
        liked_songs.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path="https://misc.scdn.co/liked-songs/liked-songs-64.png",
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            ]
        )
        liked_songs.cache_checksum = str(time.time())
        return liked_songs

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve playlists from the provider."""
        yield await self._get_liked_songs_playlist()
        async for item in self._get_all_items("me/playlists"):
            if item and item["id"]:
                yield parse_playlist(item, self)

    # Individual item retrieval methods
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        artist_obj = await self._get_data(f"artists/{prov_artist_id}")
        if not artist_obj:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
        return parse_artist(artist_obj, self)

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        album_obj = await self._get_data(f"albums/{prov_album_id}")
        if not album_obj:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        return parse_album(album_obj, self)

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        track_obj = await self._get_data(f"tracks/{prov_track_id}")
        if not track_obj:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")
        return parse_track(track_obj, self)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if prov_playlist_id == self._get_liked_songs_playlist_id():
            return await self._get_liked_songs_playlist()

        playlist_obj = await self._get_data(f"playlists/{prov_playlist_id}")
        if not playlist_obj:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found")
        return parse_playlist(playlist_obj, self)

    # Podcast methods (delegate to podcast manager)
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        return await self.podcast_manager.get_podcast(prov_podcast_id)

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get podcast episodes."""
        async for episode in self.podcast_manager.get_podcast_episodes(prov_podcast_id):
            yield episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get full podcast episode details by id."""
        return await self.podcast_manager.get_podcast_episode(prov_episode_id)

    async def get_resume_position(self, item_id: str, media_type: MediaType) -> tuple[bool, int]:
        """Get resume position for episode from Spotify."""
        return await self.podcast_manager.get_resume_position(item_id, media_type)

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: Any,
        is_playing: bool = False,
    ) -> None:
        """Call when an item is played in MA."""
        await self.podcast_manager.on_played(
            media_type, prov_item_id, fully_played, position, media_item, is_playing
        )

    # Collection methods
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

        try:
            spotify_result = await self._get_data(uri, limit=page_size, offset=offset)
        except MediaNotFoundError:
            # Playlist not found, return empty list instead of failing
            self.logger.warning(f"Playlist {prov_playlist_id} not found on Spotify")
            return result

        # Add null check before accessing dictionary keys
        if not spotify_result or "items" not in spotify_result:
            self.logger.warning(f"No data returned for playlist {prov_playlist_id}")
            return result

        for index, item in enumerate(spotify_result["items"], 1):
            if not (item and item["track"] and item["track"]["id"]):
                continue
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

        # Add null check before accessing dictionary keys
        if not items or "tracks" not in items:
            return []

        return [
            parse_track(item, self, artist=artist)
            for item in items["tracks"]
            if (item and item["id"])
        ]

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        endpoint = "recommendations"
        items = await self._get_data(endpoint, seed_tracks=prov_track_id, limit=limit)

        # Add null check before accessing dictionary keys
        if not items or "tracks" not in items:
            return []

        return [parse_track(item, self) for item in items["tracks"] if (item and item["id"])]

    # Playlist modification methods
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
            # Add null check before accessing dictionary keys
            if not spotify_result or "items" not in spotify_result:
                continue
            for item in spotify_result["items"]:
                if not (item and item["track"] and item["track"]["id"]):
                    continue
                track_uris.append({"uri": f"spotify:track:{item['track']['id']}"})
        data = {"tracks": track_uris}
        await self._delete_data(f"playlists/{prov_playlist_id}/tracks", data=data)

    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        if not self._sp_user or not self._sp_user.get("id"):
            raise RuntimeError("User information not available or missing user ID")

        data = {"name": name, "public": False}
        new_playlist = await self._post_data(f"users/{self._sp_user['id']}/playlists", data=data)
        if not new_playlist:
            raise RuntimeError("Failed to create playlist - no data returned from Spotify")
        self._fix_create_playlist_api_bug(new_playlist)
        return parse_playlist(new_playlist, self)

    # Enhanced recommendations with caching
    @use_cache(3600)  # Cache recommendations for 1 hour
    async def get_recommendations(self) -> dict[str, Any]:
        """Get cached recommendations from Spotify."""
        if self.custom_client_id_active:
            return {}  # No recommendations with custom client ID

        try:
            # Get featured playlists
            featured = await self._get_data("browse/featured-playlists", limit=20)
            # Get new releases
            new_releases = await self._get_data("browse/new-releases", limit=20)

            return {
                "featured_playlists": featured.get("playlists", {}).get("items", [])
                if featured
                else [],
                "new_releases": new_releases.get("albums", {}).get("items", [])
                if new_releases
                else [],
            }
        except Exception as e:
            self.logger.warning(f"Failed to get recommendations: {e}")
            return {}

    # Library sync with cache warming
    async def sync_library(self, media_type: MediaType) -> None:
        """Sync library with cache warming for podcasts."""
        await super().sync_library(media_type)

        if media_type == MediaType.PODCAST and self.podcasts_enabled:
            # Warm cache for library podcasts in background
            self.mass.create_task(self.podcast_manager.warm_library_podcast_cache())

    # HTTP error handling
    def _handle_http_response_errors(self, response: ClientResponse) -> None:
        """Handle common HTTP response errors."""
        self.logger.debug(f"HTTP response status: {response.status} for URL: {response.url}")

        if response.status == 429:
            backoff_time = int(response.headers.get("Retry-After", 60))
            self.logger.warning(f"Spotify rate limit hit, backing off for {backoff_time} seconds")
            raise ResourceTemporarilyUnavailable("Spotify Rate Limiter", backoff_time=backoff_time)
        if response.status == 401:
            self.logger.warning("Spotify token expired, will refresh")
            self._auth_info = None
            # backoff_time was 0.05 but mypy indicated this needs to be an int
            # so this was changed to 1
            raise ResourceTemporarilyUnavailable("Token expired", backoff_time=1)
        if response.status in (502, 503):
            self.logger.warning(f"Spotify server error: {response.status}")
            raise ResourceTemporarilyUnavailable(backoff_time=30)
        if response.status == 404:
            self.logger.warning(f"Spotify resource not found: {response.url}")
            raise MediaNotFoundError("Resource not found")

    # Unified HTTP request method
    @throttle_with_retries
    async def _make_request(
        self, method: str, endpoint: str, data: Any | None = None, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Unified HTTP request method."""
        url = f"https://api.spotify.com/v1/{endpoint}"
        kwargs["market"] = "from_token"
        kwargs["country"] = "from_token"

        if not (auth_info := kwargs.pop("auth_info", None)):
            auth_info = await self.login()

        headers = {"Authorization": f"Bearer {auth_info['access_token']}"}
        locale = self.mass.metadata.locale.replace("_", "-")
        language = locale.split("-")[0]
        headers["Accept-Language"] = f"{locale}, {language};q=0.9, *;q=0.5"

        request_kwargs = {"headers": headers, "ssl": method.upper() == "GET", "timeout": 120}

        if method.upper() == "GET":
            request_kwargs["params"] = kwargs
        else:
            request_kwargs["params"] = kwargs
            if data:
                request_kwargs["json"] = data

        async with getattr(self.mass.http_session, method.lower())(
            url, **request_kwargs
        ) as response:
            self._handle_http_response_errors(response)
            response.raise_for_status()

            if method.upper() in ("GET", "POST"):
                result = await response.json(loads=json_loads)
                if isinstance(result, dict):
                    return cast("dict[str, Any]", result)
                else:
                    # Handle unexpected response format
                    self.logger.warning(f"Unexpected response format: {type(result)}")
                    return {}
            return None

    # API methods using unified request handler
    async def _get_data(self, endpoint: str, **kwargs: Any) -> dict[str, Any] | None:
        """Get data from api."""
        return await self._make_request("GET", endpoint, **kwargs)

    async def _post_data(
        self, endpoint: str, data: Any = None, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Post data to api."""
        return await self._make_request("POST", endpoint, data, **kwargs)

    async def _put_data(self, endpoint: str, data: Any = None, **kwargs: Any) -> None:
        """Put data to api."""
        await self._make_request("PUT", endpoint, data, **kwargs)

    async def _delete_data(self, endpoint: str, data: Any = None, **kwargs: Any) -> None:
        """Delete data from api."""
        await self._make_request("DELETE", endpoint, data, **kwargs)

    # Authentication and utility methods
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
                auth_info = await response.json()
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

        args = [self._librespot_bin, "--cache", self.cache_dir, "--check-auth"]
        ret_code, stdout = await check_output(*args)
        if ret_code != 0:
            # cached librespot creds are invalid, re-authenticate
            args += ["--access-token", auth_info["access_token"]]
            ret_code, stdout = await check_output(*args)
            if ret_code != 0:
                # this should not happen, but guard it just in case
                err = stdout.decode("utf-8").strip()
                raise LoginFailed(f"Failed to verify credentials on Librespot: {err}")

        # get logged-in user info and cache it
        if not self._sp_user:
            userinfo = await self._get_data("me", auth_info=auth_info)
            if userinfo is None:
                raise LoginFailed("Failed to get user info from Spotify")

            self._sp_user = userinfo

            # Cache user info
            await self.mass.cache.set(
                key=CACHE_KEY_USER_INFO,
                base_key=self.lookup_key,
                category=CACHE_CATEGORY_OTHER,
                data=userinfo,
                expiration=60 * 60 * 24,  # 1 day
            )

            self.mass.metadata.set_default_preferred_language(userinfo["country"])
            self.logger.info("Successfully logged in to Spotify as %s", userinfo["display_name"])
        return cast("dict[str, Any]", auth_info)

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

    def _fix_create_playlist_api_bug(self, playlist_obj: dict[str, Any]) -> None:
        """Fix spotify API bug where incorrect owner id is returned from Create Playlist."""
        if self._sp_user is None:
            self.logger.warning("Cannot fix playlist API bug: user info not available")
            return

        if playlist_obj["owner"]["id"] != self._sp_user["id"]:
            playlist_obj["owner"]["id"] = self._sp_user["id"]
            playlist_obj["owner"]["display_name"] = self._sp_user["display_name"]
        else:
            self.logger.warning(
                "FIXME: Spotify have fixed their Create Playlist API, this fix can be removed."
            )

    # Cache management
    async def clear_cache(self) -> None:
        """Clear all cached data for this provider."""
        try:
            # Clear all categories
            for category in [
                CACHE_CATEGORY_PODCASTS,
                CACHE_CATEGORY_EPISODES,
                CACHE_CATEGORY_RECOMMENDATIONS,
                CACHE_CATEGORY_OTHER,
            ]:
                await self.mass.cache.clear(base_key_filter=self.lookup_key, category=category)
            self.logger.info("Successfully cleared all cached data")
        except Exception as e:
            self.logger.warning(f"Failed to clear cache: {e}")

    async def get_cache_stats(self) -> dict[str, Any]:
        """Get cache statistics for monitoring."""
        try:
            return {
                "podcasts_cached": 0,
                "episodes_cached": 0,
                "cache_categories": ["PODCASTS", "EPISODES", "RECOMMENDATIONS", "OTHER"],
            }
        except Exception as e:
            self.logger.warning(f"Failed to get cache stats: {e}")
            return {}
