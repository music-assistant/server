"""Phish.in Music Provider for Music Assistant."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails

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

    def __getattribute__(self, name: str) -> Any:
        """Debug ALL method calls."""
        if name.startswith(("get_", "__init__", "__getattribute__")):
            # Skip to avoid recursion
            if name not in ("__getattribute__", "__class__"):
                self.logger.error(f"=== METHOD CALLED: {name} ===")
        return super().__getattribute__(name)

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this provider."""
        return {
            ProviderFeature.BROWSE,
            ProviderFeature.SEARCH,
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_PLAYLISTS,
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
            search_data = await api_request(
                self, endpoint, params={"audio_status": "complete_or_partial"}
            )
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
                    self,
                    ENDPOINTS["shows"],
                    params={
                        "page": page,
                        "per_page": per_page,
                        "audio_status": "complete_or_partial",
                    },
                )

                # API returns {"shows": [...]} format
                shows = shows_data.get("shows", [])
                if not shows:
                    break

                for show in shows:
                    try:
                        # Only yield shows that have audio available
                        if show.get("audio_status") in ["complete", "partial"]:
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
                    self,
                    ENDPOINTS["tracks"],
                    params={
                        "page": page,
                        "per_page": per_page,
                        "audio_status": "complete_or_partial",
                    },
                )

                # Handle tracks response format - should be direct array
                tracks = (
                    tracks_data if isinstance(tracks_data, list) else tracks_data.get("tracks", [])
                )
                if not tracks:
                    break

                for track in tracks:
                    try:
                        # Only yield tracks that have MP3 URLs
                        if track.get("mp3_url"):
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
                params={"per_page": 50, "sort": "likes_count:desc"},
            )

            tracks = []
            # Handle response format
            tracks_list = (
                tracks_data.get("tracks", tracks_data)
                if isinstance(tracks_data, dict)
                else tracks_data
            )
            for track_data in tracks_list:
                try:
                    track = track_to_ma_track(self, track_data)
                    tracks.append(track)
                except Exception as err:
                    self.logger.warning("Failed to convert track %s: %s", track_data.get("id"), err)

            return tracks[:25]  # Return top 25

        except Exception as err:
            self.logger.error("Failed to get artist top tracks: %s", err)
            return []

    # @use_cache(expiration=604800)  # 7 days
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id (show date)."""
        try:
            endpoint = ENDPOINTS["show_by_date"].format(date=prov_album_id)
            show_data = await api_request(self, endpoint)

            # Random show endpoint returns single show object
            if not show_data:
                raise MediaNotFoundError(f"Show {prov_album_id} not found")

            return show_to_album(self, show_data)

        except Exception as err:
            self.logger.error("Failed to get album %s: %s", prov_album_id, err)
            raise MediaNotFoundError(f"Album {prov_album_id} not found") from err

    # @use_cache(expiration=604800)  # 7 days
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        try:
            endpoint = ENDPOINTS["track_by_id"].format(id=prov_track_id)
            track_data = await api_request(self, endpoint)

            if not track_data:
                raise MediaNotFoundError(f"Track {prov_track_id} not found")

            return track_to_ma_track(self, track_data)

        except Exception as err:
            self.logger.error("Failed to get track %s: %s", prov_track_id, err)
            raise MediaNotFoundError(f"Track {prov_track_id} not found") from err

    # @use_cache(expiration=604800)  # 7 days
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id (show date)."""
        try:
            endpoint = ENDPOINTS["show_by_date"].format(date=prov_album_id)
            show_data = await api_request(self, endpoint)

            if not show_data:
                raise MediaNotFoundError(f"Show {prov_album_id} not found")

            tracks = []
            for track_data in show_data.get("tracks", []):
                try:
                    track = track_to_ma_track(self, track_data, show_data)
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

    # Helper methods for cached API calls
    # @use_cache(expiration=2592000)  # 30 days
    async def _get_years_data(self) -> Any:
        """Get years data with caching."""
        return await api_request(self, ENDPOINTS["years"])

    # @use_cache(expiration=7200)  # 2 hours
    async def _get_recent_shows(self) -> Any:
        """Get recent shows with caching."""
        return await api_request(
            self,
            ENDPOINTS["shows"],
            params={"per_page": 20, "sort": "date:desc", "audio_status": "complete_or_partial"},
        )

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve library playlists from the provider."""
        self.logger.info("get_library_playlists called")
        try:
            playlists_data = await api_request(
                self, ENDPOINTS["playlists"], params={"per_page": 100, "sort": "likes_count:desc"}
            )

            for playlist_data in playlists_data.get("playlists", []):
                track_count = playlist_data.get("tracks_count", 0)
                if track_count > 0:
                    playlist_id = str(playlist_data.get("id"))
                    self.logger.info(f"Creating playlist with ID: {playlist_id}")

                    playlist = Playlist(
                        item_id=playlist_id,
                        provider=self.lookup_key,
                        name=playlist_data.get("name", ""),
                        owner=playlist_data.get("username", ""),
                        is_editable=False,
                        provider_mappings={
                            ProviderMapping(
                                item_id=playlist_id,
                                provider_domain=self.domain,
                                provider_instance=self.instance_id,
                                available=True,
                            )
                        },
                    )
                    self.logger.debug(f"Complete playlist object: {playlist}")
                    yield playlist
        except Exception as err:
            self.logger.error("Failed to get library playlists: %s", err)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        self.logger.error(f"get_playlist called: {prov_playlist_id}")
        try:
            # First get all playlists to find the slug for this ID
            playlists_data = await api_request(self, ENDPOINTS["playlists"])
            playlist_slug = None
            playlist_info = None

            for playlist in playlists_data.get("playlists", []):
                if str(playlist.get("id")) == prov_playlist_id:
                    playlist_slug = playlist.get("slug")
                    playlist_info = playlist
                    break

            if not playlist_slug or not playlist_info:
                raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found")

            return Playlist(
                item_id=prov_playlist_id,
                provider=self.lookup_key,
                name=playlist_info.get("name", ""),
                owner=playlist_info.get("username", ""),
                is_editable=False,
                provider_mappings={
                    ProviderMapping(
                        item_id=prov_playlist_id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        available=True,
                    )
                },
            )

        except Exception as err:
            self.logger.error("Failed to get playlist %s: %s", prov_playlist_id, err)
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found") from err

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks for given playlist id."""
        self.logger.error(f"=== get_playlist_tracks START: {prov_playlist_id}, page: {page} ===")

        try:
            # Find playlist slug (keep existing logic for now)
            playlists_data = await api_request(self, ENDPOINTS["playlists"])
            playlist_slug = None

            for playlist in playlists_data.get("playlists", []):
                if str(playlist.get("id")) == prov_playlist_id:
                    playlist_slug = playlist.get("slug")
                    break

            if not playlist_slug:
                self.logger.error(f"Playlist slug not found for ID {prov_playlist_id}")
                raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found")

            # Get playlist tracks
            playlist_data = await api_request(
                self, ENDPOINTS["playlist_by_slug"].format(slug=playlist_slug)
            )

            self.logger.info(f"Playlist data entries: {len(playlist_data.get('entries', []))}")

            tracks = []
            for entry in playlist_data.get("entries", []):
                track_data = entry.get("track")
                if track_data:
                    self.logger.debug(
                        f"Processing track: {track_data.get('id')} - "
                        f"MP3: {bool(track_data.get('mp3_url'))}"
                    )

                    if track_data.get("mp3_url"):
                        try:
                            track = track_to_ma_track(self, track_data)
                            tracks.append(track)
                            self.logger.debug(
                                f"Successfully converted track {track_data.get('id')}"
                            )
                        except Exception as err:
                            self.logger.error(
                                f"Failed to convert track {track_data.get('id')}: {err}"
                            )

            self.logger.info(f"Returning {len(tracks)} tracks for playlist {prov_playlist_id}")
            return tracks

        except Exception as err:
            self.logger.error(f"Failed to get playlist tracks for {prov_playlist_id}: {err}")
            return []

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items."""
        self.logger.info(f"Browse called with path: {path}")

        path_parts = [] if "://" not in path else path.split("://")[1].split("/")
        subpath = path_parts[0] if len(path_parts) > 0 else ""
        subsubpath = path_parts[1] if len(path_parts) > 1 else ""

        if not subpath:
            return self._browse_root(path)

        if subpath == "playlists":
            # Return actual Playlist objects
            playlists = []
            async for playlist in self.get_library_playlists():
                playlists.append(playlist)
                if len(playlists) >= 50:
                    break
            return playlists
        elif subpath == "years":
            return await self._browse_years(path, subsubpath)
        elif subpath == "recent":
            return await self._browse_recent()
        elif subpath == "random":
            return await self._browse_random()
        elif subpath == "today":
            return await self._browse_today()
        elif subpath == "venues":
            return await self._browse_venues(path, subsubpath)
        elif subpath == "tags":
            return await self._browse_tags(path, subsubpath)
        elif subpath == "top_shows":
            return await self._browse_top_shows()
        elif subpath == "top_tracks":
            return await self._browse_top_tracks()

        return []

    def _browse_root(self, path: str) -> list[BrowseFolder]:
        """Root level browse options."""
        return [
            BrowseFolder(
                item_id="years",
                provider=self.domain,
                path=path + "years",
                name="Browse by Year",
            ),
            BrowseFolder(
                item_id="today",
                provider=self.domain,
                path=path + "today",
                name="This Day in Phish History",
            ),
            BrowseFolder(
                item_id="recent",
                provider=self.domain,
                path=path + "recent",
                name="Recent Shows",
            ),
            BrowseFolder(
                item_id="venues",
                provider=self.domain,
                path=path + "venues",
                name="Browse by Venue",
            ),
            BrowseFolder(
                item_id="tags",
                provider=self.domain,
                path=path + "tags",
                name="Browse by Tag",
            ),
            BrowseFolder(
                item_id="playlists",
                provider=self.domain,
                path=path + "playlists",
                name="User Playlists",
            ),
            BrowseFolder(
                item_id="top_shows",
                provider=self.domain,
                path=path + "top_shows",
                name="Top 46 Shows",
            ),
            BrowseFolder(
                item_id="top_tracks",
                provider=self.domain,
                path=path + "top_tracks",
                name="Top 46 Tracks",
            ),
            BrowseFolder(
                item_id="random",
                provider=self.domain,
                path=path + "random",
                name="Random Show",
            ),
        ]

    async def _browse_years(self, path: str, subsubpath: str) -> list[BrowseFolder | Album]:
        """Browse shows by year/period."""
        if not subsubpath:
            # Show list of years/periods
            try:
                years_data = await self._get_years_data()
                folders: list[BrowseFolder | Album] = []

                for year_data in years_data:
                    period = year_data.get("period")
                    show_count = year_data.get("shows_count", 0)
                    if period and show_count > 0:
                        folders.append(
                            BrowseFolder(
                                item_id=f"period_{period}",
                                provider=self.domain,
                                path=path + f"/{period}",
                                name=f"{period} ({show_count} shows)",
                            )
                        )

                return sorted(folders, key=lambda x: x.name, reverse=True)

            except Exception as err:
                self.logger.error("Failed to browse years: %s", err)
                return []
        else:
            # Show albums for specific period/year
            return cast("list[BrowseFolder | Album]", await self._get_shows_for_period(subsubpath))

    async def _browse_recent(self) -> list[Album]:
        """Get recent shows."""
        try:
            shows_data = await self._get_recent_shows()
            albums: list[Album] = []

            for show in shows_data.get("shows", []):
                try:
                    if show.get("audio_status") in ["complete", "partial"]:
                        album = show_to_album(self, show)
                        albums.append(album)
                except Exception as err:
                    self.logger.warning("Failed to convert show %s: %s", show.get("date"), err)

            return albums

        except Exception as err:
            self.logger.error("Failed to browse recent shows: %s", err)
            return []

    async def _browse_random(self) -> list[Album]:
        """Get a random show."""
        try:
            show_data = await api_request(self, ENDPOINTS["random_show"])
            if show_data and show_data.get("audio_status") in ["complete", "partial"]:
                album = show_to_album(self, show_data)
                return [album]
            return []

        except Exception as err:
            self.logger.error("Failed to get random show: %s", err)
            return []

    async def _browse_today(self) -> list[Album]:
        """Get shows that happened on this day in history."""
        try:
            today = datetime.now()

            # Use the day_of_year endpoint with any date (we just need MM-DD)
            target_date = today.strftime("%Y-%m-%d")  # Use current year as example

            shows_data = await api_request(
                self,
                ENDPOINTS["shows_day_of_year"].format(date=target_date),
                params={"audio_status": "complete_or_partial", "sort": "date:desc"},
            )

            albums: list[Album] = []
            # API returns {"shows": [...]} format
            shows = shows_data.get("shows", [])

            for show in shows:
                try:
                    if show and show.get("audio_status") in ["complete", "partial"]:
                        album = show_to_album(self, show)
                        albums.append(album)
                except Exception as err:
                    self.logger.warning("Failed to convert show %s: %s", show.get("date"), err)

            return albums

        except ProviderUnavailableError:
            # Re-raise API unavailable errors
            raise
        except MediaNotFoundError:
            # No shows on this day in history
            self.logger.info("No shows found for %s", today.strftime("%B %d"))
            return []
        except Exception as err:
            self.logger.error("Failed to get today's shows: %s", err)
            return []

    async def _browse_venues(self, path: str, subsubpath: str) -> list[BrowseFolder | Album]:
        """Browse shows by venue."""
        if not subsubpath:
            # Show list of venues - sort by total shows count
            try:
                venues_data = await api_request(
                    self, ENDPOINTS["venues"], params={"per_page": 100, "sort": "shows_count:desc"}
                )

                folders: list[BrowseFolder | Album] = []
                for venue in venues_data.get("venues", []):
                    audio_count = venue.get("shows_with_audio_count", 0)
                    if audio_count > 0:
                        folders.append(
                            BrowseFolder(
                                item_id=f"venue_{venue.get('slug')}",
                                provider=self.domain,
                                path=path + f"/{venue.get('slug')}",
                                name=f"{venue.get('name')} ({audio_count} shows)",
                            )
                        )

                return folders[:50]  # Limit to top 50 venues

            except Exception as err:
                self.logger.error("Failed to browse venues: %s", err)
                return []
        else:
            # Show albums for specific venue
            return cast("list[BrowseFolder | Album]", await self._get_shows_for_tag(subsubpath))

    async def _browse_tags(self, path: str, subsubpath: str) -> list[BrowseFolder | Album | Track]:
        """Browse shows and tracks by tag."""
        if not subsubpath:
            # Show list of tags
            try:
                tags_data = await api_request(self, ENDPOINTS["tags"])

                folders: list[BrowseFolder | Album | Track] = []
                for tag in tags_data:
                    track_count = tag.get("tracks_count", 0)
                    show_count = tag.get("shows_count", 0)
                    if track_count > 0 or show_count > 0:
                        # Show combined count in folder name
                        count_str = (
                            f"{show_count} shows, {track_count} tracks"
                            if show_count > 0
                            else f"{track_count} tracks"
                        )
                        folders.append(
                            BrowseFolder(
                                item_id=f"tag_{tag.get('slug')}",
                                provider=self.domain,
                                path=path + f"/{tag.get('slug')}",
                                name=f"{tag.get('name')} ({count_str})",
                            )
                        )

                return sorted(folders, key=lambda x: x.name)

            except Exception as err:
                self.logger.error("Failed to browse tags: %s", err)
                return []

        elif "/" not in subsubpath:
            # Show "Shows" and "Tracks" subfolders for selected tag
            tag_slug = subsubpath
            try:
                # Get tag info to show counts
                tags_data = await api_request(self, ENDPOINTS["tags"])
                tag_info: dict[str, Any] = next(
                    (tag for tag in tags_data if tag.get("slug") == tag_slug), {}
                )
                tag_name = tag_info.get("name", tag_slug)
                show_count = tag_info.get("shows_count", 0)
                track_count = tag_info.get("tracks_count", 0)

                subfolders: list[BrowseFolder | Album | Track] = []

                if show_count > 0:
                    subfolders.append(
                        BrowseFolder(
                            item_id=f"tag_shows_{tag_slug}",
                            provider=self.domain,
                            path=path + f"/{tag_slug}/shows",
                            name=f"Shows with {tag_name} ({show_count})",
                        )
                    )

                if track_count > 0:
                    subfolders.append(
                        BrowseFolder(
                            item_id=f"tag_tracks_{tag_slug}",
                            provider=self.domain,
                            path=path + f"/{tag_slug}/tracks",
                            name=f"All {tag_name} Tracks ({track_count})",
                        )
                    )

                return subfolders

            except Exception as err:
                self.logger.error("Failed to get tag subfolders: %s", err)
                return []
        else:
            # Handle tag_slug/shows or tag_slug/tracks
            tag_slug, content_type = subsubpath.split("/", 1)
            if content_type == "shows":
                return cast(
                    "list[BrowseFolder | Album | Track]", await self._get_shows_for_tag(tag_slug)
                )
            elif content_type == "tracks":
                return cast(
                    "list[BrowseFolder | Album | Track]", await self._get_tracks_for_tag(tag_slug)
                )
            else:
                return []

    async def _get_tracks_for_tag(self, tag_slug: str) -> list[Track]:
        """Get tracks for a specific tag."""
        try:
            tracks_data = await api_request(
                self,
                ENDPOINTS["tracks"],
                params={
                    "tag_slug": tag_slug,
                    "per_page": 100,
                    "audio_status": "complete_or_partial",
                    "sort": "likes_count:desc",
                },
            )

            tracks: list[Track] = []
            tracks_list = (
                tracks_data.get("tracks", tracks_data)
                if isinstance(tracks_data, dict)
                else tracks_data
            )

            for track_data in tracks_list:
                try:
                    if track_data.get("mp3_url"):  # Only tracks with audio
                        track = track_to_ma_track(self, track_data)
                        tracks.append(track)
                except Exception as err:
                    self.logger.warning("Failed to convert track %s: %s", track_data.get("id"), err)

            return tracks

        except Exception as err:
            self.logger.error("Failed to get tracks for tag %s: %s", tag_slug, err)
            return []

    async def _browse_top_shows(self) -> list[Album]:
        """Get top 46 most liked shows."""
        try:
            shows_data = await api_request(
                self,
                ENDPOINTS["shows"],
                params={
                    "per_page": 46,
                    "sort": "likes_count:desc",
                    "audio_status": "complete_or_partial",
                },
            )

            albums: list[Album] = []
            for show in shows_data.get("shows", []):
                try:
                    if show.get("audio_status") in ["complete", "partial"]:
                        album = show_to_album(self, show)
                        albums.append(album)
                except Exception as err:
                    self.logger.warning("Failed to convert show %s: %s", show.get("date"), err)

            return albums

        except Exception as err:
            self.logger.error("Failed to get top shows: %s", err)
            return []

    async def _browse_top_tracks(self) -> list[Track]:
        """Get top 46 most liked tracks."""
        try:
            tracks_data = await api_request(
                self,
                ENDPOINTS["tracks"],
                params={
                    "per_page": 46,
                    "sort": "likes_count:desc",
                    "audio_status": "complete_or_partial",
                },
            )

            tracks: list[Track] = []
            tracks_list = (
                tracks_data.get("tracks", tracks_data)
                if isinstance(tracks_data, dict)
                else tracks_data
            )

            for track_data in tracks_list:
                try:
                    if track_data.get("mp3_url"):  # Only tracks with audio
                        track = track_to_ma_track(self, track_data)
                        tracks.append(track)
                except Exception as err:
                    self.logger.warning("Failed to convert track %s: %s", track_data.get("id"), err)

            return tracks

        except Exception as err:
            self.logger.error("Failed to get top tracks: %s", err)
            return []

    async def _get_shows_for_period(self, period: str) -> list[Album]:
        """Get shows for a specific year or period."""
        try:
            if "-" in period and len(period.split("-")) == 2:
                params = {
                    "year_range": period,
                    "per_page": 100,
                    "audio_status": "complete_or_partial",
                }
            else:
                params = {
                    "year": period,
                    "per_page": 100,
                    "audio_status": "complete_or_partial",
                }

            shows_data = await api_request(self, ENDPOINTS["shows"], params=params)

            albums = []
            for show in shows_data.get("shows", []):
                try:
                    if show.get("audio_status") in ["complete", "partial"]:
                        album = show_to_album(self, show)
                        albums.append(album)
                except Exception as err:
                    self.logger.warning("Failed to convert show %s: %s", show.get("date"), err)

            return sorted(albums, key=lambda x: x.name)

        except Exception as err:
            self.logger.error("Failed to browse period %s: %s", period, err)
            return []

    async def _get_shows_for_venue(self, venue_slug: str) -> list[Album]:
        """Get shows for a specific venue."""
        try:
            shows_data = await api_request(
                self,
                ENDPOINTS["shows"],
                params={
                    "venue_slug": venue_slug,
                    "per_page": 100,
                    "audio_status": "complete_or_partial",
                    "sort": "date:desc",
                },
            )

            albums = []
            for show in shows_data.get("shows", []):
                try:
                    if show.get("audio_status") in ["complete", "partial"]:
                        album = show_to_album(self, show)
                        albums.append(album)
                except Exception as err:
                    self.logger.warning("Failed to convert show %s: %s", show.get("date"), err)

            return albums

        except Exception as err:
            self.logger.error("Failed to get shows for venue %s: %s", venue_slug, err)
            return []

    async def _get_shows_for_tag(self, tag_slug: str) -> list[Album]:
        """Get shows for a specific tag."""
        try:
            shows_data = await api_request(
                self,
                ENDPOINTS["shows"],
                params={
                    "tag_slug": tag_slug,
                    "per_page": 100,
                    "audio_status": "complete_or_partial",
                    "sort": "date:desc",
                },
            )

            albums = []
            for show in shows_data.get("shows", []):
                try:
                    if show.get("audio_status") in ["complete", "partial"]:
                        album = show_to_album(self, show)
                        albums.append(album)
                except Exception as err:
                    self.logger.warning("Failed to convert show %s: %s", show.get("date"), err)

            return albums

        except Exception as err:
            self.logger.error("Failed to get shows for tag %s: %s", tag_slug, err)
            return []
