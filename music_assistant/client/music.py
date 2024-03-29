"""Handle Music/library related endpoints for Music Assistant."""

from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING

from music_assistant.common.models.enums import MediaType
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    MediaItemType,
    PagedItems,
    Playlist,
    Radio,
    SearchResults,
    Track,
    media_from_dict,
)
from music_assistant.common.models.provider import SyncTask

if TYPE_CHECKING:
    from .client import MusicAssistantClient


class Music:
    """Music(library) related endpoints/data for Music Assistant."""

    def __init__(self, client: MusicAssistantClient) -> None:
        """Handle Initialization."""
        self.client = client

    #  Tracks related endpoints/commands

    async def get_library_tracks(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        order_by: str | None = None,
    ) -> PagedItems:
        """Get Track listing from the server."""
        return PagedItems.parse(
            await self.client.send_command(
                "music/tracks/library_items",
                favorite=favorite,
                search=search,
                limit=limit,
                offset=offset,
                order_by=order_by,
            ),
            Track,
        )

    async def get_track(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        album_uri: str | None = None,
    ) -> Track:
        """Get single Track from the server."""
        return Track.from_dict(
            await self.client.send_command(
                "music/tracks/get_track",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
                album_uri=album_uri,
            ),
        )

    async def get_track_versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Track]:
        """Get all other versions for given Track from the server."""
        return [
            Track.from_dict(item)
            for item in await self.client.send_command(
                "music/tracks/track_versions",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            )
        ]

    async def get_track_albums(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Album]:
        """Get all (known) albums this track is featured on."""
        return [
            Album.from_dict(item)
            for item in await self.client.send_command(
                "music/tracks/track_albums",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            )
        ]

    def get_track_preview_url(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> str:
        """Get URL to preview clip of given track."""
        encoded_url = urllib.parse.quote(urllib.parse.quote(item_id))
        return f"{self.client.server_info.base_url}/preview?path={encoded_url}&provider={provider_instance_id_or_domain}"  # noqa: E501

    #  Albums related endpoints/commands

    async def get_library_albums(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        order_by: str | None = None,
    ) -> PagedItems:
        """Get Albums listing from the server."""
        return PagedItems.parse(
            await self.client.send_command(
                "music/albums/library_items",
                favorite=favorite,
                search=search,
                limit=limit,
                offset=offset,
                order_by=order_by,
            ),
            Album,
        )

    async def get_album(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> Album:
        """Get single Album from the server."""
        return Album.from_dict(
            await self.client.send_command(
                "music/albums/get_album",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            ),
        )

    async def get_album_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Track]:
        """Get tracks for given album."""
        return [
            Track.from_dict(item)
            for item in await self.client.send_command(
                "music/albums/album_tracks",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            )
        ]

    async def get_album_versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Album]:
        """Get all other versions for given Album from the server."""
        return [
            Album.from_dict(item)
            for item in await self.client.send_command(
                "music/albums/album_versions",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            )
        ]

    #  Artist related endpoints/commands

    async def get_library_artists(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        order_by: str | None = None,
        album_artists_only: bool = False,
    ) -> PagedItems:
        """Get Artists listing from the server."""
        return PagedItems.parse(
            await self.client.send_command(
                "music/artists/library_items",
                favorite=favorite,
                search=search,
                limit=limit,
                offset=offset,
                order_by=order_by,
                album_artists_only=album_artists_only,
            ),
            Artist,
        )

    async def get_artist(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> Artist:
        """Get single Artist from the server."""
        return Artist.from_dict(
            await self.client.send_command(
                "music/artists/get_artist",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            ),
        )

    async def get_artist_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Track]:
        """Get (top)tracks for given artist."""
        return [
            Artist.from_dict(item)
            for item in await self.client.send_command(
                "music/artists/artist_tracks",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            )
        ]

    async def get_artist_albums(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Album]:
        """Get (top)albums for given artist."""
        return [
            Album.from_dict(item)
            for item in await self.client.send_command(
                "music/artists/artist_albums",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            )
        ]

    #  Playlist related endpoints/commands

    async def get_library_playlists(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        order_by: str | None = None,
    ) -> PagedItems:
        """Get Playlists listing from the server."""
        return PagedItems.parse(
            await self.client.send_command(
                "music/playlists/library_items",
                favorite=favorite,
                search=search,
                limit=limit,
                offset=offset,
                order_by=order_by,
            ),
            Playlist,
        )

    async def get_playlist(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> Playlist:
        """Get single Playlist from the server."""
        return Playlist.from_dict(
            await self.client.send_command(
                "music/playlists/get_playlist",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            ),
        )

    async def get_playlist_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Track]:
        """Get tracks for given playlist."""
        return [
            Track.from_dict(item)
            for item in await self.client.send_command(
                "music/playlists/playlist_tracks",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            )
        ]

    async def add_playlist_tracks(self, db_playlist_id: str | int, uris: list[str]) -> None:
        """Add multiple tracks to playlist. Creates background tasks to process the action."""
        await self.client.send_command(
            "music/playlists/add_playlist_tracks",
            db_playlist_id=db_playlist_id,
            uris=uris,
        )

    async def remove_playlist_tracks(
        self, db_playlist_id: str | int, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove multiple tracks from playlist."""
        await self.client.send_command(
            "music/playlists/remove_playlist_tracks",
            db_playlist_id=db_playlist_id,
            positions_to_remove=positions_to_remove,
        )

    async def create_playlist(
        self, name: str, provider_instance_or_domain: str | None = None
    ) -> Playlist:
        """Create new playlist."""
        return Playlist.from_dict(
            await self.client.send_command(
                "music/playlists/create_playlist",
                name=name,
                provider_instance_or_domain=provider_instance_or_domain,
            )
        )

    #  Radio related endpoints/commands

    async def get_library_radios(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        order_by: str | None = None,
    ) -> PagedItems:
        """Get Radio listing from the server."""
        return PagedItems.parse(
            await self.client.send_command(
                "music/radio/library_items",
                favorite=favorite,
                search=search,
                limit=limit,
                offset=offset,
                order_by=order_by,
            ),
            Radio,
        )

    async def get_radio(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> Radio:
        """Get single Radio from the server."""
        return Radio.from_dict(
            await self.client.send_command(
                "music/radio/get_item",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            ),
        )

    async def get_radio_versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Radio]:
        """Get all other versions for given Radio from the server."""
        return [
            Radio.from_dict(item)
            for item in await self.client.send_command(
                "music/radio/radio_versions",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            )
        ]

    # Other/generic endpoints/commands

    async def get_item_by_uri(
        self,
        uri: str,
    ) -> MediaItemType:
        """Get single music item providing a mediaitem uri."""
        return media_from_dict(await self.client.send_command("music/item_by_uri", uri=uri))

    async def refresh_item(
        self,
        media_item: MediaItemType,
    ) -> MediaItemType | None:
        """Try to refresh a mediaitem by requesting it's full object or search for substitutes."""
        if result := await self.client.send_command("music/refresh_item", media_item=media_item):
            return media_from_dict(result)
        return None

    async def get_item(
        self,
        media_type: MediaType,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> MediaItemType:
        """Get single music item by id and media type."""
        return media_from_dict(
            await self.client.send_command(
                "music/item",
                media_type=media_type,
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            )
        )

    async def add_item_to_library(self, item: str | MediaItemType) -> MediaItemType:
        """Add item (uri or mediaitem) to the library."""
        await self.client.send_command("music/library/add_item", item=item)

    async def remove_item_from_library(
        self, media_type: MediaType, library_item_id: str | int
    ) -> None:
        """
        Remove item from the library.

        Destructive! Will remove the item and all dependants.
        """
        await self.client.send_command(
            "music/library/remove",
            media_type=media_type,
            library_item_id=library_item_id,
        )

    async def add_item_to_favorites(
        self,
        media_type: MediaType,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> None:
        """Add an item to the favorites."""
        await self.client.send_command(
            "music/favorites/add_item",
            media_type=media_type,
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
        )

    async def remove_item_from_favorites(
        self,
        media_type: MediaType,
        item_id: str | int,
    ) -> None:
        """Remove (library) item from the favorites."""
        await self.client.send_command(
            "music/favorites/remove_item",
            media_type=media_type,
            item_id=item_id,
        )

    async def browse(
        self,
        path: str | None = None,
    ) -> list[MediaItemType]:
        """Browse Music providers."""
        return [
            media_from_dict(item)
            for item in await self.client.send_command("music/browse", path=path)
        ]

    async def search(
        self,
        search_query: str,
        media_types: tuple[MediaType] = MediaType.ALL,
        limit: int = 25,
    ) -> SearchResults:
        """Perform global search for media items on all providers."""
        return SearchResults.from_dict(
            await self.client.send_command(
                "music/search",
                search_query=search_query,
                media_types=media_types,
                limit=limit,
            ),
        )

    async def get_sync_tasks(self) -> list[SyncTask]:
        """Return any/all sync tasks that are in progress on the server."""
        return [SyncTask(**item) for item in await self.client.send_command("music/synctasks")]
