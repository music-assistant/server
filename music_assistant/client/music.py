"""Handle Music/library related endpoints for Music Assistant."""

from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING

from music_assistant.common.models.enums import ImageType, MediaType
from music_assistant.common.models.media_items import (
    Album,
    AlbumTrack,
    Artist,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    Playlist,
    PlaylistTrack,
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
    ) -> list[Track]:
        """Get Track listing from the server."""
        return [
            Track.from_dict(obj)
            for obj in await self.client.send_command(
                "music/tracks/library_items",
                favorite=favorite,
                search=search,
                limit=limit,
                offset=offset,
                order_by=order_by,
            )
        ]

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
        in_library_only: bool = False,
    ) -> list[Album]:
        """Get all (known) albums this track is featured on."""
        return [
            Album.from_dict(item)
            for item in await self.client.send_command(
                "music/tracks/track_albums",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
                in_library_only=in_library_only,
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
    ) -> list[Album]:
        """Get Albums listing from the server."""
        return [
            Album.from_dict(obj)
            for obj in await self.client.send_command(
                "music/albums/library_items",
                favorite=favorite,
                search=search,
                limit=limit,
                offset=offset,
                order_by=order_by,
            )
        ]

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
        in_library_only: bool = False,
    ) -> list[AlbumTrack]:
        """Get tracks for given album."""
        return [
            AlbumTrack.from_dict(item)
            for item in await self.client.send_command(
                "music/albums/album_tracks",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
                in_library_only=in_library_only,
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
    ) -> list[Artist]:
        """Get Artists listing from the server."""
        return [
            Artist.from_dict(obj)
            for obj in await self.client.send_command(
                "music/artists/library_items",
                favorite=favorite,
                search=search,
                limit=limit,
                offset=offset,
                order_by=order_by,
                album_artists_only=album_artists_only,
            )
        ]

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
        in_library_only: bool = False,
    ) -> list[Track]:
        """Get (top)tracks for given artist."""
        return [
            Artist.from_dict(item)
            for item in await self.client.send_command(
                "music/artists/artist_tracks",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
                in_library_only=in_library_only,
            )
        ]

    async def get_artist_albums(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        in_library_only: bool = False,
    ) -> list[Album]:
        """Get (top)albums for given artist."""
        return [
            Album.from_dict(item)
            for item in await self.client.send_command(
                "music/artists/artist_albums",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
                in_library_only=in_library_only,
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
    ) -> list[Playlist]:
        """Get Playlists listing from the server."""
        return [
            Playlist.from_dict(obj)
            for obj in await self.client.send_command(
                "music/playlists/library_items",
                favorite=favorite,
                search=search,
                limit=limit,
                offset=offset,
                order_by=order_by,
            )
        ]

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
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[PlaylistTrack]:
        """Get tracks for given playlist."""
        return [
            PlaylistTrack.from_dict(obj)
            for obj in await self.client.send_command(
                "music/playlists/playlist_tracks",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
                limit=limit,
                offset=offset,
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
    ) -> list[Radio]:
        """Get Radio listing from the server."""
        return [
            Radio.from_dict(obj)
            for obj in await self.client.send_command(
                "music/radios/library_items",
                favorite=favorite,
                search=search,
                limit=limit,
                offset=offset,
                order_by=order_by,
            )
        ]

    async def get_radio(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> Radio:
        """Get single Radio from the server."""
        return Radio.from_dict(
            await self.client.send_command(
                "music/radios/get_radio",
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
                "music/radios/radio_versions",
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            )
        ]

    # Other/generic endpoints/commands

    async def start_sync(
        self,
        media_types: list[MediaType] | None = None,
        providers: list[str] | None = None,
    ) -> None:
        """Start running the sync of (all or selected) musicproviders.

        media_types: only sync these media types. None for all.
        providers: only sync these provider instances. None for all.
        """
        await self.client.send_command("music/sync", media_types=media_types, providers=providers)

    async def get_running_sync_tasks(self) -> list[SyncTask]:
        """Return list with providers that are currently (scheduled for) syncing."""
        return [SyncTask(**item) for item in await self.client.send_command("music/synctasks")]

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType] = MediaType.ALL,
        limit: int = 50,
    ) -> SearchResults:
        """Perform global search for media items on all providers.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: number of items to return in the search (per type).
        """
        return SearchResults.from_dict(
            await self.client.send_command(
                "music/search",
                search_query=search_query,
                media_types=media_types,
                limit=limit,
            ),
        )

    async def browse(
        self,
        path: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[MediaItemType]:
        """Browse Music providers."""
        return [
            media_from_dict(obj)
            for obj in await self.client.send_command(
                "music/browse",
                path=path,
                limit=limit,
                offset=offset,
            )
        ]

    async def recently_played(
        self, limit: int = 10, media_types: list[MediaType] | None = None
    ) -> list[MediaItemType]:
        """Return a list of the last played items."""
        return [
            media_from_dict(item)
            for item in await self.client.send_command(
                "music/recently_played_items", limit=limit, media_types=media_types
            )
        ]

    async def get_item_by_uri(
        self,
        uri: str,
    ) -> MediaItemType:
        """Get single music item providing a mediaitem uri."""
        return media_from_dict(await self.client.send_command("music/item_by_uri", uri=uri))

    async def get_item(
        self,
        media_type: MediaType,
        item_id: str,
        provider_instance_id_or_domain: str,
        force_refresh: bool = False,
        lazy: bool = True,
        add_to_library: bool = False,
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
                add_to_library=add_to_library,
            )
        )

    async def add_item_to_favorites(
        self,
        item: str | MediaItemType,
    ) -> None:
        """Add an item to the favorites."""
        await self.client.send_command("music/favorites/add_item", item=item)

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

    async def remove_item_from_library(
        self, media_type: MediaType, library_item_id: str | int
    ) -> None:
        """
        Remove item from the library.

        Destructive! Will remove the item and all dependants.
        """
        await self.client.send_command(
            "music/library/remove_item",
            media_type=media_type,
            library_item_id=library_item_id,
        )

    async def add_item_to_library(self, item: str | MediaItemType) -> MediaItemType:
        """Add item (uri or mediaitem) to the library."""
        return await self.client.send_command("music/library/add_item", item=item)

    async def refresh_item(
        self,
        media_item: MediaItemType,
    ) -> MediaItemType | None:
        """Try to refresh a mediaitem by requesting it's full object or search for substitutes."""
        if result := await self.client.send_command("music/refresh_item", media_item=media_item):
            return media_from_dict(result)
        return None

    # helpers

    def get_media_item_image(
        self,
        item: MediaItemType | ItemMapping,
        type: ImageType = ImageType.THUMB,  # noqa: A002
    ) -> MediaItemImage | None:
        """Get MediaItemImage for MediaItem, ItemMapping."""
        if not item:
            # guard for unexpected bad things
            return None
        # handle image in itemmapping
        if item.image and item.image.type == type:
            return item.image
        # always prefer album image for tracks
        album: Album | ItemMapping | None
        if album := getattr(item, "album", None):
            if album_image := self.get_media_item_image(album, type):
                return album_image
        # handle regular image within mediaitem
        metadata: MediaItemMetadata
        if metadata := getattr(item, "metadata", None):
            for img in metadata.images or []:
                if img.type == type:
                    return img
        # retry with album/track artist(s)
        artists: list[Artist | ItemMapping] | None
        if artists := getattr(item, "artists", None):
            for artist in artists:
                if artist_image := self.get_media_item_image(artist, type):
                    return artist_image
        # allow landscape fallback
        if type == ImageType.THUMB:
            return self.get_media_item_image(item, ImageType.LANDSCAPE)
        return None
