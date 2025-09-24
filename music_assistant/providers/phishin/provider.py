"""Phish.in Music Provider for Music Assistant."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    ENDPOINTS,
    FALLBACK_ALBUM_IMAGE,
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

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.media_items import MediaItemType
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant


class PhishInProvider(MusicProvider):
    """Phish.in music provider."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config)
        self._supported_features = supported_features

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this provider."""
        return self._supported_features

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
        if len(search_query.strip()) < 3:
            return SearchResults()

        try:
            endpoint = ENDPOINTS["search"].format(term=search_query)
            search_data = await api_request(
                self, endpoint, params={"audio_status": "complete_or_partial"}
            )

            # If venues are found and we want albums, fetch shows for those venues
            if MediaType.ALBUM in media_types and search_data.get("venues"):
                venue_shows: list[dict[str, Any]] = []
                for venue in search_data.get("venues", []):
                    venue_slug = venue["slug"]
                    try:
                        page = 1
                        while len(venue_shows) < limit:
                            shows_data = await api_request(
                                self, "/shows", params={"venue_slug": venue_slug, "page": page}
                            )

                            shows_on_page = shows_data.get("shows", [])
                            if not shows_on_page:
                                break

                            remaining_slots = limit - len(venue_shows)
                            venue_shows.extend(shows_on_page[:remaining_slots])

                            current_page = shows_data.get("current_page", 1)
                            total_pages = shows_data.get("total_pages", 1)

                            if current_page >= total_pages or len(venue_shows) >= limit:
                                break

                            page += 1

                    except ProviderUnavailableError:
                        self.logger.warning(
                            "Failed to fetch shows for venue %s: API unavailable", venue_slug
                        )

                if venue_shows:
                    search_data["venue_shows"] = venue_shows

            artists, albums, tracks, playlists = parse_search_results(
                self, search_data, media_types, search_query
            )

            return SearchResults(
                artists=artists[:limit] if MediaType.ARTIST in media_types else [],
                albums=albums[:limit] if MediaType.ALBUM in media_types else [],
                tracks=tracks[:limit] if MediaType.TRACK in media_types else [],
                playlists=playlists[:limit] if MediaType.PLAYLIST in media_types else [],
            )
        except (MediaNotFoundError, ProviderUnavailableError):
            raise
        except Exception as err:
            self.logger.error("Search failed for query '%s': %s", search_query, err)
            raise ProviderUnavailableError(f"Search error: {err}") from err

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from the provider."""
        yield await get_phish_artist(self)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums (shows) from the provider."""
        page = 1
        per_page = 50
        max_pages = 20  # 1000 albums max for UI performance

        try:
            while page <= max_pages:
                shows_data = await api_request(
                    self,
                    ENDPOINTS["shows"],
                    params={
                        "page": page,
                        "per_page": per_page,
                        "audio_status": "complete_or_partial",
                    },
                )

                shows = shows_data.get("shows", [])
                if not shows:
                    break

                for show in shows:
                    if show.get("audio_status") in ["complete", "partial"]:
                        yield show_to_album(self, show)

                if len(shows) < per_page:
                    break

                page += 1

        except (MediaNotFoundError, ProviderUnavailableError):
            raise
        except Exception as err:
            self.logger.error("Failed to get library albums: %s", err)
            raise ProviderUnavailableError(f"Library albums error: {err}") from err

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        page = 1
        per_page = 50
        max_pages = 20  # 1000 tracks max for UI performance

        try:
            while page <= max_pages:
                tracks_data = await api_request(
                    self,
                    ENDPOINTS["tracks"],
                    params={
                        "page": page,
                        "per_page": per_page,
                        "audio_status": "complete_or_partial",
                    },
                )

                tracks = tracks_data.get("tracks", [])

                if not tracks:
                    break

                for track in tracks:
                    if track.get("mp3_url"):
                        yield track_to_ma_track(self, track)

                if len(tracks) < per_page:
                    break

                page += 1

        except (MediaNotFoundError, ProviderUnavailableError):
            raise
        except Exception as err:
            self.logger.error("Failed to get library tracks: %s", err)
            raise ProviderUnavailableError(f"Library tracks error: {err}") from err

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
            if len(albums) >= 1000:  # Reasonable limit
                break

        return albums

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of most popular tracks for the given artist."""
        if prov_artist_id != PHISH_ARTIST_ID:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")

        try:
            all_tracks: list[Track] = []
            page = 1
            max_pages = 20  # 1000 tracks max for UI performance

            while len(all_tracks) < (max_pages * 50) and page <= max_pages:
                tracks_data = await api_request(
                    self,
                    ENDPOINTS["tracks"],
                    params={
                        "page": page,
                        "per_page": 50,
                        "sort": "likes_count:desc",
                        "audio_status": "complete_or_partial",
                    },
                )

                tracks_on_page = tracks_data.get("tracks", [])
                if not tracks_on_page:
                    break

                for track_data in tracks_on_page:
                    show_data = {
                        "date": track_data.get("show_date"),
                        "album_cover_url": track_data.get("show_album_cover_url"),
                        "venue": {"name": track_data.get("venue_name")},
                    }
                    track = track_to_ma_track(self, track_data, show_data)
                    all_tracks.append(track)

                if len(tracks_on_page) < 50:
                    break

                page += 1

            return all_tracks

        except (MediaNotFoundError, ProviderUnavailableError):
            raise
        except Exception as err:
            self.logger.error("Failed to get artist top tracks: %s", err)
            raise ProviderUnavailableError(f"Top tracks error: {err}") from err

    @use_cache(expiration=2592000)  # 30 days - historical show data rarely changes
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id (show date)."""
        try:
            endpoint = ENDPOINTS["show_by_date"].format(date=prov_album_id)
            show_data = await api_request(self, endpoint)

            if not show_data:
                raise MediaNotFoundError(f"Show {prov_album_id} not found")

            return show_to_album(self, show_data)

        except MediaNotFoundError:
            raise
        except Exception as err:
            self.logger.error("Failed to get album %s: %s", prov_album_id, err)
            raise ProviderUnavailableError(f"Album error: {err}") from err

    @use_cache(expiration=2592000)  # 30 days - historical track data rarely changes
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        try:
            endpoint = ENDPOINTS["track_by_id"].format(id=prov_track_id)
            track_data = await api_request(self, endpoint)

            if not track_data:
                raise MediaNotFoundError(f"Track {prov_track_id} not found")

            return track_to_ma_track(self, track_data)

        except MediaNotFoundError:
            raise
        except Exception as err:
            self.logger.error("Failed to get track %s: %s", prov_track_id, err)
            raise ProviderUnavailableError(f"Track error: {err}") from err

    @use_cache(expiration=2592000)  # 30 days - historical album tracks rarely change
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id (show date)."""
        try:
            endpoint = ENDPOINTS["show_by_date"].format(date=prov_album_id)
            show_data = await api_request(self, endpoint)

            if not show_data:
                raise MediaNotFoundError(f"Show {prov_album_id} not found")

            tracks = []
            for track_data in show_data.get("tracks", []):
                track = track_to_ma_track(self, track_data, show_data)
                tracks.append(track)

            return tracks

        except MediaNotFoundError:
            raise
        except Exception as err:
            self.logger.error("Failed to get album tracks for %s: %s", prov_album_id, err)
            raise ProviderUnavailableError(f"Album tracks error: {err}") from err

    @use_cache(expiration=604800)  # 7 days - shows containing a song rarely change
    async def get_track_albums(
        self,
        prov_track_id: str,
        library_only: bool = False,
    ) -> list[Album]:
        """Get albums (shows) that contain a specific track."""
        try:
            track_data = await api_request(self, f"/tracks/{prov_track_id}")

            if not track_data or not track_data.get("songs"):
                return []

            song_slug = track_data["songs"][0]["slug"]

            tracks_data = await api_request(
                self,
                "/tracks",
                params={
                    "song_slug": song_slug,
                    "audio_status": "complete_or_partial",
                    "per_page": 100,
                },
            )

            unique_shows = {}
            for track in tracks_data.get("tracks", []):
                show_date = track.get("show_date")
                if show_date and show_date not in unique_shows:
                    show_data = {
                        "id": track["show"]["id"],
                        "date": show_date,
                        "venue_name": track.get("venue_name"),
                        "audio_status": track["show"]["audio_status"],
                        "cover_art_urls": track["show"]["cover_art_urls"],
                        "album_cover_url": track["show"]["album_cover_url"],
                        "tour_name": track["show"].get("tour_name"),
                        "venue": track["show"]["venue"],
                    }
                    unique_shows[show_date] = show_data

            albums = []
            for show_data in unique_shows.values():
                album = show_to_album(self, show_data)
                albums.append(album)

            albums.sort(key=lambda x: x.name, reverse=True)
            return albums

        except (MediaNotFoundError, ProviderUnavailableError):
            raise
        except Exception as err:
            self.logger.error("Failed to get track albums for %s: %s", prov_track_id, err)
            raise ProviderUnavailableError(f"Track albums error: {err}") from err

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track."""
        if media_type != MediaType.TRACK:
            raise MediaNotFoundError(f"Streaming not supported for {media_type}")

        try:
            track = await self.get_track(item_id)

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
                    sample_rate=44100,
                    bit_depth=16,
                    channels=2,
                ),
                media_type=MediaType.TRACK,
                stream_type=StreamType.HTTP,
                path=mp3_url,
                allow_seek=True,
                can_seek=True,
            )

        except (MediaNotFoundError, ProviderUnavailableError):
            raise
        except Exception as err:
            self.logger.error("Failed to get stream details for %s: %s", item_id, err)
            raise ProviderUnavailableError(f"Stream error: {err}") from err

    @use_cache(expiration=86400)  # 24 hours - current year data needs to update
    async def _get_years_data(self) -> Any:
        """Get years data with caching."""
        return await api_request(self, ENDPOINTS["years"])

    @use_cache(expiration=86400)  # 24 hours - recent shows update daily
    async def _get_recent_shows(self) -> Any:
        """Get recent shows with caching."""
        return await api_request(
            self,
            ENDPOINTS["shows"],
            params={"per_page": 20, "sort": "date:desc", "audio_status": "complete_or_partial"},
        )

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve library playlists from the provider."""
        try:
            playlists_data = await api_request(
                self, ENDPOINTS["playlists"], params={"per_page": 100, "sort": "likes_count:desc"}
            )

            for playlist_data in playlists_data.get("playlists", []):
                track_count = playlist_data.get("tracks_count", 0)
                if track_count > 0:
                    playlist_id = str(playlist_data.get("id"))

                    metadata = MediaItemMetadata(
                        images=UniqueList(
                            [
                                MediaItemImage(
                                    type=ImageType.THUMB,
                                    path=FALLBACK_ALBUM_IMAGE,
                                    provider=self.instance_id,
                                    remotely_accessible=True,
                                )
                            ]
                        )
                    )
                    yield Playlist(
                        item_id=playlist_id,
                        provider=self.instance_id,
                        name=playlist_data.get("name", ""),
                        owner=playlist_data.get("username", ""),
                        is_editable=False,
                        cache_checksum=str(playlist_data.get("id")),
                        metadata=metadata,
                        provider_mappings={
                            ProviderMapping(
                                item_id=playlist_id,
                                provider_domain=self.domain,
                                provider_instance=self.instance_id,
                                available=True,
                            )
                        },
                    )
        except (MediaNotFoundError, ProviderUnavailableError):
            raise
        except Exception as err:
            self.logger.error("Failed to get library playlists: %s", err)
            raise ProviderUnavailableError(f"Library playlists error: {err}") from err

    @use_cache(expiration=86400)  # 24 hours - playlist metadata changes infrequently
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        try:
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
                provider=self.instance_id,
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

        except MediaNotFoundError:
            raise
        except Exception as err:
            self.logger.error("Failed to get playlist %s: %s", prov_playlist_id, err)
            raise ProviderUnavailableError(f"Playlist error: {err}") from err

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks for given playlist id."""
        try:
            playlists_data = await api_request(self, ENDPOINTS["playlists"])
            playlist_slug = None

            for playlist in playlists_data.get("playlists", []):
                if str(playlist.get("id")) == prov_playlist_id:
                    playlist_slug = playlist.get("slug")
                    break

            if not playlist_slug:
                return []

            playlist_data = await api_request(
                self, ENDPOINTS["playlist_by_slug"].format(slug=playlist_slug)
            )

            # Simple pagination - fetch all tracks and slice
            page_size = 50
            start_idx = page * page_size
            end_idx = start_idx + page_size

            all_tracks = []
            for entry in playlist_data.get("entries", []):
                track_data = entry.get("track")
                if track_data and track_data.get("mp3_url"):
                    track = track_to_ma_track(self, track_data)
                    all_tracks.append(track)

            return all_tracks[start_idx:end_idx]

        except (MediaNotFoundError, ProviderUnavailableError):
            raise
        except Exception as err:
            self.logger.error("Failed to get playlist tracks for %s: %s", prov_playlist_id, err)
            raise ProviderUnavailableError(f"Playlist tracks error: {err}") from err

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items."""
        path_parts = [] if "://" not in path else path.split("://")[1].split("/")
        subpath = path_parts[0] if path_parts else ""
        subsubpath = "/".join(path_parts[1:]) if len(path_parts) > 1 else ""

        if not subpath:
            return self._browse_root(path)

        if subpath == "playlists":
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
                                path=f"phishin://years/{period}",
                                name=f"{period} ({show_count} shows)",
                            )
                        )

                return sorted(folders, key=lambda x: x.name, reverse=True)

            except (MediaNotFoundError, ProviderUnavailableError):
                raise
            except Exception as err:
                self.logger.error("Failed to browse years: %s", err)
                raise ProviderUnavailableError(f"Browse years error: {err}") from err
        else:
            return await self._get_shows_for_period(subsubpath)

    async def _browse_recent(self) -> list[Album]:
        """Get recent shows."""
        try:
            shows_data = await self._get_recent_shows()
            albums: list[Album] = []

            for show in shows_data.get("shows", []):
                if show.get("audio_status") in ["complete", "partial"]:
                    album = show_to_album(self, show)
                    albums.append(album)

            return albums

        except (MediaNotFoundError, ProviderUnavailableError):
            raise
        except Exception as err:
            self.logger.error("Failed to browse recent shows: %s", err)
            raise ProviderUnavailableError(f"Browse recent error: {err}") from err

    async def _browse_random(self) -> list[Album]:
        """Get a random show."""
        try:
            show_data = await api_request(self, ENDPOINTS["random_show"])
            if show_data and show_data.get("audio_status") in ["complete", "partial"]:
                album = show_to_album(self, show_data)
                return [album]
            return []

        except (MediaNotFoundError, ProviderUnavailableError):
            raise
        except Exception as err:
            self.logger.error("Failed to get random show: %s", err)
            raise ProviderUnavailableError(f"Random show error: {err}") from err

    async def _browse_today(self) -> list[Album]:
        """Get shows that happened on this day in history."""
        try:
            today = datetime.now()
            target_date = today.strftime("%Y-%m-%d")

            shows_data = await api_request(
                self,
                ENDPOINTS["shows_day_of_year"].format(date=target_date),
                params={"audio_status": "complete_or_partial", "sort": "date:desc"},
            )

            albums: list[Album] = []
            shows = shows_data.get("shows", [])

            for show in shows:
                if show and show.get("audio_status") in ["complete", "partial"]:
                    album = show_to_album(self, show)
                    albums.append(album)

            return albums

        except MediaNotFoundError:
            self.logger.info("No shows found for %s", today.strftime("%B %d"))
            return []
        except ProviderUnavailableError:
            raise
        except Exception as err:
            self.logger.error("Failed to get today's shows: %s", err)
            raise ProviderUnavailableError(f"Today's shows error: {err}") from err

    async def _browse_venues(self, path: str, subsubpath: str) -> list[BrowseFolder | Album]:
        """Browse shows by venue."""
        if not subsubpath:
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
                                path=f"phishin://venues/{venue.get('slug')}",
                                name=f"{venue.get('name')} ({audio_count} shows)",
                            )
                        )

                return folders[:50]

            except (MediaNotFoundError, ProviderUnavailableError):
                raise
            except Exception as err:
                self.logger.error("Failed to browse venues: %s", err)
                raise ProviderUnavailableError(f"Browse venues error: {err}") from err
        else:
            return await self._get_shows_for_venue(subsubpath)

    async def _browse_tags(self, path: str, subsubpath: str) -> list[BrowseFolder | Album | Track]:
        """Browse shows and tracks by tag."""
        if not subsubpath:
            try:
                tags_data = await api_request(self, ENDPOINTS["tags"])

                folders: list[BrowseFolder | Album | Track] = []
                for tag in tags_data:
                    track_count = tag.get("tracks_count", 0)
                    show_count = tag.get("shows_count", 0)
                    if track_count > 0 or show_count > 0:
                        count_str = (
                            f"{show_count} shows, {track_count} tracks"
                            if show_count > 0
                            else f"{track_count} tracks"
                        )
                        folders.append(
                            BrowseFolder(
                                item_id=f"tag_{tag.get('slug')}",
                                provider=self.domain,
                                path=f"phishin://tags/{tag.get('slug')}",
                                name=f"{tag.get('name')} ({count_str})",
                            )
                        )

                return sorted(folders, key=lambda x: x.name)

            except (MediaNotFoundError, ProviderUnavailableError):
                raise
            except Exception as err:
                self.logger.error("Failed to browse tags: %s", err)
                raise ProviderUnavailableError(f"Browse tags error: {err}") from err

        elif "/" not in subsubpath:
            tag_slug = subsubpath
            try:
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
                            path=f"phishin://tags/{tag_slug}/shows",
                            name=f"Shows with {tag_name} ({show_count})",
                        )
                    )

                if track_count > 0:
                    subfolders.append(
                        BrowseFolder(
                            item_id=f"tag_tracks_{tag_slug}",
                            provider=self.domain,
                            path=f"phishin://tags/{tag_slug}/tracks",
                            name=f"All {tag_name} Tracks ({track_count})",
                        )
                    )

                return subfolders

            except (MediaNotFoundError, ProviderUnavailableError):
                raise
            except Exception as err:
                self.logger.error("Failed to get tag subfolders: %s", err)
                raise ProviderUnavailableError(f"Tag subfolders error: {err}") from err
        else:
            tag_slug, content_type = subsubpath.split("/", 1)
            if content_type == "shows":
                return await self._get_shows_for_tag(tag_slug)
            elif content_type == "tracks":
                return await self._get_tracks_for_tag(tag_slug)
            else:
                return []

    @use_cache(expiration=86400)  # 24 hours - historical tag track data is stable
    async def _get_tracks_for_tag(self, tag_slug: str) -> list[BrowseFolder | Album | Track]:
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

            tracks: list[BrowseFolder | Album | Track] = []
            for track_data in tracks_data.get("tracks", []):
                if track_data.get("mp3_url"):
                    track = track_to_ma_track(self, track_data)
                    tracks.append(track)

            return tracks

        except (MediaNotFoundError, ProviderUnavailableError):
            raise
        except Exception as err:
            self.logger.error("Failed to get tracks for tag %s: %s", tag_slug, err)
            raise ProviderUnavailableError(f"Tag tracks error: {err}") from err

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
                if show.get("audio_status") in ["complete", "partial"]:
                    album = show_to_album(self, show)
                    albums.append(album)

            return albums

        except (MediaNotFoundError, ProviderUnavailableError):
            raise
        except Exception as err:
            self.logger.error("Failed to get top shows: %s", err)
            raise ProviderUnavailableError(f"Top shows error: {err}") from err

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
            for track_data in tracks_data.get("tracks", []):
                if track_data.get("mp3_url"):
                    track = track_to_ma_track(self, track_data)
                    tracks.append(track)

            return tracks

        except (MediaNotFoundError, ProviderUnavailableError):
            raise
        except Exception as err:
            self.logger.error("Failed to get top tracks: %s", err)
            raise ProviderUnavailableError(f"Top tracks error: {err}") from err

    async def _get_shows_for_period(self, period: str) -> list[BrowseFolder | Album]:
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

            albums: list[BrowseFolder | Album] = []
            for show in shows_data.get("shows", []):
                if show.get("audio_status") in ["complete", "partial"]:
                    album = show_to_album(self, show)
                    albums.append(album)

            return sorted(albums, key=lambda x: x.name)

        except (MediaNotFoundError, ProviderUnavailableError):
            raise
        except Exception as err:
            self.logger.error("Failed to browse period %s: %s", period, err)
            raise ProviderUnavailableError(f"Browse period error: {err}") from err

    @use_cache(expiration=86400)  # 24 hours - historical venue data is stable
    async def _get_shows_for_venue(self, venue_slug: str) -> list[BrowseFolder | Album]:
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

            albums: list[BrowseFolder | Album] = []
            for show in shows_data.get("shows", []):
                if show.get("audio_status") in ["complete", "partial"]:
                    album = show_to_album(self, show)
                    albums.append(album)

            return albums

        except (MediaNotFoundError, ProviderUnavailableError):
            raise
        except Exception as err:
            self.logger.error("Failed to get shows for venue %s: %s", venue_slug, err)
            raise ProviderUnavailableError(f"Venue shows error: {err}") from err

    @use_cache(expiration=86400)  # 24 hours - historical tag data is stable
    async def _get_shows_for_tag(self, tag_slug: str) -> list[BrowseFolder | Album | Track]:
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

            albums: list[BrowseFolder | Album | Track] = []
            for show in shows_data.get("shows", []):
                if show.get("audio_status") in ["complete", "partial"]:
                    album = show_to_album(self, show)
                    albums.append(album)

            return albums

        except (MediaNotFoundError, ProviderUnavailableError):
            raise
        except Exception as err:
            self.logger.error("Failed to get shows for tag %s: %s", tag_slug, err)
            raise ProviderUnavailableError(f"Tag shows error: {err}") from err
