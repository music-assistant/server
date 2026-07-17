"""Bandcamp music provider support for MusicAssistant."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, cast

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
from bandcamp_async_api.models import (
    BCAlbum,
    BCTrack,
    CollectionItem,
    CollectionSummary,
    CollectionType,
    FanItem,
    FeedResponse,
    FollowingItem,
)
from mashumaro.exceptions import UnserializableDataError
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import (
    ConfigEntryType,
    ImageType,
    MediaType,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    RateLimited,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    BROWSE_FANS,
    BROWSE_FEED,
    BROWSE_FOLLOWERS,
    BROWSE_FOLLOWING,
    BROWSE_WISHLIST,
    CACHE_EMPTY_RESULTS,
    CACHE_METADATA,
    CACHE_USER_LISTS,
    CONF_IDENTITY,
    CONF_TOP_TRACKS_LIMIT,
    DEFAULT_TOP_TRACKS_LIMIT,
    PERSON_SUB_FOLDERS,
    PERSON_SUB_ROUTES,
    SUPPORTED_FEATURES,
)
from .converters import BandcampConverters

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderManifest


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
        CONF_ENTRY_UNOFFICIAL_PROVIDER,
        ConfigEntry(
            key=CONF_IDENTITY,
            type=ConfigEntryType.SECURE_STRING,
            required=False,
            value=values.get(CONF_IDENTITY) if values else None,
        ),
        ConfigEntry(
            key=CONF_TOP_TRACKS_LIMIT,
            type=ConfigEntryType.INTEGER,
            required=False,
            value=values.get(CONF_TOP_TRACKS_LIMIT) if values else DEFAULT_TOP_TRACKS_LIMIT,
            default_value=DEFAULT_TOP_TRACKS_LIMIT,
            advanced=True,
        ),
    )


def split_id(id_: str) -> tuple[int, int, int]:
    """
    Return (artist_id, album_id, track_id). Missing parts are returned as 0.

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
    _slug_to_fan_id: dict[str, int]  # unbounded; eviction would break back-navigation
    throttler: ThrottlerManager = ThrottlerManager(
        rate_limit=50,  # requests per period seconds
        period=10,
        initial_backoff=3,  # Bandcamp responds with Retry-After 3
        retry_attempts=5,
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
        self._slug_to_fan_id = {}

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
            raise RateLimited(
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

    @throttle_with_retries
    async def _fetch_collection_page(
        self,
        collection_type: CollectionType,
        older_than_token: str | None,
        fan_id: int | None,
    ) -> CollectionSummary:
        """
        Fetch a single page of collection items with throttling and retry.

        :param collection_type: The type of collection to fetch.
        :param older_than_token: Pagination cursor from the previous page.
        :param fan_id: Fan ID to query. None = authenticated user.
        """
        try:
            return await self._client.get_collection_items(
                collection_type,
                older_than_token=older_than_token,
                fan_id=fan_id,
            )
        except BandcampRateLimitError as error:
            raise RateLimited(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error

    async def _get_all_collection_items(
        self,
        collection_type: CollectionType,
        fan_id: int | None = None,
    ) -> list[CollectionItem | FollowingItem | FanItem]:
        """
        Fetch all pages of a collection endpoint.

        :param collection_type: The type of collection to fetch.
        :param fan_id: Fan ID to query. None = authenticated user.
        """
        all_items: list[CollectionItem | FollowingItem | FanItem] = []
        older_than_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            page = await self._fetch_collection_page(collection_type, older_than_token, fan_id)
            all_items.extend(page.items)
            self.logger.debug(
                "Fetched %d items for %s (has_more=%s, last_token=%s, total=%d)",
                len(page.items),
                collection_type.value,
                page.has_more,
                page.last_token,
                len(all_items),
            )
            if not page.has_more or not page.last_token:
                break
            if page.last_token in seen_tokens:
                self.logger.warning(
                    "Pagination loop detected for %s: token %s already seen, stopping",
                    collection_type.value,
                    page.last_token,
                )
                break
            seen_tokens.add(page.last_token)
            older_than_token = page.last_token
        return all_items

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve library artists from Bandcamp."""
        if not self._client.identity:  # library requires identity
            return

        try:
            items = await self._get_all_collection_items(CollectionType.COLLECTION)
            band_ids = set()
            for item in items:
                if item.item_type == "band":
                    band_ids.add(item.item_id)
                elif item.item_type == "album":
                    band_ids.add(item.band_id)

            for band_id in band_ids:
                yield await self.get_artist(str(band_id))
                await asyncio.sleep(0)  # Yield control to avoid blocking

        except BandcampMustBeLoggedInError as error:
            self.logger.error("Error getting Bandcamp library artists: Wrong identity token.")
            raise LoginFailed("Wrong Bandcamp identity token.") from error
        except BandcampNotFoundError as error:
            raise MediaNotFoundError("Bandcamp library artists returned no results") from error
        except BandcampRateLimitError as error:
            raise RateLimited(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error
        except BandcampAPIError as error:
            raise MediaNotFoundError("Failed to get library artists") from error

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve library albums from Bandcamp."""
        if not self._client.identity:  # library requires identity
            return

        try:
            items = await self._get_all_collection_items(CollectionType.COLLECTION)
            for item in items:
                if item.item_type == "album":
                    yield await self.get_album(f"{item.band_id}-{item.item_id}")
                    await asyncio.sleep(0)  # Yield control to avoid blocking
        except BandcampMustBeLoggedInError as error:
            self.logger.error("Error getting Bandcamp library albums: Wrong identity token.")
            raise LoginFailed("Wrong Bandcamp identity token.") from error
        except BandcampNotFoundError as error:
            raise MediaNotFoundError("Bandcamp library albums returned no results") from error
        except BandcampRateLimitError as error:
            raise RateLimited(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error
        except BandcampAPIError as error:
            raise MediaNotFoundError("Failed to get library albums") from error

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve library tracks from Bandcamp."""
        if not self._client.identity:  # library requires identity
            return

        async for album in self.get_library_albums():
            tracks = await self.get_album_tracks(album.item_id)
            for track in tracks:
                yield track
                await asyncio.sleep(0)  # Yield control to avoid blocking

    @use_cache(CACHE_METADATA)
    @throttle_with_retries
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        try:
            api_artist = await self._client.get_artist(prov_artist_id)
            return self._converters.artist_from_api(api_artist)
        except BandcampNotFoundError as error:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found on Bandcamp") from error
        except BandcampRateLimitError as error:
            raise RateLimited(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error
        except BandcampAPIError as error:
            raise MediaNotFoundError(f"Failed to get artist {prov_artist_id}") from error

    @use_cache(CACHE_METADATA)
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
            raise RateLimited(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error
        except BandcampAPIError as error:
            raise MediaNotFoundError(f"Failed to get album {prov_album_id}") from error

    @throttle_with_retries
    async def _fetch_api_track(self, item_id: str) -> tuple[BCTrack, BCAlbum | None]:
        """
        Fetch a raw API track and its parent album by compound item ID.

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
            raise RateLimited(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error
        except BandcampAPIError as error:
            raise MediaNotFoundError(f"Failed to get track {item_id}") from error

    @use_cache(CACHE_METADATA)
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

    @use_cache(CACHE_METADATA)
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
            raise RateLimited(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error
        except BandcampAPIError as error:
            raise MediaNotFoundError(f"Failed to get albums tracks for {prov_album_id}") from error

    @use_cache(CACHE_METADATA)
    @throttle_with_retries
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get albums by an artist."""
        try:
            api_discography = await self._client.get_artist_discography(prov_artist_id)
            return [
                self._converters.album_from_discography_item(item)
                for item in api_discography
                if item.get("item_type") == "album" and item.get("item_id")
            ]
        except BandcampNotFoundError as error:
            raise MediaNotFoundError(
                f"Artist {prov_artist_id} albums not found on Bandcamp"
            ) from error
        except BandcampRateLimitError as error:
            raise RateLimited(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error
        except BandcampAPIError as error:
            raise MediaNotFoundError(f"Failed to get albums for artist {prov_artist_id}") from error

    @use_cache(CACHE_METADATA)
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

    async def recommendations(self, wanted: set[str] | None = None) -> list[RecommendationFolder]:
        """
        Surface Bandcamp's personalised feed and wishlist as recommendations.

        :param wanted: optional set of row item_ids to build; when None, all rows are built.
        """
        if not self._client.identity:
            return []

        def want(item_id: str) -> bool:
            return wanted is None or item_id in wanted

        folders: list[RecommendationFolder] = []
        if want("feed") and (feed_tracks := await self._get_feed_tracks()):
            folders.append(
                RecommendationFolder(
                    item_id="feed",
                    provider=self.instance_id,
                    name="Bandcamp Feed",
                    translation_key="feed",
                    icon="mdi-rss",
                    is_playable=True,
                    items=UniqueList(feed_tracks),
                )
            )
        if want("wishlist") and (
            wishlist := await self._browse_person_content(None, CollectionType.WISHLIST)
        ):
            folders.append(
                RecommendationFolder(
                    item_id="wishlist",
                    provider=self.instance_id,
                    name="Wishlist",
                    translation_key="wishlist",
                    icon="mdi-heart",
                    is_playable=True,
                    items=UniqueList(wishlist),
                )
            )
        return folders

    @throttle_with_retries
    async def _fetch_feed(self) -> FeedResponse:
        """Fetch the authenticated user's feed with throttling and retry."""
        try:
            return await self._client.get_feed()
        except BandcampRateLimitError as error:
            raise RateLimited(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error

    async def _get_feed_tracks(self) -> list[Track]:
        """Fetch and convert the streamable tracks from the user's feed."""
        cache_key = "_feed_tracks"
        cached = await self.mass.cache.get(cache_key, provider=self.instance_id, base_class=Track)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        tracks: list[Track] = []
        async with self._map_api_errors("Failed to get Bandcamp feed"):
            feed = await self._fetch_feed()
            tracks = [
                self._converters.track_from_feed(track)
                for track in feed.track_list
                if track.streaming_url
            ]
        await self.mass.cache.set(
            cache_key,
            [t.to_dict() for t in tracks],
            expiration=CACHE_USER_LISTS if tracks else CACHE_EMPTY_RESULTS,
            provider=self.instance_id,
        )
        return tracks

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse this provider's items.

        :param path: The path to browse, (e.g. provider_id://artists).
        """
        subpath = path.split("://")[1] if "://" in path else ""
        # Filter empty segments from double-slashes or trailing slashes
        path_parts = [p for p in subpath.split("/") if p]
        base = f"{self.instance_id}://"

        # Route fan/follower paths (supports arbitrary nesting depth)
        if path_parts and path_parts[0] in (BROWSE_FANS, BROWSE_FOLLOWERS):
            return await self._browse_person(path_parts, base)

        # The feed/wishlist recommendation folders resolve to their tracks here when played;
        # the folder's explicit path is dropped on deserialization, so play arrives as the
        # bare item_id slug (e.g. ".../feed") rather than ".../recommendations/feed".
        if path_parts == [BROWSE_FEED]:
            return await self._get_feed_tracks()
        if path_parts == [BROWSE_WISHLIST]:
            return await self._browse_person_content(None, CollectionType.WISHLIST)
        if path_parts == [BROWSE_FOLLOWING]:
            return await self._browse_person_following(None)

        # Delegate standard library paths and root listing to the base class
        result = list(await super().browse(path))

        # At root level, append custom browse folders when authenticated.
        # These top-level folders query the authenticated user (person_id=None);
        # person-specific paths (e.g. fans/42/wishlist) work without authentication
        # since the Bandcamp API only requires identity for the "me" shortcut.
        if not path_parts and self._client.identity:
            # Collection is excluded — the user's own collection is the standard library.
            for folder_id, folder_name in (
                (BROWSE_WISHLIST, "Wishlist"),
                (BROWSE_FOLLOWING, "Following"),
                (BROWSE_FANS, "Fans"),
                (BROWSE_FOLLOWERS, "Followers"),
            ):
                result.append(
                    BrowseFolder(
                        item_id=folder_id,
                        provider=self.instance_id,
                        path=base + folder_id,
                        name=folder_name,
                        translation_key=folder_id,
                    )
                )

        return result

    async def _browse_person(
        self,
        path_parts: list[str],
        base: str,
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Route person browse paths: fans/followers and their sub-categories.

        Pattern: (fans|followers)[/{id}[/(collection|wishlist|following|fans|followers)]*]
        """
        # Top-level: authenticated user's fans or followers
        if len(path_parts) == 1:
            collection_type = (
                CollectionType.FOLLOWING_FANS
                if path_parts[0] == BROWSE_FANS
                else CollectionType.FOLLOWERS
            )
            return await self._browse_person_people(collection_type, f"{base}{path_parts[0]}")

        tail = path_parts[-1]

        # Path ends with a person identifier (numeric ID or slug) → show their 5 sub-folders
        person_id = await self._resolve_person_segment(tail)
        if person_id is not None:
            if person_id <= 0:
                raise InvalidDataError(f"Invalid person ID in browse path: {tail}")
            return self._browse_person_root(person_id, f"{base}{'/'.join(path_parts)}")

        # Path ends with a sub-category → person identifier is second-to-last
        if len(path_parts) < 2:
            raise InvalidDataError(f"Invalid browse path: {base}{'/'.join(path_parts)}")
        person_id = await self._resolve_person_segment(path_parts[-2])
        if person_id is None:
            raise InvalidDataError(f"Invalid browse path: {base}{'/'.join(path_parts)}")
        if person_id <= 0:
            raise InvalidDataError(f"Invalid person ID in browse path: {path_parts[-2]}")

        route = PERSON_SUB_ROUTES.get(tail)
        if route is None:
            raise InvalidDataError(f"Unknown browse sub-category: {tail}")

        method_kind, collection_type = route
        if method_kind == "content":
            return await self._browse_person_content(person_id, collection_type)
        if method_kind == "following":
            return await self._browse_person_following(person_id)
        if method_kind != "people":
            raise InvalidDataError(f"Unknown route kind: {method_kind}")
        canon = "/".join(path_parts)
        return await self._browse_person_people(collection_type, f"{base}{canon}", person_id)

    # --- Person browse helpers (fans, followers, and social graph traversal) ---

    async def _resolve_person_segment(self, segment: str) -> int | None:
        """
        Resolve a path segment to a fan_id.

        Checks the slug→fan_id cache first, then tries numeric parse.
        For unknown slugs, rebuilds the cache from fan/follower lists and retries.
        Returns None if the segment is neither a known slug nor a valid int,
        or if it is a known sub-route name (e.g. "collection", "wishlist").
        """
        if segment in self._slug_to_fan_id:
            return self._slug_to_fan_id[segment]
        try:
            return int(segment)
        except ValueError:
            pass
        # Known sub-route names are structural, not user slugs
        if segment in PERSON_SUB_ROUTES:
            return None
        # Slug not in cache and not numeric — rebuild from parent lists and retry
        await self._rebuild_slug_cache()
        return self._slug_to_fan_id.get(segment)

    async def _rebuild_slug_cache(self) -> None:
        """Re-fetch fan/follower lists to rebuild the slug→fan_id map."""
        base = f"{self.instance_id}://"
        for collection_type, folder_id in (
            (CollectionType.FOLLOWING_FANS, BROWSE_FANS),
            (CollectionType.FOLLOWERS, BROWSE_FOLLOWERS),
        ):
            with suppress(Exception):
                await self._browse_person_people(collection_type, f"{base}{folder_id}")

    @staticmethod
    def _fan_slug(person: FanItem) -> str | None:
        """
        Extract the URL slug from a FanItem's url.

        e.g. "https://bandcamp.com/teancom" → "teancom"
        """
        if person.url:
            slug: str = person.url.rstrip("/").rsplit("/", 1)[-1]
            if slug:
                return slug
        return None

    def _people_to_folders(self, items: list[FanItem], base_path: str) -> list[BrowseFolder]:
        """Convert a list of people to BrowseFolder items with thumbnails."""
        folders: list[BrowseFolder] = []
        for person in items:
            slug = self._fan_slug(person)
            if slug:
                self._slug_to_fan_id[slug] = person.fan_id
            path_segment = slug or str(person.fan_id)
            folder = BrowseFolder(
                item_id=f"person_{person.fan_id}",
                provider=self.instance_id,
                path=f"{base_path}/{path_segment}",
                name=person.name or f"User {person.fan_id}",
            )
            if person.image_url:
                folder.image = MediaItemImage(
                    type=ImageType.THUMB,
                    path=person.image_url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            folders.append(folder)
        return folders

    def _browse_person_root(self, person_id: int, base_path: str) -> list[BrowseFolder]:
        """Return the 5 sub-folders for a person's profile."""
        return [
            BrowseFolder(
                item_id=f"person_{person_id}_{sub_id}",
                provider=self.instance_id,
                path=f"{base_path}/{sub_id}",
                name=name,
                translation_key=sub_id,
            )
            for sub_id, name in PERSON_SUB_FOLDERS
        ]

    @asynccontextmanager
    async def _map_api_errors(self, context: str) -> AsyncIterator[None]:
        """Map Bandcamp API exceptions to MusicAssistant exceptions."""
        try:
            yield
        except BandcampMustBeLoggedInError as error:
            raise LoginFailed("Wrong Bandcamp identity token.") from error
        except BandcampRateLimitError as error:
            raise RateLimited(
                "Bandcamp rate limit reached", backoff_time=error.retry_after
            ) from error
        except BandcampAPIError as error:
            raise MediaNotFoundError(context) from error

    @staticmethod
    def _deserialize_content_item(item: dict[str, object]) -> Album | Track:
        """Deserialize a cached content item back to its model type."""
        media_type = item.get("media_type")
        if media_type == MediaType.ALBUM:
            return Album.from_dict(item)
        if media_type == MediaType.TRACK:
            return Track.from_dict(item)
        msg = f"Unexpected media_type in cached content item: {media_type}"
        raise ValueError(msg)

    @throttle_with_retries
    async def _browse_person_content(
        self, person_id: int | None, collection_type: CollectionType
    ) -> list[Album | Track]:
        """
        Fetch a person's collection or wishlist items.

        :param person_id: Person to query. None = authenticated user.
        """
        cache_key = f"_browse_person_content_{person_id}_{collection_type.value}"
        cached = await self.mass.cache.get(cache_key, provider=self.instance_id)
        if cached is not None:
            try:
                return [self._deserialize_content_item(item) for item in cached]
            except LookupError, ValueError, UnserializableDataError, InvalidDataError:
                self.logger.warning("Stale cache for %s, fetching fresh", cache_key)
        results: list[Album | Track] = []
        context = f"Failed to get {collection_type.value} for person {person_id}"
        async with self._map_api_errors(context):
            items = await self._get_all_collection_items(collection_type, fan_id=person_id)
            for item in items:
                with suppress(MediaNotFoundError):
                    if item.item_type == "album":
                        results.append(await self.get_album(f"{item.band_id}-{item.item_id}"))
                    elif item.item_type == "track":
                        results.append(await self.get_track(f"{item.band_id}-0-{item.item_id}"))
        await self.mass.cache.set(
            cache_key,
            [item.to_dict() for item in results],
            expiration=CACHE_USER_LISTS if results else CACHE_EMPTY_RESULTS,
            provider=self.instance_id,
        )
        return results

    @throttle_with_retries
    async def _browse_person_following(self, person_id: int | None) -> list[Artist]:
        """
        Fetch a person's followed artists.

        :param person_id: Person to query. None = authenticated user.
        """
        cache_key = f"_browse_person_following_{person_id}"
        cached = await self.mass.cache.get(cache_key, provider=self.instance_id, base_class=Artist)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        artists: list[Artist] = []
        async with self._map_api_errors(f"Failed to get following for person {person_id}"):
            collection = await self._get_all_collection_items(
                CollectionType.FOLLOWING, fan_id=person_id
            )
            for item in collection:
                try:
                    artists.append(await self.get_artist(item.band_id))
                except MediaNotFoundError:
                    self.logger.warning(
                        "Artist not found for band_id %s (%s)", item.band_id, item.name
                    )
        await self.mass.cache.set(
            cache_key,
            [a.to_dict() for a in artists],
            expiration=CACHE_USER_LISTS if artists else CACHE_EMPTY_RESULTS,
            provider=self.instance_id,
        )
        return artists

    @throttle_with_retries
    async def _browse_person_people(
        self,
        collection_type: CollectionType,
        base_path: str,
        person_id: int | None = None,
    ) -> list[BrowseFolder]:
        """
        Fetch a person's fans or followers as browsable folders.

        :param collection_type: FOLLOWING_FANS or FOLLOWERS.
        :param base_path: Browse path prefix for the resulting folder links.
        :param person_id: Person to query. None = authenticated user.
        """
        # base_path included intentionally: folder links differ per navigation path.
        cache_key = f"_browse_person_people_{person_id}_{collection_type.value}_{base_path}"
        cached = await self.mass.cache.get(
            cache_key, provider=self.instance_id, base_class=BrowseFolder
        )
        if cached is not None:
            for folder in cached:
                segment = folder.path.rstrip("/").rsplit("/", 1)[-1]
                fan_id_str = folder.item_id.removeprefix("person_")
                with suppress(ValueError):
                    self._slug_to_fan_id[segment] = int(fan_id_str)
            return cached  # type: ignore[no-any-return]
        context = f"Failed to get {collection_type.value} for person {person_id}"
        async with self._map_api_errors(context):
            collection = await self._get_all_collection_items(collection_type, fan_id=person_id)
            folders = self._people_to_folders(collection, base_path)
        await self.mass.cache.set(
            cache_key,
            [f.to_dict() for f in folders],
            expiration=CACHE_USER_LISTS if folders else CACHE_EMPTY_RESULTS,
            provider=self.instance_id,
        )
        return folders

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return the content details for the given track.

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
