"""Handle Music/library related endpoints for Music Assistant."""
from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.common.models.enums import MediaType
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    BrowseFolder,
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

    async def get_tracks(
        self,
        in_library: bool | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        order_by: str | None = None,
    ) -> PagedItems:
        """Get Track listing from the server."""
        return PagedItems.parse(
            await self.client.send_command(
                "music/tracks",
                in_library=in_library,
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
        force_refresh: bool | None = None,
        lazy: bool | None = None,
        album: str | None = None,
    ) -> Track:
        """Get single Track from the server."""
        return Track.from_dict(
            await self.client.send_command(
                "music/track",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
                force_refresh=force_refresh,
                lazy=lazy,
                album=album,
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
                "music/track/versions",
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
                "music/track/albums",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            )
        ]

    async def get_track_preview_url(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> str:
        """Get URL to preview clip of given track."""
        return await self.client.send_command(
            "music/track/preview",
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
        )

    #  Albums related endpoints/commands

    async def get_albums(
        self,
        in_library: bool | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        order_by: str | None = None,
    ) -> PagedItems:
        """Get Albums listing from the server."""
        return PagedItems.parse(
            await self.client.send_command(
                "music/albums",
                in_library=in_library,
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
        force_refresh: bool | None = None,
        lazy: bool | None = None,
    ) -> Album:
        """Get single Album from the server."""
        return Album.from_dict(
            await self.client.send_command(
                "music/album",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
                force_refresh=force_refresh,
                lazy=lazy,
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
                "music/album/tracks",
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
                "music/album/versions",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            )
        ]

    #  Artist related endpoints/commands

    async def get_artists(
        self,
        in_library: bool | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        order_by: str | None = None,
    ) -> PagedItems:
        """Get Artists listing from the server."""
        return PagedItems.parse(
            await self.client.send_command(
                "music/artists",
                in_library=in_library,
                search=search,
                limit=limit,
                offset=offset,
                order_by=order_by,
            ),
            Artist,
        )

    async def get_album_artists(
        self,
        in_library: bool | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        order_by: str | None = None,
    ) -> PagedItems:
        """Get AlbumArtists listing from the server."""
        return PagedItems.parse(
            await self.client.send_command(
                "music/albumartists",
                in_library=in_library,
                search=search,
                limit=limit,
                offset=offset,
                order_by=order_by,
            ),
            Artist,
        )

    async def get_artist(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        force_refresh: bool | None = None,
        lazy: bool | None = None,
    ) -> Artist:
        """Get single Artist from the server."""
        return Artist.from_dict(
            await self.client.send_command(
                "music/artist",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
                force_refresh=force_refresh,
                lazy=lazy,
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
                "music/artist/tracks",
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
                "music/artist/albums",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            )
        ]

    #  Playlist related endpoints/commands

    async def get_playlists(
        self,
        in_library: bool | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        order_by: str | None = None,
    ) -> PagedItems:
        """Get Playlists listing from the server."""
        return PagedItems.parse(
            await self.client.send_command(
                "music/playlists",
                in_library=in_library,
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
        force_refresh: bool | None = None,
        lazy: bool | None = None,
    ) -> Playlist:
        """Get single Playlist from the server."""
        return Playlist.from_dict(
            await self.client.send_command(
                "music/playlist",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
                force_refresh=force_refresh,
                lazy=lazy,
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
                "music/playlist/tracks",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            )
        ]

    async def add_playlist_tracks(self, db_playlist_id: str | int, uris: list[str]) -> None:
        """Add multiple tracks to playlist. Creates background tasks to process the action."""
        await self.client.send_command(
            "music/playlist/tracks/add",
            db_playlist_id=db_playlist_id,
            uris=uris,
        )

    async def remove_playlist_tracks(
        self, db_playlist_id: str | int, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove multiple tracks from playlist."""
        await self.client.send_command(
            "music/playlist/tracks/add",
            db_playlist_id=db_playlist_id,
            positions_to_remove=positions_to_remove,
        )

    async def create_playlist(
        self, name: str, provider_instance_or_domain: str | None = None
    ) -> Playlist:
        """Create new playlist."""
        return Playlist.from_dict(
            await self.client.send_command(
                "music/playlist/create",
                name=name,
                provider_instance_or_domain=provider_instance_or_domain,
            )
        )

    #  Radio related endpoints/commands

    async def get_radios(
        self,
        in_library: bool | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        order_by: str | None = None,
    ) -> PagedItems:
        """Get Radio listing from the server."""
        return PagedItems.parse(
            await self.client.send_command(
                "music/radios",
                in_library=in_library,
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
        force_refresh: bool | None = None,
        lazy: bool | None = None,
    ) -> Radio:
        """Get single Radio from the server."""
        return Radio.from_dict(
            await self.client.send_command(
                "music/radio",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
                force_refresh=force_refresh,
                lazy=lazy,
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
                "music/radio/versions",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            )
        ]

    # Other/generic endpoints/commands

    async def get_item_by_uri(
        self,
        uri: str,
        force_refresh: bool | None = None,
        lazy: bool | None = None,
    ) -> MediaItemType:
        """Get single music item providing a mediaitem uri."""
        return media_from_dict(
            await self.client.send_command(
                "music/item_by_uri", uri=uri, force_refresh=force_refresh, lazy=lazy
            )
        )

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
        force_refresh: bool | None = None,
        lazy: bool | None = None,
    ) -> MediaItemType:
        """Get single music item by id and media type."""
        return media_from_dict(
            await self.client.send_command(
                "music/item",
                media_type=media_type,
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
                force_refresh=force_refresh,
                lazy=lazy,
            )
        )

    async def add_to_library(
        self,
        media_type: MediaType,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> None:
        """Add an item to the library."""
        await self.client.send_command(
            "music/library/add",
            media_type=media_type,
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
        )

    async def remove_from_library(
        self,
        media_type: MediaType,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> None:
        """Remove an item from the library."""
        await self.client.send_command(
            "music/library/remove",
            media_type=media_type,
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
        )

    async def delete_db_item(
        self, media_type: MediaType, db_item_id: str | int, recursive: bool = False
    ) -> None:
        """Remove item from the database."""
        await self.client.send_command(
            "music/delete", media_type=media_type, db_item_id=db_item_id, recursive=recursive
        )

    async def browse(
        self,
        path: str | None = None,
    ) -> BrowseFolder:
        """Browse Music providers."""
        return BrowseFolder.from_dict(
            await self.client.send_command("music/browse", path=path),
        )

    async def search(
        self, search_query: str, media_types: tuple[MediaType] = MediaType.ALL, limit: int = 25
    ) -> SearchResults:
        """Perform global search for media items on all providers."""
        return SearchResults.from_dict(
            await self.client.send_command(
                "music/search", search_query=search_query, media_types=media_types, limit=limit
            ),
        )

    async def get_sync_tasks(self) -> list[SyncTask]:
        """Return any/all sync tasks that are in progress on the server."""
        return [
            SyncTask.from_dict(item) for item in await self.client.send_command("music/synctasks")
        ]
