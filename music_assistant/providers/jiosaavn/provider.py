"""JioSaavn Music Provider for Music Assistant."""

from __future__ import annotations

import contextlib
import html
from typing import Any

import aiohttp
from music_assistant_models.enums import ContentType, ImageType, MediaType, StreamType
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
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
    DEFAULT_HEADERS,
    SEARCH_ENDPOINT,
    SONG_DETAILS_ENDPOINT,
)
from .helpers import decrypt_stream_url, parse_album, parse_artist, parse_track


class JioSaavnProvider(MusicProvider):
    """JioSaavn Music Provider."""

    async def handle_async_init(self) -> None:
        """Handle async initialization."""
        # Test connection
        try:
            await self._make_request(SEARCH_ENDPOINT, {"q": "test", "n": "1"})
        except aiohttp.ClientError as err:
            self.logger.error("Failed to connect to JioSaavn: %s", err)

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

        try:
            data = await self._make_request(SEARCH_ENDPOINT, params)
        except aiohttp.ClientError as err:
            self.logger.warning("Search request failed: %s", err)
            return result

        self.logger.debug("Search response: %s", data)

        if not data or "results" not in data:
            self.logger.warning("No results in search response")
            return result

        results_data = data["results"]

        # Parse artists
        if MediaType.ARTIST in media_types and not result.artists:
            seen_ids = set()
            artists: list[Artist] = []

            for song_data in results_data[:limit]:
                for artist_data in (
                    song_data.get("more_info", {}).get("artistMap", {}).get("primary_artists", [])
                ):
                    aid = artist_data.get("id")
                    if aid and aid not in seen_ids:
                        seen_ids.add(aid)
                        with contextlib.suppress(KeyError, TypeError, InvalidDataError):
                            artists.append(parse_artist(artist_data, self.instance_id, self.domain))

            result.artists = artists[:limit]

        # Parse albums
        if MediaType.ALBUM in media_types:
            albums: list[Album] = []
            seen_album_ids = set()

            for song_data in results_data[:limit]:
                more_info = song_data.get("more_info", {})
                album_id = more_info.get("album_id")
                album_name = more_info.get("album")

                if album_id and album_id not in seen_album_ids:
                    seen_album_ids.add(album_id)
                    try:
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
                    except (KeyError, TypeError, InvalidDataError):
                        pass

            result.albums = albums[:limit]

        # Parse tracks
        if MediaType.TRACK in media_types:
            tracks: list[Track] = []
            for track_data in results_data[:limit]:  # results_data is already the list
                try:
                    tracks.append(parse_track(track_data, self.instance_id, self.domain))
                except (KeyError, TypeError, InvalidDataError) as err:
                    self.logger.debug("Skipping track with invalid data: %s", err)
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

        try:
            data = await self._make_request(ARTIST_DETAILS_ENDPOINT, params)
        except aiohttp.ClientError as err:
            raise MediaNotFoundError(f"Failed to get artist {prov_artist_id}") from err

        if not data:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")

        try:
            return parse_artist(data, self.instance_id, self.domain)
        except (KeyError, TypeError) as err:
            raise InvalidDataError(f"Invalid artist data for {prov_artist_id}") from err

    @use_cache()
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details."""
        params = {
            "albumid": prov_album_id,
            "cc": "in",
        }

        try:
            data = await self._make_request(ALBUM_DETAILS_ENDPOINT, params)
        except aiohttp.ClientError as err:
            raise MediaNotFoundError(f"Failed to get album {prov_album_id}") from err

        if not data:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")

        try:
            return parse_album(data, self.instance_id, self.domain)
        except (KeyError, TypeError) as err:
            raise InvalidDataError(f"Invalid album data for {prov_album_id}") from err

    @use_cache()
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details."""
        params = {"pids": prov_track_id}

        try:
            data = await self._make_request(SONG_DETAILS_ENDPOINT, params)
        except aiohttp.ClientError as err:
            raise MediaNotFoundError(f"Failed to get track {prov_track_id}") from err

        if not data or "songs" not in data or not data["songs"]:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")

        track_data = data["songs"][0]  # First song in the array

        try:
            return parse_track(track_data, self.instance_id, self.domain)
        except (KeyError, TypeError) as err:
            raise InvalidDataError(f"Invalid track data for {prov_track_id}") from err

    @use_cache()
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks."""
        params = {
            "albumid": prov_album_id,
            "cc": "in",
        }

        try:
            data = await self._make_request(ALBUM_DETAILS_ENDPOINT, params)
        except aiohttp.ClientError as err:
            self.logger.warning("Failed to get album tracks for %s: %s", prov_album_id, err)
            return []

        if not data or "list" not in data:
            return []

        tracks = []
        for track_data in data.get("list", []):
            try:
                tracks.append(parse_track(track_data, self.instance_id, self.domain))
            except (KeyError, TypeError, InvalidDataError) as err:
                self.logger.debug("Skipping track with invalid data: %s", err)

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

        try:
            data = await self._make_request(ARTIST_DETAILS_ENDPOINT, params)
        except aiohttp.ClientError as err:
            self.logger.warning("Failed to get albums for artist %s: %s", prov_artist_id, err)
            return []

        if not data or "topAlbums" not in data:
            return []

        albums = []
        # topAlbums has an 'albums' key with the list
        for album_data in data.get("topAlbums", []):
            try:
                albums.append(parse_album(album_data, self.instance_id, self.domain))
            except (KeyError, TypeError, InvalidDataError) as err:
                self.logger.debug("Skipping album with invalid data: %s", err)

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

        try:
            data = await self._make_request(ARTIST_DETAILS_ENDPOINT, params)
        except aiohttp.ClientError as err:
            self.logger.warning("Failed to get top tracks for artist %s: %s", prov_artist_id, err)
            return []

        if not data or "topSongs" not in data:
            return []

        tracks = []
        # topSongs has a 'songs' key with the list
        for track_data in data.get("topSongs", []):
            try:
                tracks.append(parse_track(track_data, self.instance_id, self.domain))
            except (KeyError, TypeError, InvalidDataError) as err:
                self.logger.debug("Skipping track with invalid data: %s", err)

        return tracks

    async def get_stream_details(
        self,
        item_id: str,
        media_type: MediaType = MediaType.TRACK,
    ) -> StreamDetails:
        """Get stream details for a track."""
        if media_type != MediaType.TRACK:
            raise MediaNotFoundError(f"Unsupported media type: {media_type}")

        # Get track details
        params = {
            "pids": item_id,
            "cc": "in",
        }

        try:
            data = await self._make_request(SONG_DETAILS_ENDPOINT, params)
        except aiohttp.ClientError as err:
            raise MediaNotFoundError(f"Failed to get track {item_id}") from err

        if not data or not isinstance(data, dict):
            raise MediaNotFoundError(f"Track {item_id} not found")

        if not data or "songs" not in data or not data["songs"]:
            raise MediaNotFoundError(f"Track {item_id} not found in response")

        track_data = data["songs"][0]

        # Get encrypted media URL
        encrypted_url = track_data.get("more_info", {}).get("encrypted_media_url")
        if not encrypted_url:
            raise MediaNotFoundError(f"No stream URL available for track {item_id}")

        # Decrypt the URL
        try:
            stream_url = decrypt_stream_url(encrypted_url)
        except InvalidDataError as err:
            raise MediaNotFoundError(f"Failed to decrypt stream URL for {item_id}") from err

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

    async def _make_request(
        self, endpoint: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Make API request to JioSaavn."""
        request_params: dict[str, str] = {
            "__call": endpoint,
            "api_version": "4",
            "_format": "json",
            "_marker": "0",
            "ctx": "web6dot0",
        }

        if params:
            request_params.update(params)

        async with self.mass.http_session.get(
            BASE_URL, params=request_params, headers=DEFAULT_HEADERS
        ) as response:
            response.raise_for_status()
            # JioSaavn returns JSON with wrong content-type
            result: dict[str, Any] = await response.json(content_type=None)
            return result
