"""Live Phish music provider support for Music Assistant."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import secrets
from collections.abc import AsyncGenerator, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from time import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urljoin, urlparse

from aiohttp import (
    ClientConnectionError,
    ClientPayloadError,
    ClientResponseError,
    ClientTimeout,
    ContentTypeError,
)
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
from music_assistant.helpers.aiohttp_client import create_clientsession
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)
from music_assistant.helpers.util import infer_album_type, parse_title_and_version
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.livephish.constants import (
    ACCOUNT_URL,
    AUTHORIZE_URL,
    CATALOG_API,
    CATALOG_HOST,
    CLIENT_ID,
    GRANT_TYPE,
    IMAGE_CDN,
    LEGACY_API_URL,
    REDIRECT_URI,
    SCOPE,
    STASH_API,
    STASH_URL,
    STREAM_API_URL,
    TOKEN_URL,
)

if TYPE_CHECKING:
    from aiohttp import ClientResponse, ClientSession
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_VERIFICATION_TOKEN = re.compile(
    r"""name=["']__RequestVerificationToken["'][^>]*value=["']([^"']+)["']"""
    r"""|value=["']([^"']+)["'][^>]*name=["']__RequestVerificationToken["']""",
    re.IGNORECASE,
)
_MAX_LOGIN_REDIRECTS = 8
_SECURE_IMAGE_HOST = "https://secure.livedownloads.com"

SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.BROWSE,
}

CONF_QUALITY = "quality"
QUALITY_LOSSY = "lossy"
QUALITY_LOSSLESS = "lossless"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return LivePhishProvider(mass, manifest, config, SUPPORTED_FEATURES)


class LivePhishProvider(MusicProvider):
    """Provider implementation for Live Phish."""

    throttler = ThrottlerManager(rate_limit=5, period=1)
    # the legacy search endpoint queues requests rather than returning 429s and slows
    # to timeouts above roughly one search per second, so it has its own tighter gate
    search_throttler = ThrottlerManager(rate_limit=1, period=1)

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._auth_token: str | None = None
        self._token_expiry = 0.0

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
        # a reload may follow a switch to another account, which cached details cannot tell
        for cached in (self._get_subscription_info, self._get_user_id):
            await self.mass.cache.delete(cached.__name__, provider=self.instance_id)
        await self.login()

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """
        Search the Live Phish catalog for artists, releases and recorded performances.

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
        """Retrieve artists from the Live Phish public catalog."""
        catalog = await self._get_legacy_catalog("catalog.artists")
        artists = catalog.get("artists") if isinstance(catalog, dict) else None
        if not isinstance(artists, list):
            return
        for item in artists:
            if not isinstance(item, dict):
                continue
            artist_id = item.get("artistID") or item.get("id")
            if artist_id and (item.get("artistName") or item.get("name")):
                yield self._parse_artist(item)

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve catalog shows for each Live Phish artist."""
        async for artist in self.get_library_artists():
            releases = await self._get_all_items("catalog", "releases", artistIds=artist.item_id)
            for item in releases:
                if item and item.get("id"):
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
        cache_key = f"livephish_track_{prov_track_id}"
        cached: Track | None = await self.mass.cache.get(
            cache_key, provider=self.instance_id, base_class=Track
        )
        if cached:
            return cached
        search_hit: Track | None = await self.mass.cache.get(
            f"livephish_search_track_{prov_track_id}",
            provider=self.instance_id,
            base_class=Track,
        )
        if search_hit and search_hit.album:
            for track in await self.get_album_tracks(search_hit.album.item_id):
                if track.item_id == prov_track_id:
                    await self._cache_tracks([track])
                    return track
        raise MediaNotFoundError(f"Track {prov_track_id} not found")

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all album tracks for given album id."""
        tracks = await self._load_album_tracks(prov_album_id)
        # The library list is numbered in catalog order, while Live Phish artist ids are not.
        # A playing track has to name the library artist, or the now-playing marker lands
        # on whichever library row shares the catalog id.
        for track in tracks:
            track.artists = UniqueList(
                [await self._library_artist(artist) for artist in track.artists]
            )
        return tracks

    @use_cache(3600, allow_expired_cache=True)  # Cache for 1 hour
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        if page > 0:
            # paging not yet supported
            return []
        endpoint = f"/playlists/{prov_playlist_id}/playlist-tracks/all"
        livephish_result = await self._get_data("stash", endpoint)
        for index, item in enumerate(livephish_result["items"], 1):
            track = self._parse_track(item)
            track.position = index
            result.append(track)
        await self._cache_tracks(result)
        return result

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse artists, years, songs, albums, and tours.

        Opening a song lists the shows that played that title.

        :param path: The path to browse, (e.g. provider_id://years/1997).
        """
        subpath = path.split("://", 1)[1] if "://" in path else path
        parts = [part for part in subpath.split("/") if part]
        if not parts:
            root = path if path.endswith("://") else f"{path.rstrip('/')}/"
            return self._browse_root(root)
        section, *rest = parts
        if len(rest) > 1:
            return []
        child = rest[0] if rest else None
        if section == "artist" and child is None:
            return [item async for item in self.get_library_artists()]
        if section == "years":
            return await self._browse_years(child)
        if section == "songs":
            return await self._browse_songs(child)
        if section == "albums" and child is None:
            return await self._browse_albums()
        if section == "tour":
            return await self._browse_tours(child)
        return []

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get this provider's available recommendation rows, without items."""
        return [
            RecommendationFolder(
                name="Most Popular",
                translation_key="livephish_popular_shows",
                item_id="livephish_popular_shows",
                provider=self.instance_id,
            ),
            RecommendationFolder(
                name="Recent Shows",
                translation_key="livephish_recent_shows",
                item_id="livephish_recent_shows",
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

    async def login(self) -> str:
        """Login to Live Phish and return the bearer token."""
        if self._auth_token and self._token_expiry > time():
            return self._auth_token
        username = self.get_setup_value(CONF_USERNAME)
        password = self.get_setup_value(CONF_PASSWORD)
        if not username or not password:
            msg = "Invalid login credentials"
            raise LoginFailed(msg)
        # This public client rejects grant_type=password with unauthorized_client.
        # Sign in through the identity server's login form, then exchange the code.
        code, verifier = await self._authorization_code(str(username), str(password))
        token = await self._exchange_code(code, verifier)
        access_token = token["access_token"]
        expires_in = token["expires_in"]
        self._auth_token = access_token
        self._token_expiry = time() + expires_in
        # Drop the token again unless the account endpoint accepts it. A later call
        # would otherwise reuse a bearer the API already rejected.
        accepted = False
        try:
            await self._get_account()
            accepted = True
        finally:
            if not accepted:
                self._auth_token = None
                self._token_expiry = 0
        return access_token

    async def _authorization_code(self, username: str, password: str) -> tuple[str, str]:
        """Sign in on the identity server and return the authorization code and PKCE verifier."""
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
        challenge = challenge.rstrip("=")
        # A fresh cookie jar so a previous identity session cannot replace these credentials.
        session = create_clientsession(self.mass)
        try:
            timeout = ClientTimeout(total=120)
            async with session.get(
                AUTHORIZE_URL,
                params={
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT_URI,
                    "response_type": "code",
                    "scope": SCOPE,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
                allow_redirects=True,
                timeout=timeout,
            ) as response:
                html = await response.text()
                login_url = str(response.url)
            verification = _verification_token(html)
            if verification is None:
                raise LoginFailed("Live Phish login page could not be read")
            async with session.post(
                login_url,
                data={
                    "Input.Email": username,
                    "Input.Password": password,
                    "Input.RememberLogin": "false",
                    "__RequestVerificationToken": verification,
                },
                allow_redirects=False,
                timeout=timeout,
            ) as response:
                code = await self._code_from_login(session, response)
            return code, verifier
        finally:
            await session.close()

    async def _code_from_login(self, session: ClientSession, response: ClientResponse) -> str:
        """Follow the login response until the player redirect carries an authorization code."""
        status = response.status
        location = response.headers.get("Location")
        current_url = str(response.url)
        body = "" if status in _REDIRECTS else await response.text()
        for _ in range(_MAX_LOGIN_REDIRECTS):
            if status in _REDIRECTS and location:
                next_url = urljoin(current_url, location)
                if code := _code_from_redirect(next_url):
                    return code
                timeout = ClientTimeout(total=120)
                async with session.get(next_url, allow_redirects=False, timeout=timeout) as hop:
                    status = hop.status
                    location = hop.headers.get("Location")
                    current_url = str(hop.url)
                    body = "" if status in _REDIRECTS else await hop.text()
                continue
            if "Invalid username or password" in body:
                raise LoginFailed("Invalid Live Phish username or password")
            if "valid e-mail" in body.lower():
                raise LoginFailed("Live Phish username must be the account email address")
            break
        raise LoginFailed("Live Phish login did not return an authorization code")

    async def _exchange_code(self, code: str, verifier: str) -> dict[str, Any]:
        """Exchange an authorization code for a token response."""
        timeout = ClientTimeout(total=120)
        async with self.mass.http_session.post(
            TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "grant_type": GRANT_TYPE,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
            ssl=True,
            timeout=timeout,
        ) as response:
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            if response.status >= 400:
                raise LoginFailed(await _token_error(response))
            return await response.json()

    async def _get_account(self) -> Any:
        """Return the authenticated account payload from the subscriptions endpoint."""
        headers = {"Authorization": f"Bearer {self._auth_token}"}
        timeout = ClientTimeout(total=120)
        async with self.mass.http_session.get(
            ACCOUNT_URL, headers=headers, ssl=True, timeout=timeout
        ) as response:
            if response.status == 401:
                raise LoginFailed("Live Phish authentication expired or was rejected")
            response.raise_for_status()
            return await response.json()

    async def _cache_tracks(self, tracks: list[Track], *, search_results: bool = False) -> None:
        """Cache tracks or partial search results for later lookup by get_track."""
        prefix = "livephish_search_track" if search_results else "livephish_track"
        for track in tracks:
            await self.mass.cache.set(
                f"{prefix}_{track.item_id}",
                track.to_dict(),
                expiration=3600 * 24 if search_results else 3600 * 24 * 14,
                provider=self.instance_id,
                persistent=not search_results,
            )

    def _parse_artist(self, artist_obj: dict[str, Any]) -> Artist:
        """Parse a Live Phish artist object to generic layout."""
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
                    url=f"{CATALOG_HOST}/api/v1/artists?ids={artist_id}",
                )
            },
        )
        image = None
        if isinstance(artist_obj.get("avatarImage"), dict):
            image = artist_obj["avatarImage"].get("url")
        elif artist_obj.get("artistImage"):
            image = _cover_url(str(artist_obj["artistImage"]))
        if image:
            artist.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        return artist

    def _parse_album(self, album_obj: dict[str, Any]) -> Album:
        """Parse a Live Phish release/show/album object to generic album layout."""
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
            path = _cover_url(album_obj["image"]["url"])
        if album_obj.get("img"):
            path = _cover_url(album_obj["img"]["url"])
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
        """Parse a Live Phish playlist object to generic layout."""
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
        """Parse a response from the Live Phish APIs to a Track model object."""
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
        """Discover catalog entities using the authenticated Live Phish search API."""
        data = await self._get_data("catalog", "search", query=query)
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise InvalidDataError("Invalid Live Phish search response")
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
                raise InvalidDataError("Invalid Live Phish releases response")
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

    @use_cache(3600 * 24)
    @throttle_with_retries
    async def _get_legacy_catalog(self, method: str, **params: Any) -> Any:
        """
        Return the payload of one public legacy catalog method.

        :param method: The ``method`` query value, such as ``catalog.artists``.
        :param params: Extra query parameters for that method.
        """
        try:
            async with (
                self.search_throttler.acquire(),
                self.mass.http_session.get(
                    LEGACY_API_URL,
                    params={"method": method, **params},
                    timeout=ClientTimeout(total=30),
                ) as response,
            ):
                self._check_response_status(response)
                # The legacy endpoint returns JSON with a text/html content type.
                data = json_loads(await response.text())
        except (ClientConnectionError, ClientPayloadError, TimeoutError) as err:
            raise ResourceTemporarilyUnavailable("Live Phish catalog connection failed") from err
        except ValueError as err:
            raise InvalidDataError("Invalid Live Phish catalog response") from err
        if not isinstance(data, dict) or data.get("responseAvailabilityCode") != 0:
            raise InvalidDataError("Live Phish catalog request failed")
        result = data.get("Response")
        if result is None:
            raise InvalidDataError("Invalid Live Phish catalog response")
        return result

    @use_cache(3600)
    @throttle_with_retries
    async def _get_search_data(self, query: str) -> dict[str, Any]:
        """Retrieve cached search metadata from the public Live Phish catalog."""
        try:
            async with (
                self.search_throttler.acquire(),
                self.mass.http_session.get(
                    LEGACY_API_URL,
                    params={"method": "catalog.search", "searchStr": query},
                    timeout=ClientTimeout(total=30),
                ) as response,
            ):
                self._check_response_status(response)
                # The legacy endpoint returns JSON with a text/html content type.
                data = json_loads(await response.text())
        except (ClientConnectionError, ClientPayloadError, TimeoutError) as err:
            raise ResourceTemporarilyUnavailable("Live Phish search connection failed") from err
        except ValueError as err:
            raise InvalidDataError("Invalid Live Phish catalog search response") from err
        if not isinstance(data, dict) or data.get("responseAvailabilityCode") != 0:
            raise InvalidDataError("Live Phish catalog search failed")
        result = data.get("Response")
        if not isinstance(result, dict):
            raise InvalidDataError("Invalid Live Phish catalog search response")
        if result.get("searchError"):
            raise InvalidDataError("Live Phish catalog search failed")
        return result

    # both are fixed for the account, so track starts spend no requests on account lookups
    @use_cache(3600 * 4)  # Cache for 4 hours
    async def _get_subscription_info(self) -> dict[str, Any]:
        """Return the active subscription for the Live Phish account."""
        # v3 /me/subscriptions is a list. Cache the chosen object, not the list,
        # because the cache rebuilds a dict and calls items() on the stored value.
        return _select_subscription(await self._get_data("subscription", ""))

    @use_cache(3600 * 4)  # Cache for 4 hours
    async def _get_user_id(self) -> str:
        """Return the user id of the Live Phish account."""
        user_info = await self._get_data("user", "")
        return str(user_info["userId"])

    @use_cache(3600 * 24 * 14, allow_expired_cache=True)  # Cache for 14 days
    async def _load_album_tracks(self, prov_album_id: str) -> list[Track]:
        """
        Return the show's tracks with their Live Phish artist ids.

        :param prov_album_id: The Live Phish show id.
        """
        endpoint = f"shows/{prov_album_id}"
        response = await self._get_data("catalog", endpoint)
        album_data = response["Response"]
        artist = await self.get_artist(album_data["artistID"])
        album = self._get_item_mapping(
            MediaType.ALBUM, str(album_data["containerID"]), album_data["containerInfo"]
        )
        image = _cover_url(album_data["img"]["url"]) if album_data.get("img") else None
        tracks: list[Track] = []
        for item in album_data["tracks"]:
            if not item.get("trackID"):
                continue
            track = self._parse_track(item, artist=artist, album=album, image_url=image)
            tracks.append(track)
            # The stream call's skuID is the product sku when the show has one,
            # and the track id when it does not.
            if sku := _product_sku(item):
                await self.mass.cache.set(
                    f"livephish_sku_{track.item_id}",
                    sku,
                    expiration=3600 * 24 * 14,
                    provider=self.instance_id,
                )
        await self._cache_tracks(tracks)
        return tracks

    async def _library_artist(self, artist: Artist | ItemMapping) -> Artist | ItemMapping:
        """
        Return the library artist for a Live Phish artist when it has been saved.

        :param artist: The artist on a provider track.
        """
        if artist.provider == "library":
            return artist
        library_artist = await self.mass.music.artists.get_library_item_by_prov_id(
            artist.item_id, self.instance_id
        )
        if library_artist is None:
            return artist
        return ItemMapping.from_item(library_artist)

    async def _container_id(self, track_id: str) -> str:
        """
        Return the show id the stream API expects alongside a track.

        :param track_id: The Live Phish track id.
        """
        track = await self.get_track(track_id)
        album_id = track.album.item_id if track.album is not None else None
        if not album_id:
            raise MediaNotFoundError(f"No show found for track {track_id}")
        return str(album_id)

    async def _get_stream_url(self, item_id: str) -> str:
        """
        Return the HLS stream url for a track.

        The player requests ``subplayer.aspx`` with the track as ``skuID`` and the
        show as ``containerID``. The plan decides the stream; the call does not
        send a quality id.

        :param item_id: The Live Phish track id.
        """
        subscription_info = await self._get_subscription_info()
        access_id = self._subscription_access(subscription_info)[1]
        container_id = await self._container_id(item_id)
        account: dict[str, Any] = {
            "subCostplanIDAccessList": access_id,
            "startDateStamp": _unix_stamp(subscription_info["startedAt"]),
            "endDateStamp": _unix_stamp(subscription_info["endsAt"]),
            "nn_userID": await self._get_user_id(),
            "subscriptionID": subscription_info["legacySubscriptionId"],
            "app": 1,
        }
        if stream_url := await self._request_stream_link(
            {**account, "skuID": item_id, "containerID": container_id, "v": 2}
        ):
            return stream_url
        product_sku = await self._cached_sku(item_id) or await self._sku_from_show(
            container_id, item_id
        )
        if product_sku and product_sku != item_id:
            if stream_url := await self._request_stream_link(
                {**account, "skuID": product_sku, "containerID": container_id, "v": 2}
            ):
                return stream_url
        # The player SDK also resolves audio with method=subPlayer and the track id.
        if stream_url := await self._request_stream_link(
            {
                **account,
                "platformID": 1,
                "HLS": 1,
                "orgn": "websdk",
                "method": "subPlayer",
                "trackId": item_id,
            }
        ):
            return stream_url
        raise MediaNotFoundError(f"No stream found for song {item_id}.")

    def _subscription_access(self, subscription_info: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """
        Return the plan object and the cost-plan id the stream API expects.

        The player reads ``plan.planId`` first, then a promo plan id, then ``plan.id``.

        :param subscription_info: The account subscription payload.
        """
        plan = subscription_info.get("plan") or {}
        promo_plan = (subscription_info.get("promo") or {}).get("plan") or {}
        if not isinstance(plan, dict):
            plan = {}
        if not isinstance(promo_plan, dict):
            promo_plan = {}
        active = plan or promo_plan
        access_id = plan.get("planId") or promo_plan.get("id") or plan.get("id")
        if not active or not access_id:
            msg = "No active Live Phish subscription found for this account"
            raise AudioError(msg)
        return active, str(access_id)

    async def _cached_sku(self, track_id: str) -> str | None:
        """Return a product sku cached while loading the show, when one exists."""
        cached = await self.mass.cache.get(f"livephish_sku_{track_id}", provider=self.instance_id)
        if isinstance(cached, str) and cached:
            return cached
        return None

    async def _sku_from_show(self, show_id: str, track_id: str) -> str | None:
        """
        Return the product sku for a track on a show.

        :param show_id: The Live Phish show id.
        :param track_id: The Live Phish track id.
        """
        try:
            response = await self._get_data("catalog", f"shows/{show_id}")
        except (
            InvalidDataError,
            LoginFailed,
            MediaNotFoundError,
            RateLimited,
            ResourceTemporarilyUnavailable,
        ):
            return None
        album = response.get("Response") if isinstance(response, dict) else None
        if not isinstance(album, dict):
            return None
        for item in album.get("tracks") or []:
            if not isinstance(item, dict) or str(item.get("trackID")) != track_id:
                continue
            sku = _product_sku(item)
            if sku:
                await self.mass.cache.set(
                    f"livephish_sku_{track_id}",
                    sku,
                    expiration=3600 * 24 * 14,
                    provider=self.instance_id,
                )
            return sku
        return None

    async def _request_stream_link(self, params: dict[str, Any]) -> str | None:
        """
        Request a stream link from the Live Phish stream API, returning None when there is none.

        :param params: The query parameters for the player stream request.
        """
        timeout = ClientTimeout(total=120)
        try:
            async with self.mass.http_session.get(
                STREAM_API_URL,
                params={**params, **_subplayer_proof()},
                ssl=True,
                timeout=timeout,
            ) as response:
                try:
                    response.raise_for_status()
                except ClientResponseError:
                    return None
                body = await response.text()
        except ClientConnectionError, ClientPayloadError, TimeoutError:
            return None
        stream = _stream_payload(body)
        if not isinstance(stream, dict):
            return None
        return _playable_stream_link(stream.get("streamLink"))

    def _get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.instance_id,
            name=name,
        )

    @throttle_with_retries
    async def _get_data(self, /, api: str, endpoint: str, **kwargs: Any) -> Any:
        """Return the requested data from one of the Live Phish APIs."""
        url = _api_url(api, endpoint)
        timeout = ClientTimeout(total=120)
        tokeninfo = kwargs.pop("tokeninfo", None)
        if tokeninfo is None:
            tokeninfo = await self.login()
        headers = {"Authorization": f"Bearer {tokeninfo}"}
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
            raise ResourceTemporarilyUnavailable("Live Phish API connection failed") from err
        except (ContentTypeError, ValueError) as err:
            raise InvalidDataError("Invalid Live Phish API response") from err

    @staticmethod
    def _check_response_status(response: ClientResponse) -> None:
        """Translate Live Phish HTTP errors into provider errors."""
        if response.status == 429:
            raise RateLimited(
                "Live Phish API rate limited",
                backoff_time=parse_retry_after(response.headers.get("Retry-After")),
            )
        if response.status in (500, 502, 503, 504):
            raise ResourceTemporarilyUnavailable("Live Phish API temporarily unavailable")
        if response.status == 401:
            raise LoginFailed("Live Phish authentication expired or was rejected")
        if response.status == 404:
            raise MediaNotFoundError("Live Phish item not found")
        if response.status >= 400:
            raise InvalidDataError(f"Live Phish API request failed with HTTP {response.status}")

    async def _get_all_items(self, api: str, endpoint: str, **kwargs: Any) -> list[dict[str, Any]]:
        limit = 100
        offset = 0
        all_items: list[dict[str, Any]] = []
        while True:
            kwargs["limit"] = limit
            kwargs["offset"] = offset
            result = await self._get_data(api, endpoint, **kwargs)
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
        if item_id == "livephish_popular_shows":
            popular_data = await self._get_data("catalog", "releases/popular", limit=20)
            albums = []
            for item in popular_data["items"]:
                response = await self._get_data("catalog", f"shows/{item['id']}")
                albums.append(self._parse_album(response["Response"]))
        else:  # livephish_recent_shows
            recent_data = await self._get_data("catalog", "releases/recent", limit=50)
            albums = [self._parse_album(item) for item in recent_data["items"]]
        folder.items = UniqueList(albums)
        return folder

    def _browse_root(self, path: str) -> list[BrowseFolder]:
        """Return the five browse sections from the Live Phish app."""
        sections = (
            ("artist", "Artist", "artist"),
            ("years", "Years", "years"),
            ("songs", "Songs", "songs"),
            ("albums", "Albums", "albums"),
            ("tour", "Tour", "tour"),
        )
        return [
            BrowseFolder(
                item_id=item_id,
                provider=self.instance_id,
                path=f"{path}{item_id}",
                name=name,
                translation_key=translation_key,
                is_playable=False,
            )
            for item_id, name, translation_key in sections
        ]

    async def _browse_years(self, year: str | None) -> list[BrowseFolder | Album]:
        """Return performance years, or the shows from one year."""
        if year is None:
            payload = await self._get_legacy_catalog("catalog.showYearCatalog")
            if not isinstance(payload, list):
                raise InvalidDataError("Invalid Live Phish catalog response")
            # The catalog lists the oldest year first. Newest first matches a show archive.
            years = sorted({str(item) for item in payload if str(item).isdigit()}, reverse=True)
            return [
                BrowseFolder(
                    item_id=item,
                    provider=self.instance_id,
                    path=f"{self.instance_id}://years/{item}",
                    name=item,
                    is_playable=False,
                )
                for item in years
            ]
        if not year.isdigit():
            return []
        containers = await self._get_legacy_containers(
            showYears=year,
            sortBy="performanceDate",
            sortType="desc",
            startOffset=1,
        )
        return self._albums_from_containers(containers)

    async def _browse_songs(self, song_id: str | None) -> list[BrowseFolder | Album]:
        """Return song titles, or the shows that played one title."""
        payload = await self._get_legacy_catalog("catalog.songCatalog")
        if not isinstance(payload, list):
            raise InvalidDataError("Invalid Live Phish catalog response")
        songs = [
            item
            for item in payload
            if isinstance(item, dict) and item.get("ID") and item.get("title")
        ]
        if song_id is None:
            return [
                BrowseFolder(
                    item_id=str(item["ID"]),
                    provider=self.instance_id,
                    path=f"{self.instance_id}://songs/{item['ID']}",
                    name=str(item["title"]),
                    is_playable=False,
                )
                for item in songs
            ]
        match = next((item for item in songs if str(item["ID"]) == song_id), None)
        if match is None:
            return []
        containers = await self._get_legacy_containers(
            songsPlayed=match["title"],
            startOffset=1,
            availType=1,
        )
        return self._albums_from_containers(containers)

    async def _browse_albums(self) -> list[Album]:
        """Return studio albums, the rows the Albums page loads by title."""
        containers = await self._get_legacy_containers(
            sortBy="albumTitle",
            sortType="asc",
            startOffset=1,
        )
        return self._albums_from_containers(containers)

    async def _browse_tours(self, category_id: str | None) -> list[BrowseFolder | Album]:
        """Return tour categories, or the shows in one category."""
        if category_id is None:
            payload = await self._get_legacy_catalog("catalog.containerCategories")
            categories = payload.get("containerCategories") if isinstance(payload, dict) else None
            if not isinstance(categories, list):
                raise InvalidDataError("Invalid Live Phish catalog response")
            folders: list[BrowseFolder | Album] = []
            for item in categories:
                if not isinstance(item, dict) or not item.get("containerCategoryID"):
                    continue
                name = item.get("containerCategoryName") or ""
                if not name:
                    continue
                image = None
                cover = item.get("img")
                if isinstance(cover, dict) and (url := _cover_url(cover.get("url"))):
                    image = MediaItemImage(
                        type=ImageType.THUMB,
                        path=url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                category = str(item["containerCategoryID"])
                folders.append(
                    BrowseFolder(
                        item_id=category,
                        provider=self.instance_id,
                        path=f"{self.instance_id}://tour/{category}",
                        name=str(name),
                        image=image,
                        is_playable=False,
                    )
                )
            return folders
        if not category_id.isdigit():
            return []
        containers = await self._get_legacy_containers(
            containerCategoryID=category_id,
            startOffset=1,
            availtype=1,
        )
        return self._albums_from_containers(containers)

    async def _get_legacy_containers(self, **params: Any) -> list[dict[str, Any]]:
        """Return show or album rows from catalog.containersAll."""
        payload = await self._get_legacy_catalog("catalog.containersAll", **params)
        containers = payload.get("containers") if isinstance(payload, dict) else None
        if not isinstance(containers, list):
            raise InvalidDataError("Invalid Live Phish catalog response")
        return [item for item in containers if isinstance(item, dict)]

    def _albums_from_containers(self, containers: list[dict[str, Any]]) -> list[Album]:
        """Parse container rows that have an id, a title, and an artist."""
        albums: list[Album] = []
        for item in containers:
            has_artist = bool(item.get("artist")) or (
                item.get("artistID") and item.get("artistName")
            )
            if item.get("containerID") and item.get("containerInfo") and has_artist:
                albums.append(self._parse_album(item))
        return albums


def _select_subscription(payload: Any) -> dict[str, Any]:
    """Return the active subscription from a v3 list or a single subscription object."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        entries = [item for item in payload if isinstance(item, dict)]
        accessible = [item for item in entries if item.get("isContentAccessible")]
        chosen = accessible[0] if accessible else None
        if chosen is None:
            chosen = next((item for item in entries if item.get("plan") or item.get("promo")), None)
        if chosen is None and entries:
            chosen = entries[0]
        if chosen is not None:
            return chosen
    raise AudioError("No active Live Phish subscription found for this account")


def _unix_stamp(value: Any) -> int:
    """Return a subscription date as a unix timestamp."""
    if isinstance(value, bool):
        raise InvalidDataError("Live Phish subscription dates could not be read")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    for fmt in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text[:19], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
        return int(parsed.timestamp())
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as err:
        raise InvalidDataError("Live Phish subscription dates could not be read") from err
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _verification_token(html: str) -> str | None:
    """Return the antiforgery token from the identity login page."""
    match = _VERIFICATION_TOKEN.search(html)
    if match is None:
        return None
    return match.group(1) or match.group(2)


def _code_from_redirect(url: str) -> str | None:
    """Return the authorization code when the URL is the player's redirect."""
    parsed = urlparse(url)
    expected = urlparse(REDIRECT_URI)
    if (parsed.scheme, parsed.netloc, parsed.path) != (
        expected.scheme,
        expected.netloc,
        expected.path,
    ):
        return None
    codes = parse_qs(parsed.query).get("code") or []
    return codes[0] if codes and codes[0] else None


async def _token_error(response: ClientResponse) -> str:
    """Return a login failure message from a rejected token response."""
    try:
        payload = await response.json(content_type=None)
    except ContentTypeError, ValueError:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    if error:
        return f"Live Phish login failed ({error})"
    return f"Live Phish login failed (HTTP {response.status})"


def _cover_url(url: str | None) -> str | None:
    """Resolve a show image the way the player does, against its image CDN."""
    if not url:
        return None
    if url.startswith(_SECURE_IMAGE_HOST):
        return IMAGE_CDN + url.removeprefix(_SECURE_IMAGE_HOST)
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("/"):
        return f"{IMAGE_CDN}{url}"
    return f"{IMAGE_CDN}/{url}"


def _product_sku(track_obj: dict[str, Any]) -> str | None:
    """Return the show track's product sku, when the catalog lists one."""
    for product in track_obj.get("products") or []:
        if isinstance(product, dict) and product.get("skuID"):
            return str(product["skuID"])
    return None


def _rewrite_stream_link(url: str) -> str:
    """Rewrite a stream host the way the player does before playback."""
    replacements = (
        ("http://nugslpmobile.edgesuite.net", "https://nugslpmobile.akamaized.net"),
        ("http://nugs2.edgesuite.net", "https://securestream02.akamaized.net"),
        ("http://nugs10.edgesuite.net", "https://nugs10.akamaized.net"),
        ("http://nugsuniversal-f.akamaihd.net", "https://nugsuniversal-f.akamaihd.net"),
    )
    for old, new in replacements:
        url = url.replace(old, new)
    return url


def _playable_stream_link(url: Any) -> str | None:
    """Return a stream link that points at a media file, after the player's host rewrite."""
    if not isinstance(url, str) or not url:
        return None
    rewritten = _rewrite_stream_link(url)
    # A link whose path is only master.m3u8 has no media directory. Akamai answers 403.
    if urlparse(rewritten).path.strip("/") in {"", "master.m3u8"}:
        return None
    return rewritten


def _stream_payload(body: str) -> Any:
    """Return the stream response object from JSON or a JSONP wrapper."""
    try:
        return json_loads(body)
    except ValueError:
        start = body.find("{")
        end = body.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json_loads(body[start : end + 1])
        except ValueError:
            return None


def _subplayer_proof() -> dict[str, str]:
    """Return the short-lived proof the Live Phish player attaches to subplayer requests."""
    # lxp is a millisecond timestamp 60s ahead; tk is md5 of the player's fixed prefix plus lxp.
    expires = f"{int(time() * 1000) + 60_000}.000000"
    digest = hashlib.md5(f"jdfirj8475jf_{expires}".encode(), usedforsecurity=False).hexdigest()
    return {"lxp": expires, "tk": digest}


def _api_url(api: str, endpoint: str) -> str:
    """Return the Live Phish URL for a catalog, stash, subscription, or user call."""
    endpoint = endpoint.removeprefix("/")
    if api == "catalog":
        return f"{CATALOG_API}/{endpoint}" if endpoint else CATALOG_API
    if api == "stash":
        return f"{STASH_API}/me/{endpoint}" if endpoint else f"{STASH_API}/me"
    if api == "subscription":
        return ACCOUNT_URL
    if api == "user":
        return STASH_URL
    raise MediaNotFoundError(f"{api} not found")
