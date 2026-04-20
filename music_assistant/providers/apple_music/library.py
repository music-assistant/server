"""Library management for Apple Music."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MusicAssistantError

from .helpers.utils import is_catalog_id, is_library_id, translate_media_type_to_apple_type
from .parsers import parse_album, parse_artist, parse_playlist, parse_track

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.media_items import Album, Artist, MediaItemType, Playlist, Track

    from .provider import AppleMusicProvider


class AppleMusicLibraryManager:
    """Manages Apple Music library operations."""

    def __init__(self, provider: AppleMusicProvider) -> None:
        """Initialize library manager."""
        self.provider = provider
        self.api = provider.api_client
        self.logger = provider.logger

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from the provider."""
        endpoint = "me/library/artists"
        for item in await self.api.get_all_items(
            endpoint, include="catalog", extend="editorialNotes"
        ):
            if item and item["id"]:
                yield parse_artist(self.provider, item)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        endpoint = "me/library/albums"
        album_items = await self.api.get_all_items(
            endpoint, include="catalog,artists", extend="editorialNotes"
        )
        album_catalog_item_ids = [
            item["id"]
            for item in album_items
            if item and item["id"] and not is_library_id(item["id"])
        ]
        album_library_item_ids = [
            item["id"] for item in album_items if item and item["id"] and is_library_id(item["id"])
        ]
        rating_catalog_response = await self.api.get_ratings(
            album_catalog_item_ids, MediaType.ALBUM
        )
        rating_library_response = await self.api.get_ratings(
            album_library_item_ids, MediaType.ALBUM
        )
        for item in album_items:
            if item and item["id"]:
                is_favourite = (
                    rating_catalog_response.get(item["id"])
                    if not is_library_id(item["id"])
                    else rating_library_response.get(item["id"])
                )
                album = parse_album(self.provider, item, is_favourite)
                if album:
                    yield album

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        endpoint = "me/library/songs"
        song_catalog_ids = []
        library_only_tracks = []
        for item in await self.api.get_all_items(endpoint):
            catalog_id = item.get("attributes", {}).get("playParams", {}).get("catalogId")
            if not catalog_id:
                library_only_tracks.append(item)
            else:
                song_catalog_ids.append(catalog_id)
        # Obtain catalog info per 150 songs; the documented limit of 300 results in a 504 timeout.
        max_limit = 150
        for i in range(0, len(song_catalog_ids), max_limit):
            catalog_ids = song_catalog_ids[i : i + max_limit]
            catalog_endpoint = f"catalog/{self.provider._storefront}/songs"
            response = await self.api.get_data(
                catalog_endpoint, ids=",".join(catalog_ids), include="artists,albums"
            )
            rating_response = await self.api.get_ratings(catalog_ids, MediaType.TRACK)
            for item in response["data"]:
                is_favourite = rating_response.get(item["id"])
                yield parse_track(self.provider, item, is_favourite)
        library_ids = [item["id"] for item in library_only_tracks if item and item["id"]]
        library_rating_response = await self.api.get_ratings(library_ids, MediaType.TRACK)
        for item in library_only_tracks:
            is_favourite = library_rating_response.get(item["id"])
            yield parse_track(self.provider, item, is_favourite)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve playlists from the provider."""
        endpoint = "me/library/playlists"
        playlist_items = await self.api.get_all_items(endpoint)
        playlist_library_item_ids = [
            item["id"]
            for item in playlist_items
            if item and item["id"] and is_library_id(item["id"])
        ]
        rating_library_response = await self.api.get_ratings(
            playlist_library_item_ids, MediaType.PLAYLIST
        )
        for item in playlist_items:
            is_favourite = rating_library_response.get(item["id"])
            # Prefer catalog information over library information in case of public playlists
            if item["attributes"]["hasCatalog"]:
                yield await self.provider.get_playlist(
                    item["attributes"]["playParams"]["globalId"], is_favourite
                )
            elif item and item["id"]:
                yield parse_playlist(self.provider, item, is_favourite)

    async def library_add(self, item: MediaItemType) -> None:
        """Add item to library."""
        item_type = translate_media_type_to_apple_type(item.media_type)
        kwargs = {f"ids[{item_type}]": item.item_id}
        await self.api.post_data("me/library/", **kwargs)

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> None:
        """Remove item from library."""
        self.logger.warning(
            "Deleting items from your library is not yet supported by the Apple Music API. "
            f"Skipping deletion of {media_type} - {prov_item_id}."
        )

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        endpoint = f"me/library/playlists/{prov_playlist_id}/tracks"
        data = {
            "data": [
                {
                    "id": track_id,
                    "type": "library-songs" if is_library_id(track_id) else "songs",
                }
                for track_id in prov_track_ids
            ]
        }
        await self.api.post_data(endpoint, data=data)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        message = (
            "Removing tracks from playlists is not supported by the Apple Music "
            "API. Make sure to delete them using the Apple Music app."
        )
        raise MusicAssistantError(message)

    async def set_favorite(self, prov_item_id: str, media_type: MediaType, favorite: bool) -> None:
        """Set the favorite status of an item."""
        data = {
            "type": "ratings",
            "attributes": {"value": 1 if favorite else -1},
        }
        item_type = translate_media_type_to_apple_type(media_type)
        if is_catalog_id(prov_item_id):
            endpoint = f"me/ratings/{item_type}/{prov_item_id}"
        else:
            endpoint = f"me/ratings/library-{item_type}/{prov_item_id}"
        await self.api.put_data(endpoint, data=data)
