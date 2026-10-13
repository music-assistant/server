"""Nugs.net musicprovider support for MusicAssistant."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator
from contextlib import suppress
from datetime import UTC, datetime
from time import time
from typing import TYPE_CHECKING, Any

from aiohttp import ClientConnectionError, ClientPayloadError, ClientTimeout, ContentTypeError
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    AudioError,
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    Playlist,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import (
    CONF_ENTRY_UNOFFICIAL_PROVIDER,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)
from music_assistant.helpers.util import infer_album_type, parse_title_and_version
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from aiohttp import ClientResponse
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.RECOMMENDATIONS,
}

CONF_QUALITY = "quality"
QUALITY_LOSSY = "lossy"
QUALITY_LOSSLESS = "lossless"
# platformID values nugs.net's own clients send to subPlayer.aspx for each quality
PLATFORM_IDS = {QUALITY_LOSSY: -1, QUALITY_LOSSLESS: 2}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return NugsProvider(mass, manifest, config, SUPPORTED_FEATURES)


class NugsProvider(MusicProvider):
    """Provider implementation for Nugs.net."""

    _auth_token: str | None = None
    _token_expiry: float = 0
    _plan_quality_logged: bool = False
    throttler = ThrottlerManager(rate_limit=5, period=1)
    # the legacy search endpoint queues requests rather than returning 429s and slows
    # to timeouts above roughly one search per second, so it has its own tighter gate
    search_throttler = ThrottlerManager(rate_limit=1, period=1)

    @property
    def supported_media_types(self) -> set[MediaType]:
        """Return the media types this provider can serve."""
        return super().supported_media_types | {MediaType.TRACK}

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            CONF_ENTRY_UNOFFICIAL_PROVIDER,
            ConfigEntry(
                key=CONF_QUALITY,
                type=ConfigEntryType.STRING,
                default_value=QUALITY_LOSSLESS,
                options=[
                    ConfigValueOption(QUALITY_LOSSLESS),
                    ConfigValueOption(QUALITY_LOSSY),
                ],
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # a reload may follow a switch to another account, which the cached details cannot tell
        for cached in (self._get_subscription_info, self._get_user_id):
            await self.mass.cache.delete(cached.__name__, provider=self.instance_id)
        await self.login()

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """
        Search the Nugs catalog for artists, releases and recorded performances.

        :param search_query: Search query.
        :param media_types: Media types to include.
        :param limit: Maximum number of results per media type.
        """
        query = search_query.strip()
        if (
            not query
            or limit <= 0
            or not {MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK}.intersection(media_types)
        ):
            return SearchResults()
        if not {MediaType.ARTIST, MediaType.ALBUM}.intersection(media_types):
            return SearchResults(tracks=await self._search_recordings(query, limit))
        if MediaType.TRACK not in media_types:
            artists, albums = await self._search_catalog(query, media_types, limit)
            return SearchResults(artists=artists, albums=albums)
        (artists, albums), tracks = await asyncio.gather(
            self._search_catalog(query, media_types, limit),
            self._search_recordings(query, limit),
        )
        return SearchResults(artists=artists, albums=albums, tracks=tracks)

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve library artists from nugs.net."""
        artist_data = await self._get_all_items("stash", "artists/favorite/")
        for item in artist_data:
            if item and item["id"]:
                yield self._parse_artist(item)

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve library albums from the provider."""
        album_data = await self._get_all_items("stash", "releases/favorite")
        for item in album_data:
            if item and item["id"]:
                yield self._parse_album(item)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve playlists from the provider."""
        playlist_data = await self._get_all_items("stash", "playlists/")
        for item in playlist_data:
            if item and item["id"]:
                yield self._parse_playlist(item)

    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details by id."""
        endpoint = f"/releases/recent?limit=1&artistIds={prov_artist_id}"
        artist_response = await self._get_data("catalog", endpoint)
        artist_data = artist_response["items"][0]["artist"]
        return self._parse_artist(artist_data)

    @use_cache(3600 * 24 * 14, allow_expired_cache=True)  # Cache for 14 days
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        params = {
            "artistIds": prov_artist_id,
            "contentType": "any",
        }
        return [
            self._parse_album(item)
            for item in await self._get_all_items("catalog", "releases/recent", **params)
            if (item and item["id"])
        ]

    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    async def get_album(self, prov_album_id: str) -> Album:
        """Get album details by id."""
        endpoint = f"shows/{prov_album_id}"
        response = await self._get_data("catalog", endpoint)
        return self._parse_album(response["Response"])

    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        endpoint = f"playlists/{prov_playlist_id}"
        response = await self._get_data("stash", endpoint)
        return self._parse_playlist(response["items"])

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        cache_key = f"nugs_track_{prov_track_id}"
        cached: Track | None = await self.mass.cache.get(
            cache_key, provider=self.instance_id, base_class=Track
        )
        if cached:
            return cached
        search_hit: Track | None = await self.mass.cache.get(
            f"nugs_search_track_{prov_track_id}", provider=self.instance_id, base_class=Track
        )
        if search_hit and search_hit.album:
            for track in await self.get_album_tracks(search_hit.album.item_id):
                if track.item_id == prov_track_id:
                    await self._cache_tracks([track])
                    return track
        raise MediaNotFoundError(f"Track {prov_track_id} not found")

    @use_cache(3600 * 24 * 14, allow_expired_cache=True)  # Cache for 14 days
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all album tracks for given album id."""
        endpoint = f"shows/{prov_album_id}"
        response = await self._get_data("catalog", endpoint)
        album_data = response["Response"]
        artist = await self.get_artist(album_data["artistID"])
        album = self._get_item_mapping(
            MediaType.ALBUM, str(album_data["containerID"]), album_data["containerInfo"]
        )
        image = f"https://api.livedownloads.com{album_data['img']['url']}"
        tracks = [
            self._parse_track(item, artist=artist, album=album, image_url=image)
            for item in album_data["tracks"]
            if item["trackID"]
        ]
        await self._cache_tracks(tracks)
        return tracks

    @use_cache(3600, allow_expired_cache=True)  # Cache for 1 hour
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        if page > 0:
            # paging not yet supported
            return []
        endpoint = f"/playlists/{prov_playlist_id}/playlist-tracks/all"
        nugs_result = await self._get_data("stash", endpoint)
        for index, item in enumerate(nugs_result["items"], 1):
            track = self._parse_track(item)
            track.position = index
            result.append(track)
        await self._cache_tracks(result)
        return result

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get this provider's available recommendation rows, without items."""
        return [
            RecommendationFolder(
                name="Most Popular",
                translation_key="nugs_popular_shows",
                item_id="nugs_popular_shows",
                provider=self.instance_id,
            ),
            RecommendationFolder(
                name="Recommended Shows",
                translation_key="nugs_recommended_shows",
                item_id="nugs_recommended_shows",
                provider=self.instance_id,
            ),
            RecommendationFolder(
                name="Recent Shows",
                translation_key="nugs_recent_shows",
                item_id="nugs_recent_shows",
                provider=self.instance_id,
            ),
        ]

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        # caching lives on the folder-level helper: a RecommendationFolder is reconstructed
        # from cached json via base_class, unlike this method's parameterized union type
        folder = await self._get_recommendation_folder(item_id)
        if folder is None:
            return UniqueList()
        return folder.items

    async def _cache_tracks(self, tracks: list[Track], *, search_results: bool = False) -> None:
        """Cache tracks or partial search results for later lookup by get_track."""
        prefix = "nugs_search_track" if search_results else "nugs_track"
        for track in tracks:
            await self.mass.cache.set(
                f"{prefix}_{track.item_id}",
                track.to_dict(),
                expiration=3600 * 24 if search_results else 3600 * 24 * 14,
                provider=self.instance_id,
                persistent=not search_results,
            )

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        stream_url = await self._get_stream_url(item_id)
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=ContentType.UNKNOWN,
            ),
            stream_type=StreamType.HTTP,
            path=stream_url,
        )

    def _parse_artist(self, artist_obj: dict[str, Any]) -> Artist:
        """Parse nugs artist object to generic layout."""
        artist_id = artist_obj.get("artistID") or artist_obj.get("id")
        artist_name = artist_obj.get("artistName") or artist_obj.get("name")
        artist = Artist(
            item_id=str(artist_id),
            provider=self.instance_id,
            name=str(artist_name),
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"https://catalog.nugs.net/api/v1/artists?ids={artist_id}",
                )
            },
        )
        if artist_obj.get("avatarImage"):
            artist.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=artist_obj["avatarImage"]["url"],
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        return artist

    def _parse_album(self, album_obj: dict[str, Any]) -> Album:
        """Parse nugs release/show/album object to generic album layout."""
        item_id = album_obj.get("releaseId") or album_obj.get("id") or album_obj.get("containerID")
        title = album_obj.get("title") or album_obj.get("containerInfo")
        name, version = parse_title_and_version(str(title))
        album = Album(
            item_id=str(item_id),
            provider=self.instance_id,
            name=name,
            version=version,
            provider_mappings={
                ProviderMapping(
                    item_id=str(item_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

        artist_obj = album_obj.get("artist", False) or {
            "id": album_obj["artistID"],
            "name": album_obj["artistName"],
        }
        if artist_obj.get("name") and artist_obj.get("id"):
            album.artists.append(
                self._get_item_mapping(MediaType.ARTIST, str(artist_obj["id"]), artist_obj["name"])
            )

        path: str | None = None
        if album_obj.get("image"):
            path = album_obj["image"]["url"]
        if album_obj.get("img"):
            path = f"https://api.livedownloads.com{album_obj['img']['url']}"
        if path:
            album.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=path,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        year = album_obj.get("performanceDateYear")
        if not year:
            # studio albums carry no performance date; releases spell their date
            # albumReleaseDate (2000-01-01T00:00:00), shows/{id} releaseDateFormatted (2000/01/01)
            date = (
                album_obj.get("performanceDate")
                or album_obj.get("albumReleaseDate")
                or album_obj.get("albumreleaseDate")
                or album_obj.get("releaseDateFormatted")
            )
            if date and (year_match := re.match(r"\d{4}", date)):
                year = year_match[0]
        if year:
            album.year = int(year)

        # No album type info in this provider so try and infer it
        album.album_type = infer_album_type(album.name, album.version)

        return album

    def _parse_playlist(self, playlist_obj: dict[str, Any]) -> Playlist:
        """Parse nugs playlist object to generic layout."""
        return Playlist(
            item_id=playlist_obj["id"],
            provider=self.instance_id,
            name=playlist_obj["name"],
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_obj["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(
                images=UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=playlist_obj["imageUrl"],
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                ),
            ),
            is_editable=False,
        )

    def _parse_track(
        self,
        track_obj: dict[str, Any],
        artist: Artist | ItemMapping | None = None,
        album: Album | ItemMapping | None = None,
        image_url: str | None = None,
    ) -> Track:
        """Parse response from inconsistent nugs.net APIs to a Track model object."""
        track_id = (
            track_obj.get("trackId") or track_obj.get("trackID") or track_obj.get("trackLabel")
        )
        track_name = track_obj.get("name") or track_obj.get("songTitle")
        name, version = parse_title_and_version(str(track_name))

        track = Track(
            item_id=str(track_id),
            provider=self.instance_id,
            name=name,
            version=version,
            provider_mappings={
                ProviderMapping(
                    item_id=str(track_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=True,
                )
            },
        )

        if artist:
            track.artists.append(artist)
        if (
            track_obj.get("artist")
            and isinstance(track_obj.get("artist"), dict)
            and track_obj["artist"].get("id")
        ):
            track.artists.append(
                self._get_item_mapping(
                    MediaType.ARTIST, track_obj["artist"]["id"], track_obj["artist"]["name"]
                )
            )
        if not track.artists:
            msg = "Track is missing artists"
            raise InvalidDataError(msg)

        if album:
            track.album = album
        if image_url is None and track_obj.get("image"):
            image_url = track_obj["image"]["url"]
        if image_url:
            track.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        duration = track_obj.get("durationSeconds") or track_obj.get("totalRunningTime")
        if duration:
            track.duration = int(duration)
        return track

    def _get_search_filters(self, query: str, data: dict[str, Any]) -> dict[str, str]:
        """Resolve explicit artist, venue, year and ISO-date queries to release filters."""

        def normalize(value: str) -> str:
            # Core search removes apostrophes before invoking providers.
            return " ".join(
                re.findall(r"\w+", value.casefold().replace("'", "").replace("\u2019", ""))
            )

        filters: dict[str, str] = {}
        remaining = query.casefold()
        if date_match := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", remaining):
            try:
                date = datetime.strptime(date_match[0], "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                return {}
            filters.update(
                years=str(date.year),
                monthDayFrom=date.strftime("%m-%d"),
                monthDayTo=date.strftime("%m-%d"),
            )
            remaining = remaining.replace(date_match[0], " ", 1)
        elif year_match := re.search(r"\b(?:19|20)\d{2}\b", remaining):
            filters["years"] = year_match[0]
            remaining = remaining.replace(year_match[0], " ", 1)
        remaining = normalize(remaining)
        # Match the longest complete artist name before resolving the remaining venue.
        artists = sorted(
            data.get("artists") or [], key=lambda item: len(item.get("name") or ""), reverse=True
        )
        for artist in artists:
            name = normalize(artist.get("name") or "")
            if name and artist.get("id") and f" {name} " in f" {remaining} ":
                filters["artistIds"] = str(artist["id"])
                remaining = f" {remaining} ".replace(f" {name} ", " ", 1).strip()
                break
        if filters:
            remaining = re.sub(r"^(?:at|in)\s+", "", remaining)
        if remaining:
            venue_ids: list[str] = []
            for venue in data.get("venues") or []:
                name = normalize(venue.get("name") or "")
                # A complete artist match constrains abbreviated venue searches.
                # Consume all remaining words; never discard an unknown artist
                # or other query terms just because a venue was recognized.
                if venue.get("id") and (
                    name == remaining
                    or (len(remaining.split()) >= 2 and name.startswith(f"{remaining} "))
                    or ("artistIds" in filters and f" {remaining} " in f" {name} ")
                ):
                    venue_ids.append(str(venue["id"]))
            if not venue_ids:
                return {}
            filters["venueIds"] = ",".join(dict.fromkeys(venue_ids))
        return filters

    async def _search_catalog(
        self, query: str, media_types: list[MediaType], limit: int
    ) -> tuple[list[Artist], list[Album]]:
        """
        Search the authenticated catalog for artists and releases.

        :param query: Search query.
        :param media_types: Media types to include.
        :param limit: Maximum number of results per media type.
        """
        data = await self._get_catalog_search(query)
        filters = self._get_search_filters(query, data)
        artist_filter = filters.get("artistIds")
        artists: list[Artist] = []
        albums: list[Album] = []
        seen: set[str] = set()
        if MediaType.ARTIST in media_types:
            for item in data.get("artists") or []:
                item_id = str(item.get("id") or "")
                if not item_id or not item.get("name") or item_id in seen:
                    continue
                if artist_filter and item_id != artist_filter:
                    continue
                artists.append(self._parse_artist(item))
                seen.add(item_id)
                if len(artists) == limit:
                    break
        if MediaType.ALBUM in media_types:
            if filters:
                albums = [
                    self._parse_album(item)
                    for item in await self._get_search_releases(filters, limit)
                ]
            seen = {album.item_id for album in albums}
            for item in data.get("albums") or []:
                if len(albums) >= limit:
                    break
                item_id = str(item.get("id") or "")
                if not item_id or not item.get("title") or item_id in seen:
                    continue
                # Discovery supplies titles and covers, but no artist or availability.
                # Full details are resolved through get_album when the item is opened.
                albums.append(
                    self._parse_album(
                        {
                            **item,
                            "artistID": None,
                            "artistName": None,
                            "image": {"url": item["coverImage"]}
                            if item.get("coverImage")
                            else None,
                        }
                    )
                )
                seen.add(item_id)
        return artists, albums

    @use_cache(3600)
    async def _get_catalog_search(self, query: str) -> dict[str, Any]:
        """Discover catalog entities using the authenticated Nugs search API."""
        data = await self._get_data("catalog", "search", query=query)
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise InvalidDataError("Invalid Nugs search response")
        return result

    @use_cache(3600)
    async def _get_search_releases(
        self, filters: dict[str, str], limit: int
    ) -> list[dict[str, Any]]:
        """Return available audio releases matching catalog filters."""
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        offset = 0
        # Bound foreground search latency even when pages contain unavailable
        # or duplicate releases. All venue IDs share one intersected request.
        for _ in range(3):
            data = await self._get_data(
                "catalog",
                "releases",
                **filters,
                contentType="audio",
                orderBy="date",
                order="desc",
                limit=min(limit, 100),
                offset=offset,
            )
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("items"), list)
                or not isinstance(data.get("total"), int)
            ):
                raise InvalidDataError("Invalid Nugs releases response")
            page = data["items"]
            if not page:
                break
            for item in page:
                item_id = str(item.get("id") or "")
                if (
                    not item_id
                    or item_id in seen
                    or not item.get("title")
                    or item.get("status") != "available"
                    or item.get("isInSubscriptionCatalog") is False
                ):
                    continue
                items.append(item)
                seen.add(item_id)
                if len(items) == limit:
                    break
            offset += len(page)
            if len(items) >= limit or offset >= data["total"]:
                break
        return items

    async def _search_recordings(
        self, query: str, limit: int, artist_name: str | None = None
    ) -> list[Track]:
        """Find individual song recordings with their album context."""
        data = await self._get_search_data(query)
        tracks: list[Track] = []
        seen: set[str] = set()
        for group in data.get("catalogSearchTypeContainers") or []:
            if group.get("matchType") != 2:
                continue
            for section in group.get("catalogSearchContainers") or []:
                title = section.get("scHeader", "").removeprefix("Song: ")
                for item in section.get("catalogSearchResultItems") or []:
                    track_id = str(item.get("trackID") or "0")
                    artist_id = str(item.get("artistID") or "0")
                    album_id = str(item.get("containerID") or "0")
                    if (
                        item.get("availability") != 1
                        or track_id == "0"
                        or track_id in seen
                        or artist_id == "0"
                        or album_id == "0"
                        or not title
                        or not item.get("artistName")
                        or not item.get("containerName")
                    ):
                        continue
                    if artist_name and not compare_strings(artist_name, item["artistName"]):
                        continue
                    album = self._parse_search_album(item)
                    tracks.append(
                        self._parse_track(
                            {"trackID": track_id, "songTitle": title},
                            artist=self._get_item_mapping(
                                MediaType.ARTIST, artist_id, item["artistName"]
                            ),
                            album=self._get_item_mapping(MediaType.ALBUM, album_id, album.name),
                            image_url=album.image.path if album.image else None,
                        )
                    )
                    seen.add(track_id)
                    if len(tracks) == limit:
                        await self._cache_tracks(tracks, search_results=True)
                        return tracks
        if not tracks and artist_name is None and " - " in query:
            # Core matching supplies Artist - Track. Try the literal query first
            # so song titles containing the separator retain their meaning.
            artist, title = query.split(" - ", 1)
            if artist.strip() and title.strip():
                return await self._search_recordings(title.strip(), limit, artist.strip())
        await self._cache_tracks(tracks, search_results=True)
        return tracks

    def _parse_search_album(self, item: dict[str, Any]) -> Album:
        """Normalize a legacy search result before parsing its album metadata."""
        album_data = {**item, "containerInfo": item["containerName"]}
        date = album_data.pop("performanceDate", None)
        if date:
            with suppress(ValueError):
                album_data["performanceDateYear"] = (
                    datetime.strptime(date, "%m/%d/%Y").replace(tzinfo=UTC).year
                )
        return self._parse_album(album_data)

    @use_cache(3600)
    @throttle_with_retries
    async def _get_search_data(self, query: str) -> dict[str, Any]:
        """Retrieve cached search metadata from the public Nugs catalog."""
        try:
            async with (
                self.search_throttler.acquire(),
                self.mass.http_session.get(
                    "https://streamapi.nugs.net/api.aspx",
                    params={"method": "catalog.search", "searchStr": query},
                    timeout=ClientTimeout(total=30),
                ) as response,
            ):
                self._check_response_status(response)
                # The legacy endpoint returns JSON with a text/html content type.
                data = json_loads(await response.text())
        except (ClientConnectionError, ClientPayloadError, TimeoutError) as err:
            raise ResourceTemporarilyUnavailable("Nugs search connection failed") from err
        except ValueError as err:
            raise InvalidDataError("Invalid Nugs catalog search response") from err
        if not isinstance(data, dict) or data.get("responseAvailabilityCode") != 0:
            raise InvalidDataError("Nugs catalog search failed")
        result = data.get("Response")
        if not isinstance(result, dict):
            raise InvalidDataError("Invalid Nugs catalog search response")
        if result.get("searchError"):
            raise InvalidDataError("Nugs catalog search failed")
        return result

    # both are fixed for the account, so track starts spend no requests on account lookups
    @use_cache(3600 * 4)  # Cache for 4 hours
    async def _get_subscription_info(self) -> dict[str, Any]:
        """Return the subscription details of the nugs.net account."""
        subscription_info: dict[str, Any] = await self._get_data("subscription", "")
        return subscription_info

    @use_cache(3600 * 4)  # Cache for 4 hours
    async def _get_user_id(self) -> str:
        """Return the user id of the nugs.net account."""
        user_info = await self._get_data("user", "")
        return str(user_info["userId"])

    async def _get_stream_url(self, item_id: str) -> str:
        """
        Return the stream url for a track in the configured quality, or lossy when unavailable.

        :param item_id: The nugs.net track id.
        """
        subscription_info = await self._get_subscription_info()
        # trial and promo accounts have no regular plan: their plan sits on the promo object
        plan = subscription_info.get("plan") or (subscription_info.get("promo") or {}).get("plan")
        if not plan:
            msg = "No active nugs.net subscription found for this account"
            raise AudioError(msg)
        dt_start = datetime.strptime(subscription_info["startedAt"], "%m/%d/%Y %H:%M:%S").replace(
            tzinfo=UTC
        )
        dt_end = datetime.strptime(subscription_info["endsAt"], "%m/%d/%Y %H:%M:%S").replace(
            tzinfo=UTC
        )
        params: dict[str, Any] = {
            "app": 1,
            "HLS": 1,
            "orgn": "websdk",
            "method": "subPlayer",
            "trackId": item_id,
            "subCostplanIDAccessList": plan["id"],
            "startDateStamp": int(dt_start.timestamp()),
            "endDateStamp": int(dt_end.timestamp()),
            "nn_userID": await self._get_user_id(),
            "subscriptionID": subscription_info["legacySubscriptionId"],
        }
        quality = self._get_quality(plan)
        if quality != QUALITY_LOSSY:
            if stream_url := await self._request_stream_link(
                {**params, "platformID": PLATFORM_IDS[quality]}
            ):
                return stream_url
            self.logger.debug("No %s stream for track %s, falling back to lossy", quality, item_id)
        if stream_url := await self._request_stream_link(
            {**params, "platformID": PLATFORM_IDS[QUALITY_LOSSY]}
        ):
            return stream_url
        raise MediaNotFoundError(f"No stream found for song {item_id}.")

    def _get_quality(self, plan: dict[str, Any]) -> str:
        """
        Return the configured stream quality, capped at what the subscription plan includes.

        :param plan: The plan object of the user's nugs.net subscription.
        """
        quality = str(self.config.get_value(CONF_QUALITY))
        if quality != QUALITY_LOSSY and not plan.get("isHighQuality"):
            if not self._plan_quality_logged:
                self.logger.info(
                    "Your nugs.net plan does not include %s streaming, using lossy instead",
                    quality,
                )
                self._plan_quality_logged = True
            return QUALITY_LOSSY
        return quality

    async def _request_stream_link(self, params: dict[str, Any]) -> str | None:
        """
        Request a stream link from the nugs.net stream API, returning None when there is none.

        :param params: The query parameters for the subPlayer request.
        """
        url = "https://streamapi.nugs.net/bigriver/subplayer.aspx"
        timeout = ClientTimeout(total=120)
        async with (
            self.mass.http_session.get(url, params=params, ssl=True, timeout=timeout) as response,
        ):
            response.raise_for_status()
            stream = json_loads(await response.text())
        return stream.get("streamLink") or None

    def _get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.instance_id,
            name=name,
        )

    async def login(self) -> Any:
        """Login to nugs.net and return the token."""
        if self._auth_token and (self._token_expiry > time()):
            return self._auth_token
        if not self.get_setup_value(CONF_USERNAME) or not self.get_setup_value(CONF_PASSWORD):
            msg = "Invalid login credentials"
            raise LoginFailed(msg)
        login_data = {
            "username": self.get_setup_value(CONF_USERNAME),
            "password": self.get_setup_value(CONF_PASSWORD),
            "scope": "offline_access nugsnet:api nugsnet:legacyapi openid profile email",
            "grant_type": "password",
            "client_id": "Eg7HuH873H65r5rt325UytR5429",
        }
        token = None
        url = "https://id.nugs.net/connect/token"
        timeout = ClientTimeout(total=120)
        async with (
            self.mass.http_session.post(
                url, data=login_data, ssl=True, timeout=timeout
            ) as response,
        ):
            # Handle errors
            if response.status == 401:
                raise LoginFailed("Invalid Nugs.net username or password")
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            response.raise_for_status()
            token = await response.json()
            self._auth_token = token["access_token"]
            self._token_expiry = time() + token["expires_in"]
        return token["access_token"]

    @throttle_with_retries
    async def _get_data(self, /, nugs_api: str, endpoint: str, **kwargs: Any) -> Any:
        """Return the requested data from one of various nugs.net API."""
        headers = {}
        url: str | None = None
        timeout = ClientTimeout(total=120)
        tokeninfo = kwargs.pop("tokeninfo", None)
        if tokeninfo is None:
            tokeninfo = await self.login()
        headers = {"Authorization": f"Bearer {tokeninfo}"}
        if nugs_api == "catalog":
            url = f"https://catalog.nugs.net/api/v1/{endpoint}"
        if nugs_api == "stash":
            url = f"https://stash.nugs.net/api/v1/me/{endpoint}"
        if nugs_api == "subscription":
            url = "https://subscriptions.nugs.net/api/v1/me/subscriptions"
        if nugs_api == "user":
            url = "https://stash.nugs.net/api/v1/stash"
        if not url:
            raise MediaNotFoundError(f"{nugs_api} not found")
        try:
            async with self.mass.http_session.get(
                url, headers=headers, params=kwargs, ssl=True, timeout=timeout
            ) as response:
                self._check_response_status(response)
                return await response.json()
        except LoginFailed:
            self._auth_token = None
            raise
        except (ClientConnectionError, ClientPayloadError, TimeoutError) as err:
            raise ResourceTemporarilyUnavailable("Nugs API connection failed") from err
        except (ContentTypeError, ValueError) as err:
            raise InvalidDataError("Invalid Nugs API response") from err

    @staticmethod
    def _check_response_status(response: ClientResponse) -> None:
        """Translate Nugs HTTP errors into provider errors."""
        if response.status == 429:
            raise RateLimited(
                "Nugs API rate limited",
                backoff_time=parse_retry_after(response.headers.get("Retry-After")),
            )
        if response.status in (500, 502, 503, 504):
            raise ResourceTemporarilyUnavailable("Nugs API temporarily unavailable")
        if response.status == 401:
            raise LoginFailed("Nugs authentication expired or was rejected")
        if response.status == 404:
            raise MediaNotFoundError("Nugs item not found")
        if response.status >= 400:
            raise InvalidDataError(f"Nugs API request failed with HTTP {response.status}")

    async def _get_all_items(
        self, nugs_api: str, endpoint: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        limit = 100
        offset = 0
        total = 0
        all_items = []
        while True:
            kwargs["limit"] = limit
            kwargs["offset"] = offset
            result = await self._get_data(nugs_api, endpoint, **kwargs)
            total = result["total"]
            all_items += result["items"]
            if total <= offset + limit:
                break
            offset += limit
        return all_items

    @use_cache(3600 * 4, base_class=RecommendationFolder)  # Cache for 4 hours
    async def _get_recommendation_folder(self, item_id: str) -> RecommendationFolder | None:
        """
        Fetch a single recommendation row, including its items.

        :param item_id: The item_id of the row (unknown ids yield None).
        """
        folder = next(
            (row for row in await self.get_recommendations() if row.item_id == item_id), None
        )
        if folder is None:
            return None
        albums: list[Album]
        if item_id == "nugs_popular_shows":
            popular_data = await self._get_data("catalog", "releases/popular", limit=20)
            albums = []
            for item in popular_data["items"]:
                response = await self._get_data("catalog", f"shows/{item['id']}")
                albums.append(self._parse_album(response["Response"]))
        elif item_id == "nugs_recommended_shows":
            recommended_data = await self._get_data("catalog", "me/releases/recommendations")
            albums = [self._parse_album(item) for item in recommended_data["items"]]
        else:  # nugs_recent_shows
            recent_data = await self._get_data("catalog", "releases/recent", limit=50)
            albums = [self._parse_album(item) for item in recent_data["items"]]
        folder.items = UniqueList(albums)
        return folder
