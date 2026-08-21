"""Media retrieval operations for Tidal."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from aiohttp.client_exceptions import ClientError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
)
from music_assistant_models.media_items import SearchResults

from .constants import FAVORITE_TRACKS_PLAYLIST_ID, PAGES_MIX, PLAYLISTS, SKIPPABLE_ITEM_ERRORS
from .parsers import (
    parse_favorite_tracks_playlist,
    parse_playlist,
    parse_track,
)
from .parsers_v2 import (
    _parse_items,
    _parse_or_skip,
)
from .parsers_v2 import (
    parse_album as parse_album_v2,
)
from .parsers_v2 import (
    parse_artist as parse_artist_v2,
)
from .parsers_v2 import (
    parse_playlist as parse_playlist_v2,
)
from .parsers_v2 import (
    parse_track as parse_track_v2,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Artist, Playlist, Track

    from .provider import TidalProvider


class TidalMediaManager:
    """Handles retrieval of media items from Tidal."""

    def __init__(self, provider: TidalProvider):
        """Initialize media retriever."""
        self.provider = provider
        self.api = provider.api
        self.logger = provider.logger

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Perform search on Tidal."""
        results = SearchResults()
        wanted = set(media_types)

        # Build the includes for the requested types only, keeping under the
        # official API's 10-included-resource cap. Track album covers are
        # included so track results carry artwork; standalone album results
        # trade artist names for staying within the cap.
        includes: list[str] = []
        if MediaType.TRACK in wanted:
            includes += ["tracks.artists", "tracks.albums.coverArt"]
        if MediaType.ALBUM in wanted:
            includes.append("albums.coverArt")
        if MediaType.ARTIST in wanted:
            includes.append("artists.profileArt")
        if MediaType.PLAYLIST in wanted:
            includes.append("playlists.coverArt")
        if not includes:
            return results

        # Since spec 1.10.101 search is a collection endpoint taking the query as
        # a filter and returning exactly one searchResults resource (with an
        # opaque id); the old /searchResults/{query} path 400s.
        doc = await self.api.get_jsonapi(
            "searchResults", params={"filter[query]": search_query}, include=includes
        )
        if not doc.data_list:
            return results
        data = doc.data_list[0]

        # Slice the resources before parsing so we only parse up to `limit` items.
        if MediaType.TRACK in wanted:
            results.tracks = [
                track
                for res in doc.related(data, "tracks")[:limit]
                if (track := _parse_or_skip(parse_track_v2, self.provider, doc, res)) is not None
            ]
        if MediaType.ALBUM in wanted:
            results.albums = [
                album
                for res in doc.related(data, "albums")[:limit]
                if (album := _parse_or_skip(parse_album_v2, self.provider, doc, res)) is not None
            ]
        if MediaType.ARTIST in wanted:
            results.artists = [
                artist
                for res in doc.related(data, "artists")[:limit]
                if (artist := _parse_or_skip(parse_artist_v2, self.provider, doc, res)) is not None
            ]
        if MediaType.PLAYLIST in wanted:
            results.playlists = [
                playlist
                for res in doc.related(data, "playlists")[:limit]
                if (playlist := _parse_or_skip(parse_playlist_v2, self.provider, doc, res))
                is not None
            ]
        return results

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details."""
        try:
            doc = await self.api.get_jsonapi(
                f"artists/{prov_artist_id}", include=["profileArt", "biography"]
            )
            return parse_artist_v2(self.provider, doc, doc.data)
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found") from err

    async def get_album(self, prov_album_id: str) -> Album:
        """Get album details."""
        try:
            doc = await self.api.get_jsonapi(
                f"albums/{prov_album_id}", include=["artists", "coverArt", "genres"]
            )
            return parse_album_v2(self.provider, doc, doc.data)
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Album {prov_album_id} not found") from err

    async def get_track(self, prov_track_id: str) -> Track:
        """Get track details."""
        try:
            # The album cover is resolved via the albums.coverArt include so the
            # track carries an image, matching the unofficial API's behaviour.
            doc = await self.api.get_jsonapi(
                f"tracks/{prov_track_id}",
                include=["artists", "albums", "albums.coverArt", "genres", "credits"],
            )
            track = parse_track_v2(self.provider, doc, doc.data)
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Track {prov_track_id} not found") from err

        # Lyrics remain on the unofficial API (not exposed at the official
        # third-party tier). A lyrics failure must not fail the track lookup.
        if lyrics := await self._get_lyrics(prov_track_id):
            if plain := lyrics.get("lyrics"):
                track.metadata.lyrics = plain
            if synced := lyrics.get("subtitles"):
                track.metadata.lrc_lyrics = synced

        return track

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details."""
        if prov_playlist_id == FAVORITE_TRACKS_PLAYLIST_ID:
            return parse_favorite_tracks_playlist(self.provider)

        if prov_playlist_id.startswith("mix_"):
            return await self._get_mix_details(prov_playlist_id[4:])

        try:
            data = await self.api.get(f"{PLAYLISTS}/{prov_playlist_id}")
            return parse_playlist(self.provider, data)
        except MediaNotFoundError:
            return await self._get_mix_details(prov_playlist_id)
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found") from err

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks."""
        tracks: list[Track] = []
        async for doc in self.api.paginate_jsonapi(
            f"albums/{prov_album_id}/relationships/items",
            include=["items.artists", "items.albums.coverArt"],
            replace_media="items",
        ):
            for item in doc.data_list:
                # The items relationship is mixed-type: an album's music
                # videos appear here too, and parsing one as a track would
                # yield an id that 404s on playback and shift trackNumber.
                if item.get("type") != "tracks":
                    continue
                if not (resource := doc.resolve(item)):
                    continue
                if (track := _parse_or_skip(parse_track_v2, self.provider, doc, resource)) is None:
                    continue
                item_meta = item.get("meta") or {}
                track.track_number = item_meta.get("trackNumber", 0) or 0
                track.disc_number = item_meta.get("volumeNumber", 0) or 0
                tracks.append(track)
        return tracks

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get artist albums."""
        albums: list[Album] = []
        async for doc in self.api.paginate_jsonapi(
            f"artists/{prov_artist_id}/relationships/albums",
            include=["albums.artists", "albums.coverArt"],
            replace_media="albums",
        ):
            albums.extend(_parse_items(parse_album_v2, self.provider, doc))
        return albums

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get artist top tracks."""
        # Top tracks are a bounded, ranked list: the first page is enough.
        doc = await self.api.get_jsonapi(
            f"artists/{prov_artist_id}/relationships/tracks",
            params={"collapseBy": "FINGERPRINT"},
            include=["tracks.artists", "tracks.albums.coverArt"],
            replace_media="tracks",
        )
        return _parse_items(parse_track_v2, self.provider, doc)

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Get similar tracks."""
        # Similar tracks are a bounded, ranked list: the first page is enough.
        doc = await self.api.get_jsonapi(
            f"tracks/{prov_track_id}/relationships/similarTracks",
            include=["similarTracks.artists", "similarTracks.albums.coverArt"],
            replace_media="similarTracks",
        )
        return _parse_items(parse_track_v2, self.provider, doc)[:limit]

    async def get_similar_artists(self, prov_artist_id: str, limit: int = 25) -> list[Artist]:
        """Get similar artists."""
        # Similar artists are a bounded, ranked list: the first page is enough.
        doc = await self.api.get_jsonapi(
            f"artists/{prov_artist_id}/relationships/similarArtists",
            include=["similarArtists.profileArt"],
        )
        return _parse_items(parse_artist_v2, self.provider, doc)[:limit]

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        page_size = 200
        offset = page * page_size

        if prov_playlist_id == FAVORITE_TRACKS_PLAYLIST_ID:
            return await self._get_favorite_tracks(offset)

        if prov_playlist_id.startswith("mix_"):
            return await self._get_mix_tracks(prov_playlist_id[4:], page_size, offset)

        try:
            data = await self.api.get(
                f"{PLAYLISTS}/{prov_playlist_id}/tracks",
                params={"limit": page_size, "offset": offset},
            )
            return self._process_tracks(data.get("items", []), offset)
        except MediaNotFoundError:
            return await self._get_mix_tracks(prov_playlist_id, page_size, offset)

    async def _get_mix_details(self, prov_mix_id: str) -> Playlist:
        """Get details for a Tidal Mix."""
        try:
            tidal_mix = await self._fetch_mix_page(prov_mix_id)
            mix_obj = {
                "id": prov_mix_id,
                "title": tidal_mix.get("title", "Unknown Mix"),
                "updated": tidal_mix.get("lastUpdated", ""),
                "subTitle": tidal_mix.get("subTitle", ""),
                "images": {},
            }
            if module := self._find_mix_module(tidal_mix.get("rows", []), "mix"):
                mix_obj["images"] = (module.get("mix") or {}).get("images", {})
            return parse_playlist(self.provider, mix_obj, is_mix=True)
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Mix {prov_mix_id} not found") from err

    async def _get_favorite_tracks(self, offset: int) -> list[Track]:
        """Get the user's favorite tracks from the official user collection (newest first)."""
        # The official collection is cursor-paginated, which does not map to MA's
        # page-based interface. Walk the whole collection on the first page (cached
        # by get_playlist_tracks) and return nothing for later pages.
        if offset > 0:
            return []
        tracks: list[Track] = []
        async for doc in self.api.paginate_jsonapi(
            "userCollectionTracks/me/relationships/items",
            include=["items.artists", "items.albums.coverArt"],
            # Request the newest-first order explicitly rather than relying on the
            # server default, since the positions below encode it.
            params={"sort": "-addedAt"},
            replace_media="items",
        ):
            for item in doc.data_list:
                if not (resource := doc.resolve(item)):
                    continue
                if (track := _parse_or_skip(parse_track_v2, self.provider, doc, resource)) is None:
                    continue
                track.position = len(tracks) + 1
                tracks.append(track)
                # Feed the stale->live pairs Tidal computed for this read into
                # the churn cache, as the library walk already does.
                self.provider.note_replaced_track(item)
        return tracks

    async def _get_mix_tracks(self, mix_id: str, limit: int, offset: int) -> list[Track]:
        """Get tracks from a mix."""
        try:
            data = await self._fetch_mix_page(mix_id)
            module = self._find_mix_module(data.get("rows", []), "pagedList")
            if not module:
                raise MediaNotFoundError(f"Mix {mix_id} has no tracks")
            all_items = module["pagedList"].get("items", [])
            # The mix feed is not itself paginated, so slice MA's page window in memory.
            paged_items = all_items[offset : offset + limit]
            return self._process_tracks(paged_items, offset)
        except (KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Mix {mix_id} not found") from err

    async def _get_lyrics(self, prov_track_id: str) -> dict[str, str] | None:
        """Get lyrics for a track, returning None when unavailable."""
        # Lyrics are optional enrichment: never fail the track lookup on
        # a missing/failed lyrics response.
        try:
            return await self.api.get(f"tracks/{prov_track_id}/lyrics")
        except (ClientError, MusicAssistantError) as err:
            self.logger.debug("Failed to fetch lyrics for track %s: %s", prov_track_id, err)
            return None

    def _process_tracks(self, items: list[dict[str, Any]], offset: int) -> list[Track]:
        result = []
        for idx, item in enumerate(items, 1):
            try:
                track = parse_track(self.provider, item)
                track.position = offset + idx
                result.append(track)
            except SKIPPABLE_ITEM_ERRORS as err:
                track_data = item.get("item", item) if isinstance(item, dict) else item
                self.logger.warning(
                    "Skipping Tidal track %s: %s",
                    track_data.get("id", "[no id]") if isinstance(track_data, dict) else "[no id]",
                    err,
                    exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
                )
                continue
        return result

    async def _fetch_mix_page(self, mix_id: str) -> dict[str, Any]:
        """
        Fetch the raw pages/mix feed for a mix, cached and shared.

        The single feed carries both the mix header and its track list, so caching it
        here lets get_playlist (details) and get_playlist_tracks share one upstream
        request per mix instead of fetching the same feed twice.
        """
        cache = self.provider.mass.cache
        cache_key = f"mix_page.{mix_id}"
        if (cached := await cache.get(cache_key, provider=self.provider.instance_id)) is not None:
            return cast("dict[str, Any]", cached)
        data = await self.api.get(PAGES_MIX, params={"mixId": mix_id, "deviceType": "BROWSER"})
        # Await the store: details and tracks are read back-to-back on a mix open,
        # and a background write could lose that race and refetch the feed.
        await cache.set(cache_key, data, expiration=3600 * 3, provider=self.provider.instance_id)
        return data

    @staticmethod
    def _find_mix_module(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
        """
        Return the first pages/mix module carrying the given key.

        The mix header (``mix``) and track list (``pagedList``) live in separate rows
        whose order Tidal does not guarantee, so locate them by content rather than by a
        fixed row/module index.
        """
        for row in rows:
            for module in row.get("modules") or []:
                if key in module:
                    return cast("dict[str, Any]", module)
        return None
