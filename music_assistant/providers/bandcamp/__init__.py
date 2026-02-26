"""Bandcamp music provider support for MusicAssistant."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import cast

from bandcamp_async_api import (
    BandcampAPIClient,
    BandcampAPIError,
    BandcampMustBeLoggedInError,
    BandcampNotFoundError,
    BandcampRateLimitError,
    SearchResultAlbum,
    SearchResultArtist,
    SearchResultTrack,
)
from bandcamp_async_api.models import BCAlbum, BCTrack, CollectionType
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import (
    ConfigEntryType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import Album, Artist, AudioFormat, SearchResults, Track
from music_assistant_models.provider import ProviderManifest
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.music_provider import MusicProvider

from .converters import BandcampConverters

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
}

CONF_IDENTITY = "identity"
CONF_TOP_TRACKS_LIMIT = "top_tracks_limit"
DEFAULT_TOP_TRACKS_LIMIT = 50
CACHE = 3600 * 24 * 30  # Cache for 30 days


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return BandcampProvider(mass, manifest, config, SUPPORTED_FEATURES)


# noinspection PyTypeHints,PyUnusedLocal
async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_IDENTITY,
            type=ConfigEntryType.SECURE_STRING,
            label="Identity token",
            required=False,
            description="Identity token from Bandcamp cookies for account collection access."
            " Log in https://bandcamp.com and extract browser cookie named 'identity'.",
            value=values.get(CONF_IDENTITY) if values else None,
        ),
        ConfigEntry(
            key=CONF_TOP_TRACKS_LIMIT,
            type=ConfigEntryType.INTEGER,
            label="Artist Top Tracks search limit",
            required=False,
            description="Search limit while getting artist top tracks.",
            value=values.get(CONF_TOP_TRACKS_LIMIT) if values else DEFAULT_TOP_TRACKS_LIMIT,
            default_value=DEFAULT_TOP_TRACKS_LIMIT,
            advanced=True,
        ),
    )


def split_id(id_: str) -> tuple[int, int, int]:
    """Return (artist_id, album_id, track_id). Missing parts are returned as 0.

    :param id_: Compound ID string, e.g. "123-456-789".
    :raises InvalidDataError: If the ID contains non-numeric parts.
    """
    try:
        parts = id_.split("-")
        part_0 = int(parts[0])
        part_1 = int(parts[1]) if len(parts) > 1 else 0
        part_2 = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError) as error:
        raise InvalidDataError(f"Malformed Bandcamp ID: {id_}") from error
    return part_0, part_1, part_2


class BandcampProvider(MusicProvider):
    """Bandcamp provider support."""

    _client: BandcampAPIClient
    _converters: BandcampConverters
    throttler: ThrottlerManager = ThrottlerManager(
        rate_limit=50,  # requests per period seconds
        period=10,
        initial_backoff=3,  # Bandcamp responds with Retry-After 3
        retry_attempts=10,
    )
    top_tracks_limit: int

    async def handle_async_init(self) -> None:
        """Handle async init of the Bandcamp provider."""
        identity = self.config.get_value(CONF_IDENTITY)
        self.top_tracks_limit = cast(
            "int", self.config.get_value(CONF_TOP_TRACKS_LIMIT, DEFAULT_TOP_TRACKS_LIMIT)
        )
        self._client = BandcampAPIClient(
            session=self.mass.http_session,
            identity_token=identity,
            default_retry_after=3,  # Bandcamp responds with Retry-After 3
        )
        self._converters = BandcampConverters(self.domain, self.instance_id)

        # The provider can function without login (search and streaming),
        # but if credentials were explicitly configured, validate them now.
        # A bad login fails hard so the user can fix it immediately;
        # transient errors (rate limits, network) are logged and the provider
        # continues since the login may still be valid.
        if identity:
            try:
                await self._client.get_collection_summary()
            except BandcampMustBeLoggedInError as error:
                raise LoginFailed("Bandcamp login is invalid or expired.") from error
            except BandcampAPIError as error:
                self.logger.warning("Could not validate Bandcamp login: %s", error)

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    @use_cache(CACHE)
    @throttle_with_retries
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 50
    ) -> SearchResults:
        """Perform search on music provider."""
        results = SearchResults()
        if not media_types:
            return results

        try:
            search_results = await self._client.search(search_query)
        except BandcampNotFoundError as error:
            raise MediaNotFoundError("No results for Bandcamp search") from error
        except BandcampRateLimitError as error:
            raise ResourceTemporarilyUnavailable(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error
        except BandcampAPIError as error:
            raise InvalidDataError("Unexpected error during Bandcamp search") from error

        for item in search_results[:limit]:
            try:
                if isinstance(item, SearchResultTrack) and MediaType.TRACK in media_types:
                    results.tracks = [*results.tracks, self._converters.track_from_search(item)]
                elif isinstance(item, SearchResultAlbum) and MediaType.ALBUM in media_types:
                    results.albums = [*results.albums, self._converters.album_from_search(item)]
                elif isinstance(item, SearchResultArtist) and MediaType.ARTIST in media_types:
                    results.artists = [*results.artists, self._converters.artist_from_search(item)]
            except BandcampAPIError as error:
                self.logger.warning("Failed to convert search result item: %s", error)
                continue

        return results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from Bandcamp."""
        if not self._client.identity:  # library requires identity
            return

        try:
            async with self.throttler.acquire():  # AsyncGenerator method cannot be decorated
                collection = await self._client.get_collection_items(CollectionType.COLLECTION)
            band_ids = set()
            for item in collection.items:
                if item.item_type == "band":
                    band_ids.add(item.item_id)
                elif item.item_type == "album":
                    band_ids.add(item.band_id)

            for band_id in band_ids:
                yield await self.get_artist(band_id)
                await asyncio.sleep(0)  # Yield control to avoid blocking

        except BandcampMustBeLoggedInError as error:
            self.logger.error("Error getting Bandcamp library artists: Wrong identity token.")
            raise LoginFailed("Wrong Bandcamp identity token.") from error
        except BandcampNotFoundError as error:
            raise MediaNotFoundError("Bandcamp library artists returned no results") from error
        except BandcampRateLimitError as error:
            raise ResourceTemporarilyUnavailable(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error
        except BandcampAPIError as error:
            raise MediaNotFoundError("Failed to get library artists") from error

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from Bandcamp."""
        if not self._client.identity:  # library requires identity
            return

        try:
            async with self.throttler.acquire():  # AsyncGenerator method cannot be decorated
                api_collection = await self._client.get_collection_items(CollectionType.COLLECTION)
            for item in api_collection.items:
                if item.item_type == "album":
                    yield await self.get_album(f"{item.band_id}-{item.item_id}")
                    await asyncio.sleep(0)  # Yield control to avoid blocking
        except BandcampMustBeLoggedInError as error:
            self.logger.error("Error getting Bandcamp library albums: Wrong identity token.")
            raise LoginFailed("Wrong Bandcamp identity token.") from error
        except BandcampNotFoundError as error:
            raise MediaNotFoundError("Bandcamp library albums returned no results") from error
        except BandcampRateLimitError as error:
            raise ResourceTemporarilyUnavailable(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error
        except BandcampAPIError as error:
            raise MediaNotFoundError("Failed to get library albums") from error

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Bandcamp."""
        if not self._client.identity:  # library requires identity
            return

        async for album in self.get_library_albums():
            tracks = await self.get_album_tracks(album.item_id)
            for track in tracks:
                yield track
                await asyncio.sleep(0)  # Yield control to avoid blocking

    @use_cache(CACHE)
    @throttle_with_retries
    async def get_artist(self, prov_artist_id: str | int) -> Artist:
        """Get full artist details by id."""
        try:
            api_artist = await self._client.get_artist(prov_artist_id)
            return self._converters.artist_from_api(api_artist)
        except BandcampNotFoundError as error:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found on Bandcamp") from error
        except BandcampRateLimitError as error:
            raise ResourceTemporarilyUnavailable(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error
        except BandcampAPIError as error:
            raise MediaNotFoundError(f"Failed to get artist {prov_artist_id}") from error

    @use_cache(CACHE)
    @throttle_with_retries
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        artist_id, album_id, _ = split_id(prov_album_id)
        try:
            api_album = await self._client.get_album(artist_id, album_id)
            return self._converters.album_from_api(api_album)
        except BandcampNotFoundError as error:
            raise MediaNotFoundError(f"Album {prov_album_id} not found on Bandcamp") from error
        except BandcampRateLimitError as error:
            raise ResourceTemporarilyUnavailable(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error
        except BandcampAPIError as error:
            raise MediaNotFoundError(f"Failed to get album {prov_album_id}") from error

    @throttle_with_retries
    async def _fetch_api_track(self, item_id: str) -> tuple[BCTrack, BCAlbum | None]:
        """Fetch a raw API track and its parent album by compound item ID.

        Uses get_album when album_id is present (most tracks), falling back
        to get_track for standalone tracks (album_id=0).

        :param item_id: Compound track ID in the form artist_id-album_id-track_id.
        """
        artist_id, album_id, track_id = split_id(item_id)
        if not track_id:
            album_id, track_id = 0, album_id

        try:
            if album_id:
                api_album = await self._client.get_album(artist_id, album_id)
                api_track = next((t for t in api_album.tracks if t.id == track_id), None)
                if not api_track:
                    raise MediaNotFoundError(f"Track {item_id} not found in album on Bandcamp")
                return api_track, api_album
            return await self._client.get_track(artist_id, track_id), None
        except BandcampMustBeLoggedInError as error:
            raise LoginFailed("Bandcamp login is invalid or expired.") from error
        except BandcampNotFoundError as error:
            raise MediaNotFoundError(f"Track {item_id} not found on Bandcamp") from error
        except BandcampRateLimitError as error:
            raise ResourceTemporarilyUnavailable(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error
        except BandcampAPIError as error:
            raise MediaNotFoundError(f"Failed to get track {item_id}") from error

    @use_cache(CACHE)
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        api_track, api_album = await self._fetch_api_track(prov_track_id)
        if api_album:
            return self._converters.track_from_api(
                track=api_track,
                album_id=api_album.id,
                album_name=api_album.title,
                album_image_url=api_album.art_url,
            )
        return self._converters.track_from_api(
            track=api_track,
            album_id=api_track.album.id if api_track.album else None,
            album_name=api_track.album.title if api_track.album else "",
            album_image_url=api_track.album.art_url if api_track.album else "",
        )

    @use_cache(CACHE)
    @throttle_with_retries
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all tracks in an album."""
        artist_id, album_id, _ = split_id(prov_album_id)
        try:
            api_album = await self._client.get_album(artist_id, album_id)
            if api_album.tracks:
                return [
                    self._converters.track_from_api(
                        track=track,
                        album_id=album_id,
                        album_name=api_album.title,
                        album_image_url=api_album.art_url,
                    )
                    for track in api_album.tracks
                    if track.streaming_url  # Only include tracks with streaming URLs
                ]

            return []

        except BandcampNotFoundError as error:
            raise MediaNotFoundError(
                f"Album tracks for {prov_album_id} not found on Bandcamp"
            ) from error
        except BandcampRateLimitError as error:
            raise ResourceTemporarilyUnavailable(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error
        except BandcampAPIError as error:
            raise MediaNotFoundError(f"Failed to get albums tracks for {prov_album_id}") from error

    @use_cache(CACHE)
    @throttle_with_retries
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get albums by an artist."""
        albums = []
        try:
            api_discography = await self._client.get_artist_discography(prov_artist_id)
            for item in api_discography:
                if item.get("item_type") == "album" and item.get("item_id"):
                    album = None

                    with suppress(MediaNotFoundError):
                        album = await self.get_album(f"{item['band_id']}-{item['item_id']}")

                    with suppress(MediaNotFoundError):
                        album = album or await self.get_album(f"{prov_artist_id}-{item['item_id']}")

                    if album:
                        albums.append(album)

        except BandcampNotFoundError as error:
            raise MediaNotFoundError(
                f"Artist {prov_artist_id} albums not found on Bandcamp"
            ) from error
        except BandcampRateLimitError as error:
            raise ResourceTemporarilyUnavailable(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error
        except BandcampAPIError as error:
            raise MediaNotFoundError(f"Failed to get albums for artist {prov_artist_id}") from error

        return albums

    @use_cache(CACHE)
    @throttle_with_retries
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top tracks of an artist."""
        tracks: list[Track] = []
        # get_artist_albums and get_album_tracks already handle exceptions and rate limiting
        albums = await self.get_artist_albums(prov_artist_id)
        albums.sort(key=lambda album: (album.year is None, album.year or 0), reverse=True)
        for album in albums:
            tracks.extend(await self.get_album_tracks(album.item_id))
            if len(tracks) >= self.top_tracks_limit:
                break

        return tracks[: self.top_tracks_limit]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track.

        Fetches fresh from the Bandcamp API since streaming URLs may expire.
        """
        api_track, _ = await self._fetch_api_track(item_id)

        streaming_url, bitrate, content_type = self._converters.streaming_url_from_api(
            api_track.streaming_url or {}
        )
        if not streaming_url:
            raise MediaNotFoundError(f"No streaming URL found for track {item_id}")

        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=content_type,
                bit_rate=bitrate,
            ),
            stream_type=StreamType.HTTP,
            media_type=media_type,
            path=streaming_url,
            can_seek=True,
            allow_seek=True,
        )
