"""Media retrieval operations for Tidal."""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any

from aiohttp.client_exceptions import ClientError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import SearchResults

from .parsers import parse_album, parse_artist, parse_playlist, parse_track

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
        parsed_results = SearchResults()
        media_type_strings = []

        if MediaType.ARTIST in media_types:
            media_type_strings.append("artists")
        if MediaType.ALBUM in media_types:
            media_type_strings.append("albums")
        if MediaType.TRACK in media_types:
            media_type_strings.append("tracks")
        if MediaType.PLAYLIST in media_types:
            media_type_strings.append("playlists")

        if not media_type_strings:
            return parsed_results

        results = await self.api.get_data(
            "search",
            params={
                "query": search_query.replace("'", ""),
                "limit": limit,
                "types": ",".join(media_type_strings),
            },
        )

        if "artists" in results and results["artists"].get("items"):
            parsed_results.artists = [
                parse_artist(self.provider, x) for x in results["artists"]["items"]
            ]
        if "albums" in results and results["albums"].get("items"):
            parsed_results.albums = [
                parse_album(self.provider, x) for x in results["albums"]["items"]
            ]
        if "playlists" in results and results["playlists"].get("items"):
            parsed_results.playlists = [
                parse_playlist(self.provider, x) for x in results["playlists"]["items"]
            ]
        if "tracks" in results and results["tracks"].get("items"):
            parsed_results.tracks = [
                parse_track(self.provider, x) for x in results["tracks"]["items"]
            ]
        return parsed_results

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details."""
        try:
            data = await self.api.get_data(f"artists/{prov_artist_id}")
            return parse_artist(self.provider, data)
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found") from err

    async def get_album(self, prov_album_id: str) -> Album:
        """Get album details."""
        try:
            data = await self.api.get_data(f"albums/{prov_album_id}")
            return parse_album(self.provider, data)
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Album {prov_album_id} not found") from err

    async def get_track(self, prov_track_id: str) -> Track:
        """Get track details."""
        try:
            track_obj = await self.api.get_data(f"tracks/{prov_track_id}")

            lyrics = None
            with suppress(MediaNotFoundError):
                lyrics = await self.api.get_data(f"tracks/{prov_track_id}/lyrics")

            return parse_track(self.provider, track_obj, lyrics=lyrics)
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Track {prov_track_id} not found") from err

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist details."""
        if prov_playlist_id.startswith("mix_"):
            return await self._get_mix_details(prov_playlist_id[4:])

        try:
            data = await self.api.get_data(f"playlists/{prov_playlist_id}")
            return parse_playlist(self.provider, data)
        except MediaNotFoundError:
            return await self._get_mix_details(prov_playlist_id)
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found") from err

    async def _get_mix_details(self, prov_mix_id: str) -> Playlist:
        """Get details for a Tidal Mix."""
        try:
            params = {"mixId": prov_mix_id, "deviceType": "BROWSER"}
            tidal_mix = await self.api.get_data("pages/mix", params=params)

            mix_obj = {
                "id": prov_mix_id,
                "title": tidal_mix.get("title", "Unknown Mix"),
                "updated": tidal_mix.get("lastUpdated", ""),
                "images": {},
            }

            # Try to extract images from rows/modules structure
            rows = tidal_mix.get("rows", [])
            if rows and (modules := rows[0].get("modules")):
                if mix_data := modules[0].get("mix"):
                    mix_obj["images"] = mix_data.get("images", {})

            if "subTitle" not in mix_obj:
                mix_obj["subTitle"] = tidal_mix.get("subTitle", "")

            return parse_playlist(self.provider, mix_obj, is_mix=True)
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Mix {prov_mix_id} not found") from err

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks."""
        try:
            data = await self.api.get_data(f"albums/{prov_album_id}/tracks", params={"limit": 250})
            return [parse_track(self.provider, x) for x in data.get("items", [])]
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Album {prov_album_id} not found") from err

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get artist albums."""
        try:
            data = await self.api.get_data(
                f"artists/{prov_artist_id}/albums", params={"limit": 250}
            )
            return [parse_album(self.provider, x) for x in data.get("items", [])]
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found") from err

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get artist top tracks."""
        try:
            data = await self.api.get_data(
                f"artists/{prov_artist_id}/toptracks", params={"limit": 10, "offset": 0}
            )
            return [parse_track(self.provider, x) for x in data.get("items", [])]
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found") from err

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Get similar tracks."""
        try:
            data = await self.api.get_data(f"tracks/{prov_track_id}/radio", params={"limit": limit})
            return [parse_track(self.provider, x) for x in data.get("items", [])]
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Track {prov_track_id} not found") from err

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        page_size = 200
        offset = page * page_size

        if prov_playlist_id.startswith("mix_"):
            return await self._get_mix_tracks(prov_playlist_id[4:], page_size, offset)

        try:
            data = await self.api.get_data(
                f"playlists/{prov_playlist_id}/tracks",
                params={"limit": page_size, "offset": offset},
            )
            return self._process_tracks(data.get("items", []), offset)
        except MediaNotFoundError:
            return await self._get_mix_tracks(prov_playlist_id, page_size, offset)

    async def _get_mix_tracks(self, mix_id: str, limit: int, offset: int) -> list[Track]:
        """Get tracks from a mix."""
        try:
            params = {"mixId": mix_id, "deviceType": "BROWSER"}
            data = await self.api.get_data("pages/mix", params=params)

            # Mix tracks are usually in the second row
            rows = data.get("rows", [])
            if len(rows) < 2:
                raise MediaNotFoundError(f"Mix {mix_id} has no tracks")

            modules = rows[1].get("modules", [])
            if not modules or "pagedList" not in modules[0]:
                raise MediaNotFoundError(f"Mix {mix_id} has no tracks")

            all_items = modules[0]["pagedList"].get("items", [])
            # Manual pagination for mixes
            paged_items = all_items[offset : offset + limit]
            return self._process_tracks(paged_items, offset)
        except (ClientError, KeyError, ValueError) as err:
            raise MediaNotFoundError(f"Mix {mix_id} not found") from err

    def _process_tracks(self, items: list[dict[str, Any]], offset: int) -> list[Track]:
        result = []
        for idx, item in enumerate(items, 1):
            try:
                track = parse_track(self.provider, item)
                track.position = offset + idx
                result.append(track)
            except (KeyError, TypeError):
                continue
        return result
