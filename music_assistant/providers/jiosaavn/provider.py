"""JioSaavn Music Provider for Music Assistant."""

from __future__ import annotations

import contextlib
import html
from typing import Any

import aiohttp
from music_assistant_models.enums import ContentType, ImageType, MediaType, StreamType
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    MediaItemImage,
    ProviderMapping,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    ALBUM_DETAILS_ENDPOINT,
    ARTIST_DETAILS_ENDPOINT,
    BASE_URL,
    SEARCH_ENDPOINT,
    SONG_DETAILS_ENDPOINT,
)
from .helpers import decrypt_stream_url, parse_album, parse_artist, parse_track


class JioSaavnProvider(MusicProvider):
    """JioSaavn Music Provider."""

    async def handle_async_init(self) -> None:
        """Handle async initialization."""
        # Test connection - _make_request handles all errors
        await self._make_request(SEARCH_ENDPOINT, {"q": "test", "n": "1", "p": "0"})

    @property
    def is_streaming_provider(self) -> bool:
        """Return True - JioSaavn is a streaming provider."""
        return True

    @use_cache()
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on JioSaavn."""
        result = SearchResults()

        params = {
            "q": search_query,
            "n": str(limit),
            "p": "0",
        }

        # _make_request handles connection errors
        data = await self._make_request(SEARCH_ENDPOINT, params)

        self.logger.debug("Search response: %s", data)

        if not data or "results" not in data:
            self.logger.warning("No results in search response")
            return result

        results_data = data["results"]

        # Parse artists
        if MediaType.ARTIST in media_types and not result.artists:
            seen_ids: set[str] = set()
            artists: list[Artist] = []

            for song_data in results_data[:limit]:
                artist_map = song_data.get("more_info", {}).get("artistMap", {})
                primary_artists = artist_map.get("primary_artists", [])

                for artist_data in primary_artists:
                    aid = artist_data.get("id")
                    if aid and aid not in seen_ids:
                        seen_ids.add(aid)
                        with contextlib.suppress(InvalidDataError):
                            artists.append(parse_artist(artist_data, self.instance_id, self.domain))

            result.artists = artists[:limit]

        # Parse albums
        if MediaType.ALBUM in media_types:
            albums: list[Album] = []
            seen_album_ids: set[str] = set()

            for song_data in results_data[:limit]:
                more_info = song_data.get("more_info", {})
                album_id = more_info.get("album_id")
                album_name = more_info.get("album")

                if album_id and album_id not in seen_album_ids:
                    seen_album_ids.add(album_id)
                    with contextlib.suppress(InvalidDataError):
                        album = Album(
                            item_id=str(album_id),
                            provider=self.instance_id,
                            name=html.unescape(album_name or ""),
                            provider_mappings={
                                ProviderMapping(
                                    item_id=str(album_id),
                                    provider_domain=self.domain,
                                    provider_instance=self.instance_id,
                                    available=True,
                                    audio_format=AudioFormat(
                                        content_type=ContentType.AAC,
                                        bit_rate=320,
                                    ),
                                )
                            },
                        )

                        # Add image
                        if image_url := song_data.get("image"):
                            album.metadata.images = UniqueList(
                                [
                                    MediaItemImage(
                                        type=ImageType.THUMB,
                                        path=image_url.replace("150x150", "500x500"),
                                        provider=self.instance_id,
                                        remotely_accessible=True,
                                    )
                                ]
                            )

                        albums.append(album)

            result.albums = albums[:limit]

        # Parse tracks
        if MediaType.TRACK in media_types:
            tracks: list[Track] = []
            for track_data in results_data[:limit]:
                with contextlib.suppress(InvalidDataError):
                    tracks.append(parse_track(track_data, self.instance_id, self.domain))
            result.tracks = tracks

        return result

    @use_cache()
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details."""
        params = {
            "artistId": prov_artist_id,
            "n_song": "1",
            "n_album": "1",
            "cc": "in",
        }

        data = await self._make_request(ARTIST_DETAILS_ENDPOINT, params)

        if not data:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")

        return parse_artist(data, self.instance_id, self.domain)

    @use_cache()
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details."""
        params = {
            "albumid": prov_album_id,
            "cc": "in",
        }

        data = await self._make_request(ALBUM_DETAILS_ENDPOINT, params)

        if not data:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")

        return parse_album(data, self.instance_id, self.domain)

    @use_cache()
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details."""
        params = {"pids": prov_track_id}

        data = await self._make_request(SONG_DETAILS_ENDPOINT, params)

        if not data or "songs" not in data or not data["songs"]:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")

        track_data = data["songs"][0]

        return parse_track(track_data, self.instance_id, self.domain)

    @use_cache()
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks."""
        params = {
            "albumid": prov_album_id,
            "cc": "in",
        }

        data = await self._make_request(ALBUM_DETAILS_ENDPOINT, params)

        if not data or "list" not in data:
            return []

        tracks = []
        for track_data in data.get("list", []):
            with contextlib.suppress(InvalidDataError):
                tracks.append(parse_track(track_data, self.instance_id, self.domain))

        return tracks

    @use_cache()
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get albums by artist."""
        params = {
            "artistId": prov_artist_id,
            "n_song": "0",
            "n_album": "50",
            "cc": "in",
        }

        data = await self._make_request(ARTIST_DETAILS_ENDPOINT, params)

        if not data or "topAlbums" not in data:
            return []

        albums = []
        for album_data in data.get("topAlbums", []):
            with contextlib.suppress(InvalidDataError):
                albums.append(parse_album(album_data, self.instance_id, self.domain))

        return albums

    @use_cache(86400 * 7)  # 7 days
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top tracks by artist."""
        params = {
            "artistId": prov_artist_id,
            "n_song": "50",
            "n_album": "0",
            "cc": "in",
        }

        data = await self._make_request(ARTIST_DETAILS_ENDPOINT, params)

        if not data or "topSongs" not in data:
            return []

        tracks = []
        for track_data in data.get("topSongs", []):
            with contextlib.suppress(InvalidDataError):
                tracks.append(parse_track(track_data, self.instance_id, self.domain))

        return tracks

    async def get_stream_details(
        self,
        item_id: str,
        media_type: MediaType = MediaType.TRACK,
    ) -> StreamDetails:
        """Get stream details for a track."""
        if media_type != MediaType.TRACK:
            raise MediaNotFoundError(f"Unsupported media type: {media_type}")

        params = {
            "pids": item_id,
            "cc": "in",
        }

        data = await self._make_request(SONG_DETAILS_ENDPOINT, params)

        if not data or not isinstance(data, dict):
            raise MediaNotFoundError(f"Track {item_id} not found")

        if "songs" not in data or not data["songs"]:
            raise MediaNotFoundError(f"Track {item_id} not found in response")

        track_data = data["songs"][0]

        # Get encrypted media URL
        encrypted_url = track_data.get("more_info", {}).get("encrypted_media_url")
        if not encrypted_url:
            raise MediaNotFoundError(f"No stream URL available for track {item_id}")

        # Decrypt the URL - raises InvalidDataError if it fails
        stream_url = decrypt_stream_url(encrypted_url)

        # Get duration
        duration_str = track_data.get("duration") or "0"
        try:
            duration = int(duration_str)
        except (ValueError, TypeError):
            duration = 0

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.AAC,
                bit_rate=320,
            ),
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,
            path=stream_url,
            duration=duration,
            can_seek=True,
            allow_seek=True,
        )

    async def _make_request(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """Make API request to JioSaavn API.

        Raises:
            SetupFailedError: During initialization if connection fails
            LoginFailed: For 401/403 authentication errors
            MediaNotFoundError: For 404 errors
            ProviderUnavailableError: For all other HTTP/network errors
        """
        full_params = {
            **params,
            "_format": "json",
            "_marker": "0",
            "ctx": "web6dot0",
            "api_version": "4",
        }

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }

        url = f"{BASE_URL}?__call={endpoint}"

        try:
            async with self.mass.http_session.get(
                url,
                params=full_params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                response.raise_for_status()
                data: dict[str, Any] = await response.json()
                return data
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403):
                raise LoginFailed(f"JioSaavn API authentication failed: {err}") from err
            if err.status == 404:
                raise MediaNotFoundError(f"Resource not found: {endpoint}") from err
            # All other HTTP errors (500, 503, etc.)
            raise ProviderUnavailableError(f"JioSaavn API error: {err.status}") from err
        except aiohttp.ClientError as err:
            # Network errors, timeouts, etc.
            raise ProviderUnavailableError(f"JioSaavn connection error: {err}") from err
