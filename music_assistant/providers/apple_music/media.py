"""Catalog and media lookups for Apple Music."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Playlist,
    SearchResults,
    Track,
)

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.track_filter import filter_tracks

from .constants import ARTWORK_CACHE_EXPIRATION, PARSED_ITEM_CACHE_CHECKSUM
from .helpers.utils import is_catalog_id, is_library_id, translate_media_type_to_apple_type
from .parsers import (
    format_artwork_url,
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
)

if TYPE_CHECKING:
    from .provider import AppleMusicProvider


class AppleMusicMediaManager:
    """Handles catalog reads and search for Apple Music."""

    def __init__(self, provider: AppleMusicProvider) -> None:
        """Initialize media manager."""
        self.provider = provider
        self.mass = provider.mass
        self.instance_id = provider.instance_id
        self.domain = provider.domain
        self.api = provider.api_client
        self.logger = provider.logger

    @use_cache(cache_checksum=PARSED_ITEM_CACHE_CHECKSUM)
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType] | None,
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on the Apple Music catalog."""
        endpoint = f"catalog/{self.provider._storefront}/search"
        limit = min(limit, 25)
        searchresult = SearchResults()
        if not media_types:
            return searchresult
        searchtypes = []
        if MediaType.ARTIST in media_types:
            searchtypes.append("artists")
        if MediaType.ALBUM in media_types:
            searchtypes.append("albums")
        if MediaType.TRACK in media_types:
            searchtypes.append("songs")
        if MediaType.PLAYLIST in media_types:
            searchtypes.append("playlists")
        if not searchtypes:
            return searchresult
        searchtype = ",".join(searchtypes)
        search_query = search_query.replace("'", "")
        response = await self.api.get_data(
            endpoint, term=search_query, types=searchtype, limit=limit
        )
        if "artists" in response["results"]:
            searchresult.artists = [
                *searchresult.artists,
                *(
                    parse_artist(self.provider, item)
                    for item in response["results"]["artists"]["data"]
                ),
            ]
        if "albums" in response["results"]:
            searchresult.albums = [
                *searchresult.albums,
                *(
                    cast("Album", parse_album(self.provider, item))
                    for item in response["results"]["albums"]["data"]
                ),
            ]
        if "songs" in response["results"]:
            searchresult.tracks = [
                *searchresult.tracks,
                *(
                    parse_track(self.provider, item)
                    for item in response["results"]["songs"]["data"]
                ),
            ]
        if "playlists" in response["results"]:
            searchresult.playlists = [
                *searchresult.playlists,
                *(
                    parse_playlist(self.provider, item)
                    for item in response["results"]["playlists"]["data"]
                ),
            ]
        return searchresult

    @use_cache(cache_checksum=PARSED_ITEM_CACHE_CHECKSUM)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        if is_library_id(prov_artist_id):
            endpoint = f"me/library/artists/{prov_artist_id}"
            response = await self.api.get_data(endpoint, include="catalog", extend="editorialNotes")
        else:
            endpoint = f"catalog/{self.provider._storefront}/artists/{prov_artist_id}"
            response = await self.api.get_data(endpoint, extend="editorialNotes")
        return cast("Artist", parse_artist(self.provider, response["data"][0]))

    @use_cache(cache_checksum=PARSED_ITEM_CACHE_CHECKSUM)
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        if is_library_id(prov_album_id):
            endpoint = f"me/library/albums/{prov_album_id}"
            response = await self.api.get_data(endpoint, include="catalog,artists")
        else:
            endpoint = f"catalog/{self.provider._storefront}/albums/{prov_album_id}"
            response = await self.api.get_data(endpoint, include="artists")
        rating_response = await self.api.get_ratings([prov_album_id], MediaType.ALBUM)
        is_favourite = rating_response.get(prov_album_id)
        return cast("Album", parse_album(self.provider, response["data"][0], is_favourite))

    @use_cache(cache_checksum=PARSED_ITEM_CACHE_CHECKSUM)
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        if is_library_id(prov_track_id):
            endpoint = f"me/library/songs/{prov_track_id}"
            response = await self.api.get_data(endpoint, include="catalog,artists,albums")
        else:
            endpoint = f"catalog/{self.provider._storefront}/songs/{prov_track_id}"
            response = await self.api.get_data(endpoint, include="artists,albums")
        rating_response = await self.api.get_ratings([prov_track_id], MediaType.TRACK)
        is_favourite = rating_response.get(prov_track_id)
        return parse_track(self.provider, response["data"][0], is_favourite)

    async def get_playlist(
        self,
        prov_playlist_id: str,
        is_favourite: bool = False,
        can_edit_hint: bool | None = None,
        library_id_override: str | None = None,
    ) -> Playlist:
        """Get full playlist details by id."""
        return await self._get_regular_playlist(
            prov_playlist_id,
            is_favourite,
            can_edit_hint=can_edit_hint,
            library_id_override=library_id_override,
        )

    @use_cache(ARTWORK_CACHE_EXPIRATION, cache_checksum=PARSED_ITEM_CACHE_CHECKSUM)
    async def get_artwork_url(self, media_type: str, prov_item_id: str) -> str | None:
        """
        Return the current artwork URL for the given item, if any.

        Blobstore artwork URLs are presigned with a limited lifetime, so they are
        resolved on demand (and cached well below the signature lifetime) instead
        of being persisted anywhere.

        :param media_type: The media type value of the artwork token (album/track/...).
        :param prov_item_id: The provider item id of the item the artwork belongs to.
        """
        try:
            apple_type = translate_media_type_to_apple_type(MediaType(media_type))
        except ValueError, MusicAssistantError:
            return None
        # playlists use globalId ("pl.") catalog ids; all other types use the
        # library id format to tell library and catalog items apart
        if media_type == MediaType.PLAYLIST.value:
            in_library = not is_catalog_id(prov_item_id)
        else:
            in_library = is_library_id(prov_item_id)
        if in_library:
            endpoint = f"me/library/{apple_type}/{prov_item_id}"
        else:
            endpoint = f"catalog/{self.provider._storefront}/{apple_type}/{prov_item_id}"
        response = await self.api.get_data(endpoint, include="catalog")
        item_obj = response["data"][0]
        attributes = item_obj.get("attributes") or {}
        if not attributes.get("artwork"):
            # library items may only carry artwork on their catalog counterpart
            catalog_data = item_obj.get("relationships", {}).get("catalog", {}).get("data") or []
            if catalog_data:
                attributes = catalog_data[0].get("attributes") or {}
        return format_artwork_url(attributes)

    @use_cache(cache_checksum=PARSED_ITEM_CACHE_CHECKSUM)
    async def _get_regular_playlist(
        self,
        prov_playlist_id: str,
        is_favourite: bool = False,
        can_edit_hint: bool | None = None,
        library_id_override: str | None = None,
    ) -> Playlist:
        """Fetch and cache details for a regular (non-station) playlist."""
        if not is_catalog_id(prov_playlist_id):
            endpoint = f"me/library/playlists/{prov_playlist_id}"
        else:
            endpoint = f"catalog/{self.provider._storefront}/playlists/{prov_playlist_id}"
        response = await self.api.get_data(endpoint)
        return parse_playlist(
            self.provider,
            response["data"][0],
            is_favourite,
            can_edit_hint=can_edit_hint,
            library_id_override=library_id_override,
        )

    @use_cache(cache_checksum=PARSED_ITEM_CACHE_CHECKSUM, allow_expired_cache=True)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all album tracks for given album id."""
        if is_library_id(prov_album_id):
            endpoint = f"me/library/albums/{prov_album_id}/tracks"
            response = await self.api.get_data(endpoint, include="catalog,artists")
        else:
            endpoint = f"catalog/{self.provider._storefront}/albums/{prov_album_id}/tracks"
            response = await self.api.get_data(endpoint, include="artists")
        album = await self.get_album(prov_album_id)
        track_ids = [track_obj["id"] for track_obj in response["data"] if "id" in track_obj]
        rating_response = await self.api.get_ratings(track_ids, MediaType.TRACK)
        tracks = []
        for track_obj in response["data"]:
            if "id" not in track_obj:
                continue
            track = parse_track(self.provider, track_obj, rating_response.get(track_obj["id"]))
            track.album = album
            tracks.append(track)
        return tracks

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get all playlist tracks for given playlist id."""
        if prov_playlist_id.startswith("ra."):
            if page > 0:
                return []
            return await self._get_station_tracks(prov_playlist_id)
        return await self._get_playlist_tracks_cached(prov_playlist_id, page)

    @use_cache(3600 * 3, cache_checksum=PARSED_ITEM_CACHE_CHECKSUM)
    async def _get_playlist_tracks_cached(
        self, prov_playlist_id: str, page: int = 0
    ) -> list[Track]:
        """Fetch and cache tracks for a regular (non-station) playlist."""
        if is_catalog_id(prov_playlist_id):
            endpoint = f"catalog/{self.provider._storefront}/playlists/{prov_playlist_id}/tracks"
        else:
            endpoint = f"me/library/playlists/{prov_playlist_id}/tracks"
        result: list[Track] = []
        page_size = 100
        offset = page * page_size
        response = await self.api.get_data(
            endpoint, include="artists,catalog", limit=page_size, offset=offset
        )
        if not response or "data" not in response:
            return result
        playlist_track_ids = [track["id"] for track in response["data"] if track and track["id"]]
        rating_response = await self.api.get_ratings(playlist_track_ids, MediaType.TRACK)
        for index, track in enumerate(response["data"]):
            if track and track["id"]:
                is_favourite = rating_response.get(track["id"])
                parsed_track = parse_track(self.provider, track, is_favourite)
                parsed_track.position = offset + index + 1
                result.append(parsed_track)
        return result

    async def _get_station_tracks(self, station_id: str) -> list[Track]:
        """Fetch the next batch of tracks for a radio station."""
        tracks = await self._fetch_station_tracks(station_id)
        if not tracks:
            # Apple may rotate station IDs for personal stations; try to resolve the current one.
            fresh_id = await self.provider.recommendation_manager.resolve_station_id(station_id)
            if fresh_id and fresh_id != station_id:
                self.logger.debug(
                    "Station ID %s appears stale, retrying with refreshed ID %s",
                    station_id,
                    fresh_id,
                )
                tracks = await self._fetch_station_tracks(fresh_id)
        return filter_tracks(tracks)

    async def _fetch_station_tracks(self, station_id: str) -> list[Track]:
        """Fetch tracks for a station ID from the Apple Music API."""
        response = await self.api.post_data(
            f"me/stations/next-tracks/{station_id}", include="artists"
        )
        tracks = response.get("data", [])
        if not tracks:
            self.logger.debug(
                "No tracks returned for station_id=%s; errors=%s",
                station_id,
                response.get("errors"),
            )
            return []
        track_ids = [t["id"] for t in tracks if t and t.get("id")]
        rating_response = await self.api.get_ratings(track_ids, MediaType.TRACK)
        return [
            parse_track(self.provider, t, rating_response.get(t["id"]))
            for t in tracks
            if t and t.get("id")
        ]

    @use_cache(3600 * 24 * 7, cache_checksum=PARSED_ITEM_CACHE_CHECKSUM, allow_expired_cache=True)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        endpoint = f"catalog/{self.provider._storefront}/artists/{prov_artist_id}/albums"
        try:
            response = await self.api.get_all_items(endpoint)
        except MediaNotFoundError:
            self.logger.info("No albums found for artist %s", prov_artist_id)
            return []
        album_ids = [album["id"] for album in response if album["id"]]
        rating_response = await self.api.get_ratings(album_ids, MediaType.ALBUM)
        albums = []
        for album in response:
            if not album["id"]:
                continue
            parsed = parse_album(self.provider, album, rating_response.get(album["id"]))
            if parsed:
                albums.append(cast("Album", parsed))
        return albums

    @use_cache(3600 * 24 * 7, cache_checksum=PARSED_ITEM_CACHE_CHECKSUM, allow_expired_cache=True)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        endpoint = f"catalog/{self.provider._storefront}/artists/{prov_artist_id}/view/top-songs"
        try:
            response = await self.api.get_data(endpoint)
        except MediaNotFoundError:
            self.logger.info("No top tracks found for artist %s", prov_artist_id)
            return []
        track_ids = [track["id"] for track in response["data"] if track["id"]]
        rating_response = await self.api.get_ratings(track_ids, MediaType.TRACK)
        return [
            parse_track(self.provider, track, rating_response.get(track["id"]))
            for track in response["data"]
            if track["id"]
        ]
