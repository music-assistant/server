"""Phish.in Music Provider for Music Assistant."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    ENDPOINTS,
    MAX_SEARCH_RESULTS,
    PHISH_ARTIST_ID,
)
from .helpers import (
    api_request,
    get_phish_artist,
    parse_search_results,
    show_to_album,
    track_to_ma_track,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.media_items import MediaItemType


class PhishInProvider(MusicProvider):
    """Phish.in music provider."""

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this provider."""
        return {
            ProviderFeature.BROWSE,
            ProviderFeature.SEARCH,
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.ARTIST_ALBUMS,
            ProviderFeature.ARTIST_TOPTRACKS,
        }

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = MAX_SEARCH_RESULTS,
    ) -> SearchResults:
        """Perform search on Phish.in."""
        try:
            endpoint = ENDPOINTS["search"].format(term=search_query)
            search_data = await api_request(self, endpoint)

            artists, albums, tracks = parse_search_results(self, search_data, media_types)

            return SearchResults(
                artists=artists[:limit] if MediaType.ARTIST in media_types else [],
                albums=albums[:limit] if MediaType.ALBUM in media_types else [],
                tracks=tracks[:limit] if MediaType.TRACK in media_types else [],
            )

        except Exception as err:
            self.logger.error("Search failed for query '%s': %s", search_query, err)
            return SearchResults()

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from the provider."""
        # Phish.in only has Phish as the main artist
        yield await get_phish_artist(self)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums (shows) from the provider."""
        try:
            page = 1
            per_page = 100

            while True:
                # Get shows page by page to avoid memory issues
                shows_data = await api_request(
                    self, ENDPOINTS["shows"], params={"page": page, "per_page": per_page}
                )

                shows = shows_data.get("data", [])
                if not shows:
                    break

                for show in shows:
                    try:
                        # Only yield shows that have audio available
                        if not show.get("incomplete", True):  # incomplete=False means has audio
                            yield show_to_album(self, show)
                    except Exception as err:
                        self.logger.warning(
                            "Failed to convert show %s to album: %s", show.get("date"), err
                        )

                # Check if we've reached the end
                if len(shows) < per_page:
                    break

                page += 1

        except Exception as err:
            self.logger.error("Failed to get library albums: %s", err)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        try:
            page = 1
            per_page = 100

            while True:
                # Get tracks page by page to avoid memory issues
                tracks_data = await api_request(
                    self, ENDPOINTS["tracks"], params={"page": page, "per_page": per_page}
                )

                tracks = tracks_data.get("data", [])
                if not tracks:
                    break

                for track in tracks:
                    try:
                        # Only yield tracks that have MP3 URLs
                        if track.get("mp3"):
                            yield track_to_ma_track(self, track)
                    except Exception as err:
                        self.logger.warning("Failed to convert track %s: %s", track.get("id"), err)

                # Check if we've reached the end
                if len(tracks) < per_page:
                    break

                page += 1

        except Exception as err:
            self.logger.error("Failed to get library tracks: %s", err)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        if prov_artist_id == PHISH_ARTIST_ID:
            return await get_phish_artist(self)
        raise MediaNotFoundError(f"Artist {prov_artist_id} not found")

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        if prov_artist_id != PHISH_ARTIST_ID:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")

        albums = []
        async for album in self.get_library_albums():
            albums.append(album)
            # Limit to prevent memory issues
            if len(albums) >= 1000:
                break

        return albums

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of most popular tracks for the given artist."""
        if prov_artist_id != PHISH_ARTIST_ID:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")

        try:
            # Get recent popular tracks (limited set)
            tracks_data = await api_request(
                self,
                ENDPOINTS["tracks"],
                params={"per_page": 50, "sort_attr": "likes_count", "sort_dir": "desc"},
            )

            tracks = []
            for track_data in tracks_data.get("data", []):
                try:
                    track = track_to_ma_track(self, track_data)
                    tracks.append(track)
                except Exception as err:
                    self.logger.warning("Failed to convert track %s: %s", track_data.get("id"), err)

            return tracks[:25]  # Return top 25

        except Exception as err:
            self.logger.error("Failed to get artist top tracks: %s", err)
            return []

    @use_cache(expiration=604800)  # 7 days
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id (show date)."""
        try:
            endpoint = ENDPOINTS["show_by_date"].format(date=prov_album_id)
            show_data = await api_request(self, endpoint)

            show = show_data.get("data")
            if not show:
                raise MediaNotFoundError(f"Show {prov_album_id} not found")

            return show_to_album(self, show)

        except Exception as err:
            self.logger.error("Failed to get album %s: %s", prov_album_id, err)
            raise MediaNotFoundError(f"Album {prov_album_id} not found") from err

    @use_cache(expiration=604800)  # 7 days
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        try:
            endpoint = ENDPOINTS["track_by_id"].format(id=prov_track_id)
            track_data = await api_request(self, endpoint)

            track = track_data.get("data")
            if not track:
                raise MediaNotFoundError(f"Track {prov_track_id} not found")

            return track_to_ma_track(self, track)

        except Exception as err:
            self.logger.error("Failed to get track %s: %s", prov_track_id, err)
            raise MediaNotFoundError(f"Track {prov_track_id} not found") from err

    @use_cache(expiration=604800)  # 7 days
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id (show date)."""
        try:
            endpoint = ENDPOINTS["show_by_date"].format(date=prov_album_id)
            show_data = await api_request(self, endpoint)

            show = show_data.get("data")
            if not show:
                raise MediaNotFoundError(f"Show {prov_album_id} not found")

            tracks = []
            for track_data in show.get("tracks", []):
                try:
                    track = track_to_ma_track(self, track_data, show)
                    tracks.append(track)
                except Exception as err:
                    self.logger.warning("Failed to convert track %s: %s", track_data.get("id"), err)

            return tracks

        except Exception as err:
            self.logger.error("Failed to get album tracks for %s: %s", prov_album_id, err)
            return []

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track."""
        if media_type != MediaType.TRACK:
            raise MediaNotFoundError(f"Streaming not supported for {media_type}")

        try:
            # Get track details to get MP3 URL
            track = await self.get_track(item_id)

            # Get the MP3 URL from provider mappings
            mp3_url = None
            for mapping in track.provider_mappings:
                if mapping.provider_instance == self.instance_id and mapping.url:
                    mp3_url = mapping.url
                    break

            if not mp3_url:
                raise MediaNotFoundError(f"No audio URL found for track {item_id}")

            return StreamDetails(
                provider=self.instance_id,
                item_id=item_id,
                audio_format=AudioFormat(
                    content_type=ContentType.MP3,
                    sample_rate=44100,  # Assume standard MP3
                    bit_depth=16,
                    channels=2,
                ),
                media_type=MediaType.TRACK,
                stream_type=StreamType.HTTP,
                path=mp3_url,
                allow_seek=True,
                can_seek=True,
            )

        except Exception as err:
            self.logger.error("Failed to get stream details for %s: %s", item_id, err)
            raise MediaNotFoundError(f"Stream not available for track {item_id}") from err

    @use_cache(expiration=86400)  # 24 hours
    async def _get_years_data(self) -> Any:  # Change from dict[str, Any] to Any
        """Get years data with caching."""
        return await api_request(self, ENDPOINTS["years"])

    @use_cache(expiration=3600)  # 1 hour
    async def _get_shows_for_year(self, year: str) -> Any:  # Change from dict[str, Any] to Any
        """Get shows for a specific year with caching."""
        return await api_request(self, ENDPOINTS["shows"], params={"year": year})

    @use_cache(expiration=7200)  # 2 hours
    async def _get_recent_shows(self) -> Any:  # Change from dict[str, Any] to Any
        """Get recent shows with caching."""
        return await api_request(
            self,
            ENDPOINTS["shows"],
            params={"per_page": 20, "sort_attr": "date", "sort_dir": "desc"},
        )

    @use_cache(expiration=1800)  # 30 minutes
    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items."""
        if not path or path == "root":
            # Root level - show main browse options
            return [
                BrowseFolder(
                    item_id="years",
                    provider=self.instance_id,
                    path="years",
                    name="Browse by Year",
                ),
                BrowseFolder(
                    item_id="recent",
                    provider=self.instance_id,
                    path="recent",
                    name="Recent Shows",
                ),
                BrowseFolder(
                    item_id="random",
                    provider=self.instance_id,
                    path="random",
                    name="Random Show",
                ),
            ]

        if path == "years":
            # Get available years
            try:
                years_data = await self._get_years_data()

                folders = []
                for year_data in years_data.get("data", []):
                    year = year_data.get("date")
                    show_count = year_data.get("show_count", 0)
                    if year and show_count > 0:
                        folders.append(
                            BrowseFolder(
                                item_id=f"year_{year}",
                                provider=self.instance_id,
                                path=f"year/{year}",
                                name=f"{year} ({show_count} shows)",
                            )
                        )

                return sorted(folders, key=lambda x: x.name, reverse=True)

            except Exception as err:
                self.logger.error("Failed to browse years: %s", err)
                return []

        if path.startswith("year/"):
            # Get shows for specific year
            year = path.split("/")[1]
            try:
                shows_data = await self._get_shows_for_year(year)

                albums = []
                for show in shows_data.get("data", []):
                    try:
                        album = show_to_album(self, show)
                        albums.append(album)
                    except Exception as err:
                        self.logger.warning("Failed to convert show %s: %s", show.get("date"), err)

                return sorted(albums, key=lambda x: x.name)

            except Exception as err:
                self.logger.error("Failed to browse year %s: %s", year, err)
                return []

        if path == "recent":
            # Get recent shows
            try:
                shows_data = await self._get_recent_shows()

                albums = []
                for show in shows_data.get("data", []):
                    try:
                        album = show_to_album(self, show)
                        albums.append(album)
                    except Exception as err:
                        self.logger.warning("Failed to convert show %s: %s", show.get("date"), err)

                return albums

            except Exception as err:
                self.logger.error("Failed to browse recent shows: %s", err)
                return []

        if path == "random":
            # Get a random show
            try:
                show_data = await api_request(self, ENDPOINTS["random_show"])
                show = show_data.get("data")
                if show:
                    album = show_to_album(self, show)
                    return [album]
                return []

            except Exception as err:
                self.logger.error("Failed to get random show: %s", err)
                return []

        return []
