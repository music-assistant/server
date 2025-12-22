"""Bandcamp music provider support for MusicAssistant."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress

from bandcamp_async_api import (
    BandcampAPIClient,
    BandcampAPIError,
    BandcampNotFoundError,
    SearchResultAlbum,
    SearchResultArtist,
    SearchResultTrack,
)
from bandcamp_async_api.models import CollectionType
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError

# noinspection PyProtectedMember
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    SearchResults,
    Track,
)
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
    # ProviderFeature.BROWSE,  # TODO: Consider
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    # ProviderFeature.RECOMMENDATIONS,  # TODO: Consider
    # ProviderFeature.SIMILAR_TRACKS,  # TODO: Consider
}

CONF_IDENTITY = "identity"
CONF_SEARCH_LIMIT = "search_limit"
CONF_TOP_TRACKS_LIMIT = "top_tracks_limit"
DEFAULT_SEARCH_LIMIT = 10
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
    # noinspection PyTypeChecker
    return (
        ConfigEntry(
            key=CONF_IDENTITY,
            type=ConfigEntryType.SECURE_STRING,
            label="Identity token",
            required=False,
            description="Identity token from Bandcamp cookies for collection access. "
            "See https://bandcamp.com and extract from browser cookies.",
            value=values.get(CONF_IDENTITY) if values else None,
        ),
        ConfigEntry(
            key=CONF_SEARCH_LIMIT,
            type=ConfigEntryType.INTEGER,
            label="Search items limit",
            required=False,
            description="Search items limit for one search.",
            value=values.get(CONF_SEARCH_LIMIT) if values else DEFAULT_SEARCH_LIMIT,
            default_value=DEFAULT_SEARCH_LIMIT,
        ),
        ConfigEntry(
            key=CONF_TOP_TRACKS_LIMIT,
            type=ConfigEntryType.INTEGER,
            label="Artist Top Tracks search limit",
            required=False,
            description="Search limit while getting artist top tracks.",
            value=values.get(CONF_TOP_TRACKS_LIMIT) if values else DEFAULT_TOP_TRACKS_LIMIT,
            default_value=DEFAULT_TOP_TRACKS_LIMIT,
        ),
    )


def split_id(id_: str) -> tuple[int, int | None, int | None]:
    """Return (artist_id, album_id, track_id). Missing parts are returned as 0."""
    parts = id_.split("-")
    part_0 = int(parts[0])
    part_1 = int(parts[1]) if len(parts) > 1 else 0
    part_2 = int(parts[2]) if len(parts) > 2 else 0
    return part_0, part_1, part_2


class BandcampProvider(MusicProvider):
    """Bandcamp provider support."""

    _client: BandcampAPIClient
    _converters: BandcampConverters
    throttler: ThrottlerManager
    search_limit: int
    top_tracks_limit: int

    async def handle_async_init(self) -> None:
        """Handle async init of the Bandcamp provider."""
        identity = self.config.get_value(CONF_IDENTITY)
        self.search_limit = self.config.get_value(CONF_SEARCH_LIMIT, DEFAULT_SEARCH_LIMIT)
        self.top_tracks_limit = self.config.get_value(
            CONF_TOP_TRACKS_LIMIT, DEFAULT_TOP_TRACKS_LIMIT
        )

        # Initialize the new async API client
        self._client = BandcampAPIClient(session=self.mass.http_session, identity_token=identity)

        self.throttler = ThrottlerManager(rate_limit=1, period=2)
        self._converters = BandcampConverters(self.domain, self.instance_id)

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return False

    @use_cache(CACHE)
    @throttle_with_retries
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int | None = None
    ) -> SearchResults:
        """Perform search on music provider."""
        if limit is None:
            limit = self.search_limit

        results = SearchResults()
        if not self._client.identity:
            return results

        if media_types is None:
            return results

        try:
            search_results = await self._client.search(search_query)
        except (BandcampAPIError, BandcampNotFoundError) as e:
            self.logger.warning("Failed to search Bandcamp: %s", e)
            return results
        except Exception as e:
            self.logger.exception("Unexpected error during Bandcamp search: %s", e)
            return results

        for item in search_results[:limit]:
            try:
                if isinstance(item, SearchResultTrack) and MediaType.TRACK in media_types:
                    # noinspection PyUnresolvedReferences
                    results.tracks.append(self._converters.track_from_search(item))
                    # results.tracks.append(
                    #     await self.get_track(f"{item.artist_id}-{item.album_id or 0}-{item.id}")
                    # )
                elif isinstance(item, SearchResultAlbum) and MediaType.ALBUM in media_types:
                    # noinspection PyUnresolvedReferences
                    results.albums.append(self._converters.album_from_search(item))
                    # results.albums.append(await self.get_album(f"{item.artist_id}-{item.id}"))
                elif isinstance(item, SearchResultArtist) and MediaType.ARTIST in media_types:
                    # noinspection PyUnresolvedReferences
                    results.artists.append(self._converters.artist_from_search(item))
                    # results.artists.append(await self.get_artist(item.id))
            except Exception as e:
                self.logger.warning("Failed to convert search result item: %s", e)
                continue

        return results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from Bandcamp."""
        if not self._client.identity:  # library requires identity
            return

        # noinspection PyBroadException
        try:
            collection = await self._client.get_collection_items(CollectionType.COLLECTION)
            band_ids = set()
            for item in collection.items:
                if item.item_type == "band":
                    band_ids.add(item.item_id)
                elif item.item_type == "album":
                    # noinspection PyArgumentList
                    band_ids.add(item.band_id)

            for band_id in band_ids:
                # noinspection PyArgumentList
                yield await self.get_artist(band_id)
                await asyncio.sleep(0)  # Yield control to avoid blocking

        except Exception:
            self.logger.exception("Failed to get library artists")
            return

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from Bandcamp."""
        if not self._client.identity:  # library requires identity
            return

        # noinspection PyBroadException
        try:
            api_collection = await self._client.get_collection_items(CollectionType.COLLECTION)
            for item in api_collection.items:
                if item.item_type == "album":
                    # noinspection PyArgumentList
                    yield await self.get_album(f"{item.band_id}-{item.item_id}")
                    await asyncio.sleep(0)  # Yield control to avoid blocking
        except Exception:
            self.logger.exception("Failed to get library albums")
            return

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Bandcamp."""
        if not self._client.identity:  # library requires identity
            return

        # noinspection PyBroadException
        try:
            # noinspection PyTypeChecker,PyArgumentList
            async for album in self.get_library_albums():
                # noinspection PyArgumentList
                tracks = await self.get_album_tracks(album.item_id)
                for track in tracks:
                    yield track
                    await asyncio.sleep(0)  # Yield control to avoid blocking
        except Exception:
            self.logger.exception("Failed to get library tracks")
            return

    @use_cache(CACHE)
    async def get_artist(self, prov_artist_id: str | int) -> Artist | None:
        """Get full artist details by id."""
        try:
            api_artist = await self._client.get_artist(prov_artist_id)
            return self._converters.artist_from_api(api_artist)
        except Exception as error:
            self.logger.warning("Failed getting artist: %s", error)
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found on Bandcamp") from error

    @use_cache(CACHE)
    async def get_album(self, prov_album_id: str) -> Album | None:
        """Get full album details by id."""
        artist_id, album_id, _ = split_id(prov_album_id)
        try:
            api_album = await self._client.get_album(artist_id, album_id)
            return self._converters.album_from_api(api_album)
        except Exception as error:
            self.logger.warning("Failed getting album: %s", error)
            raise MediaNotFoundError(f"Album {prov_album_id} not found on Bandcamp") from error

    @use_cache(CACHE)
    async def get_track(self, prov_track_id: str) -> Track | None:
        """Get full track details by id."""
        artist_id, album_id, track_id = split_id(prov_track_id)
        if track_id is None:  # artist_id-track_id
            album_id, track_id = None, album_id

        try:
            if all((artist_id, album_id, track_id)):
                api_album = await self._client.get_album(artist_id, album_id)
                api_track = next((_ for _ in api_album.tracks if _.id == track_id), None)
                return self._converters.track_from_api(
                    track=api_track,
                    album_id=api_album.id,
                    album_name=api_album.title,
                    album_image_url=api_album.art_url,
                )
            elif not album_id:
                api_track = await self._client.get_track(artist_id, track_id)
                return self._converters.track_from_api(
                    track=api_track,
                    album_id=api_track.album.id if api_track.album else None,
                    album_name=api_track.album.title if api_track.album else None,
                    album_image_url=api_track.album.art_url if api_track.album else None,
                )
            else:
                raise MediaNotFoundError(f"Track {prov_track_id} not found on Bandcamp")
        except Exception as error:
            self.logger.warning("Failed getting track: %s", error)
            raise MediaNotFoundError(f"Track {prov_track_id} not found on Bandcamp") from error

    @use_cache(CACHE)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all tracks in an album."""
        artist_id, album_id, _ = split_id(prov_album_id)
        # noinspection PyBroadException
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
        except Exception:
            self.logger.exception("Failed to get album tracks")
            return []

    @use_cache(CACHE)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get albums by an artist."""
        albums = []
        # noinspection PyBroadException
        try:
            api_discography = await self._client.get_artist_discography(prov_artist_id)
            for item in api_discography:
                if item.get("item_type") == "album" and item.get("item_id"):
                    album = None

                    with suppress(MediaNotFoundError):
                        # noinspection PyArgumentList
                        album = await self.get_album(f"{item['band_id']}-{item['item_id']}")

                    with suppress(MediaNotFoundError):
                        # noinspection PyArgumentList
                        album = album or await self.get_album(f"{prov_artist_id}-{item['item_id']}")

                    if album:
                        albums.append(album)
        except Exception:
            self.logger.exception("Failed to get artist albums")
        return albums

    @use_cache(CACHE)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top tracks of an artist."""
        tracks = []
        try:
            # noinspection PyArgumentList
            albums = await self.get_artist_albums(prov_artist_id)
            albums.sort(key=lambda _: _.year, reverse=True)
            for album in albums:
                # noinspection PyArgumentList
                tracks.extend(await self.get_album_tracks(album.item_id))
                if len(tracks) >= self.top_tracks_limit:
                    break
        except Exception:
            self.logger.exception("Failed to get artist top tracks")
        return tracks[: self.top_tracks_limit]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track."""
        try:
            # noinspection PyArgumentList
            track_ma = await self.get_track(item_id)  # consider _client
            content_type = ContentType.MP3
            link = next(iter(track_ma.metadata.links))
            if not link:
                raise MediaNotFoundError(
                    f"No streaming URL found for track {item_id}. Please report this"
                )

            streaming_url = link.url
            if not streaming_url:
                raise MediaNotFoundError(
                    f"No streaming URL found for track {item_id}: {streaming_url}"
                )

            return StreamDetails(
                item_id=item_id,
                provider=self.instance_id,
                audio_format=AudioFormat(content_type=content_type),  # , bit_rate=bitrate
                stream_type=StreamType.HTTP,
                media_type=media_type,
                path=streaming_url,
                can_seek=True,
                allow_seek=True,
            )

        except Exception as error:
            self.logger.warning("Failed to get stream details for %s: %s", item_id, error)
            raise MediaNotFoundError(f"Stream details not available for track {item_id}") from error
