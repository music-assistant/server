"""JioSaavn Music Provider for Music Assistant."""

from __future__ import annotations

from typing import Any

import aiohttp
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import InvalidDataError, LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    Playlist,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

from .constants import API_URL, DEFAULT_HEADERS
from .helpers import parse_album, parse_artist, parse_playlist, parse_track


class JioSaavnProvider(MusicProvider):
    """JioSaavn Music Provider."""

    async def handle_async_init(self) -> None:
        """Handle async initialization."""
        # Test connection
        try:
            await self._make_request("content.getFeaturedPlaylists", {"n": "1", "p": "1"})
        except aiohttp.ClientError as err:
            raise LoginFailed(f"Failed to connect to JioSaavn: {err}") from err

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close."""
        # No cleanup needed since we use mass.http_session

    @property
    def is_streaming_provider(self) -> bool:
        """Return True - JioSaavn is a streaming provider."""
        return True

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on JioSaavn."""
        result = SearchResults()

        params = {
            "query": search_query,
        }

        try:
            data = await self._make_request("autocomplete.get", params)
        except aiohttp.ClientError as err:
            self.logger.warning("Search request failed: %s", err)
            return result

        if not data:
            return result

        # Parse artists
        if MediaType.ARTIST in media_types:
            artists_data = data.get("artists", {}).get("data", [])
            artists: list[Artist] = []
            for artist_data in artists_data[:limit]:
                try:
                    artists.append(parse_artist(artist_data, self.instance_id, self.domain))
                except (KeyError, TypeError) as err:
                    self.logger.debug("Skipping artist with invalid data: %s", err)
            result.artists = artists

        # Parse albums
        if MediaType.ALBUM in media_types:
            albums_data = data.get("albums", {}).get("data", [])
            albums: list[Album] = []
            for album_data in albums_data[:limit]:
                try:
                    albums.append(parse_album(album_data, self.instance_id, self.domain))
                except (KeyError, TypeError) as err:
                    self.logger.debug("Skipping album with invalid data: %s", err)
            result.albums = albums

        # Parse tracks
        if MediaType.TRACK in media_types:
            tracks_data = data.get("songs", {}).get("data", [])
            tracks: list[Track] = []
            for track_data in tracks_data[:limit]:
                try:
                    tracks.append(parse_track(track_data, self.instance_id, self.domain))
                except (KeyError, TypeError) as err:
                    self.logger.debug("Skipping track with invalid data: %s", err)
            result.tracks = tracks

        # Parse playlists
        if MediaType.PLAYLIST in media_types:
            playlists_data = data.get("playlists", {}).get("data", [])
            playlists: list[Playlist] = []
            for playlist_data in playlists_data[:limit]:
                try:
                    playlists.append(parse_playlist(playlist_data, self.instance_id, self.domain))
                except (KeyError, TypeError) as err:
                    self.logger.debug("Skipping playlist with invalid data: %s", err)
            result.playlists = playlists

        return result

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details."""
        params = {
            "token": prov_artist_id,
            "type": "artist",
        }

        try:
            data = await self._make_request("webapi.get", params)
        except aiohttp.ClientError as err:
            raise MediaNotFoundError(f"Failed to get artist {prov_artist_id}") from err

        if not data:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")

        self.logger.debug("Artist API response for %s: %s", prov_artist_id, data)

        try:
            return parse_artist(data, self.instance_id, self.domain)
        except (KeyError, TypeError) as err:
            self.logger.error("Failed to parse artist %s: %s", prov_artist_id, err, exc_info=True)
            raise InvalidDataError(f"Invalid artist data for {prov_artist_id}") from err

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details."""
        params = {
            "token": prov_album_id,
            "type": "album",
        }

        try:
            data = await self._make_request("webapi.get", params)
        except aiohttp.ClientError as err:
            raise MediaNotFoundError(f"Failed to get album {prov_album_id}") from err

        if not data:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")

        try:
            return parse_album(data, self.instance_id, self.domain)
        except (KeyError, TypeError) as err:
            raise InvalidDataError(f"Invalid album data for {prov_album_id}") from err

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details."""
        params = {
            "token": prov_track_id,
            "type": "song",
        }

        try:
            data = await self._make_request("webapi.get", params)
        except aiohttp.ClientError as err:
            raise MediaNotFoundError(f"Failed to get track {prov_track_id}") from err

        if not data or "songs" not in data:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")

        songs = data.get("songs", [])
        if not songs:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")

        try:
            return parse_track(songs[0], self.instance_id, self.domain)
        except (KeyError, TypeError) as err:
            raise InvalidDataError(f"Invalid track data for {prov_track_id}") from err

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details."""
        params = {
            "token": prov_playlist_id,
            "type": "playlist",
        }

        try:
            data = await self._make_request("webapi.get", params)
        except aiohttp.ClientError as err:
            raise MediaNotFoundError(f"Failed to get playlist {prov_playlist_id}") from err

        if not data:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found")

        try:
            return parse_playlist(data, self.instance_id, self.domain)
        except (KeyError, TypeError) as err:
            raise InvalidDataError(f"Invalid playlist data for {prov_playlist_id}") from err

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks."""
        params = {
            "token": prov_album_id,
            "type": "album",
        }

        try:
            data = await self._make_request("webapi.get", params)
        except aiohttp.ClientError as err:
            self.logger.warning("Failed to get album tracks for %s: %s", prov_album_id, err)
            return []

        if not data or "songs" not in data:
            return []

        tracks = []
        for track_data in data.get("songs", []):
            try:
                tracks.append(parse_track(track_data, self.instance_id, self.domain))
            except (KeyError, TypeError) as err:
                self.logger.debug("Skipping track with invalid data: %s", err)

        return tracks

    async def get_playlist_tracks(
        self,
        prov_playlist_id: str,
        page: int = 0,
    ) -> list[Track]:
        """Get playlist tracks."""
        if page > 0:
            return []

        params = {
            "token": prov_playlist_id,
            "type": "playlist",
        }

        try:
            data = await self._make_request("webapi.get", params)
        except aiohttp.ClientError as err:
            self.logger.warning("Failed to get playlist tracks for %s: %s", prov_playlist_id, err)
            return []

        if not data or "songs" not in data:
            return []

        tracks = []
        for position, track_data in enumerate(data.get("songs", []), start=1):
            try:
                track = parse_track(track_data, self.instance_id, self.domain)
                track.position = position
                tracks.append(track)
            except (KeyError, TypeError) as err:
                self.logger.debug("Skipping track with invalid data: %s", err)

        return tracks

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get albums by artist."""
        params = {
            "token": prov_artist_id,
            "type": "artist",
            "n_album": "50",
            "sub_type": "",
            "category": "",
            "sort_order": "",
        }

        try:
            data = await self._make_request("webapi.get", params)
        except aiohttp.ClientError as err:
            self.logger.warning("Failed to get albums for artist %s: %s", prov_artist_id, err)
            return []

        if not data or "topAlbums" not in data:
            return []

        albums = []
        for album_data in data.get("topAlbums", []):
            try:
                albums.append(parse_album(album_data, self.instance_id, self.domain))
            except (KeyError, TypeError) as err:
                self.logger.debug("Skipping album with invalid data: %s", err)

        return albums

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top tracks by artist."""
        params = {
            "token": prov_artist_id,
            "type": "artist",
            "n_song": "50",
            "sub_type": "songs",
            "category": "",
            "sort_order": "",
        }

        try:
            data = await self._make_request("webapi.get", params)
        except aiohttp.ClientError as err:
            self.logger.warning("Failed to get top tracks for artist %s: %s", prov_artist_id, err)
            return []

        if not data or "topSongs" not in data:
            return []

        tracks = []
        for track_data in data.get("topSongs", []):
            try:
                tracks.append(parse_track(track_data, self.instance_id, self.domain))
            except (KeyError, TypeError) as err:
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

        # Get track details to get encrypted URL
        track_params = {
            "token": item_id,
            "type": "song",
        }

        try:
            track_data = await self._make_request("webapi.get", track_params)
        except aiohttp.ClientError as err:
            raise MediaNotFoundError(f"Failed to get track {item_id}") from err

        if not track_data or "songs" not in track_data:
            raise MediaNotFoundError(f"Track {item_id} not found")

        songs = track_data.get("songs", [])
        if not songs:
            raise MediaNotFoundError(f"Track {item_id} not found")

        song = songs[0]
        more_info = song.get("more_info", {})
        encrypted_url = more_info.get("encrypted_media_url")

        if not encrypted_url:
            raise MediaNotFoundError(f"No stream URL available for track {item_id}")

        # Always use highest quality (320kbps if available, else 128kbps)
        has_320 = more_info.get("320kbps") == "true"
        bitrate = "320" if has_320 else "128"

        # Get actual stream URL
        stream_params = {
            "url": encrypted_url,
            "bitrate": bitrate,
        }

        try:
            auth_data = await self._make_request("song.generateAuthToken", stream_params)
        except aiohttp.ClientError as err:
            raise MediaNotFoundError(f"Failed to generate stream URL for track {item_id}") from err

        stream_url = auth_data.get("auth_url")
        if not stream_url:
            raise MediaNotFoundError(f"No stream URL returned for track {item_id}")

        # Get duration
        duration = song.get("duration")
        duration_int = int(duration) if duration and str(duration).isdigit() else 0

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.AAC,
            ),
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,
            path=stream_url,
            duration=duration_int,
            can_seek=True,
            allow_seek=True,
        )

    async def _make_request(
        self, call: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Make API request to JioSaavn."""
        request_params: dict[str, str] = {
            "__call": call,
            "api_version": "4",
            "_format": "json",
            "_marker": "0",
            "ctx": "web6dot0",
        }

        if params:
            # Remove __call if it exists in params to avoid duplication
            params_copy = {k: v for k, v in params.items() if k != "__call"}
            request_params.update(params_copy)

        async with self.mass.http_session.get(
            API_URL, params=request_params, headers=DEFAULT_HEADERS
        ) as response:
            response.raise_for_status()
            # JioSaavn returns JSON but with text/html content-type, so we need content_type=None
            result: dict[str, Any] = await response.json(content_type=None)
            return result
