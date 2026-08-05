"""Library management for Tidal."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aiohttp.client_exceptions import ClientError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError, ResourceTemporarilyUnavailable

from .parsers import parse_favorite_tracks_playlist
from .parsers_v2 import parse_album as parse_album_v2
from .parsers_v2 import parse_artist as parse_artist_v2
from .parsers_v2 import parse_playlist as parse_playlist_v2
from .parsers_v2 import parse_track as parse_track_v2

# MediaType -> (official collection resource, JSON:API resource type).
_COLLECTIONS = {
    MediaType.ARTIST: ("userCollectionArtists", "artists"),
    MediaType.ALBUM: ("userCollectionAlbums", "albums"),
    MediaType.TRACK: ("userCollectionTracks", "tracks"),
    MediaType.PLAYLIST: ("userCollectionPlaylists", "playlists"),
}

# Errors treated as a failed (best-effort) collection write.
_WRITE_ERRORS = (ClientError, MediaNotFoundError, ResourceTemporarilyUnavailable)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.media_items import (
        Album,
        Artist,
        MediaItemType,
        Playlist,
        Track,
    )

    from .provider import TidalProvider


class TidalLibraryManager:
    """Manages Tidal library operations."""

    def __init__(self, provider: TidalProvider):
        """Initialize library manager."""
        self.provider = provider
        self.api = provider.api
        self.auth = provider.auth
        self.logger = provider.logger

    async def get_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve library artists."""
        async for doc in self.api.paginate_jsonapi(
            "userCollectionArtists/me/relationships/items", include=["items.profileArt"]
        ):
            for item in doc.data_list:
                if resource := doc.resolve(item):
                    artist = parse_artist_v2(self.provider, doc, resource)
                    _set_date_added(artist, item)
                    yield artist

    async def get_albums(self) -> AsyncGenerator[Album]:
        """Retrieve library albums."""
        async for doc in self.api.paginate_jsonapi(
            "userCollectionAlbums/me/relationships/items",
            include=["items.artists", "items.coverArt"],
            replace_media="items",
        ):
            for item in doc.data_list:
                if resource := doc.resolve(item):
                    album = parse_album_v2(self.provider, doc, resource)
                    _set_date_added(album, item)
                    yield album

    async def get_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve library tracks."""
        async for doc in self.api.paginate_jsonapi(
            "userCollectionTracks/me/relationships/items",
            include=["items.artists", "items.albums.coverArt"],
            replace_media="items",
        ):
            for item in doc.data_list:
                if resource := doc.resolve(item):
                    track = parse_track_v2(self.provider, doc, resource)
                    _set_date_added(track, item)
                    self.provider.note_replaced_track(item)
                    yield track

    async def get_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve library playlists."""
        # The official playlists collection returns both user playlists and
        # favourited mixes (as MIX-type playlists).
        async for doc in self.api.paginate_jsonapi(
            "userCollectionPlaylists/me/relationships/items",
            include=["items.coverArt", "items.owners"],
        ):
            for item in doc.data_list:
                if resource := doc.resolve(item):
                    playlist = parse_playlist_v2(self.provider, doc, resource)
                    _set_date_added(playlist, item)
                    yield playlist

        # The virtual "favorite tracks" playlist is a Music Assistant construct.
        yield parse_favorite_tracks_playlist(self.provider)

    async def add_item(self, item: MediaItemType) -> bool:
        """Add item to library."""
        return await self._modify_collection(item.item_id, item.media_type, "POST")

    async def remove_item(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from library."""
        return await self._modify_collection(prov_item_id, media_type, "DELETE")

    async def _modify_collection(self, item_id: str, media_type: MediaType, method: str) -> bool:
        """Add (POST) or remove (DELETE) an item via the official user collection."""
        collection = _COLLECTIONS.get(media_type)
        if not collection:
            return False
        resource_name, resource_type = collection
        # Mixes are stored with a "mix_" prefix but live in the playlists collection.
        if media_type == MediaType.PLAYLIST and item_id.startswith("mix_"):
            item_id = item_id[4:]
        try:
            if method == "POST" and media_type == MediaType.TRACK:
                return await self._add_track_with_healing(resource_name, resource_type, item_id)
            body = {"data": [{"type": resource_type, "id": item_id}]}
            await self.api.write_jsonapi(method, f"{resource_name}/me/relationships/items", body)
            return True
        except _WRITE_ERRORS:
            return False

    async def _add_track_with_healing(
        self, resource_name: str, resource_type: str, original_id: str
    ) -> bool:
        """Add a track to a user collection, healing a stale id if it was rejected."""
        send_id = await self.provider.redirect_cached_id(original_id)
        body = {"data": [{"type": resource_type, "id": send_id}]}
        result = await self.api.write_jsonapi(
            "POST", f"{resource_name}/me/relationships/items", body
        )
        # The add response reports rejected ids in meta.skipped. NOT_FOUND means the
        # id is stale (Tidal churns tracks, re-adding them under new ids), so heal it
        # via the live equivalent; ALREADY_PRESENT is a success. The top-level "data"
        # is the paginated collection listing (new items append at the end), not an
        # echo of what was accepted, so it must not be diffed to infer rejection.
        skipped = (result.get("meta") or {}).get("skipped") or []
        if not any(s.get("id") == send_id and s.get("reason") == "NOT_FOUND" for s in skipped):
            return True
        live = await self.provider.resolve_live_track_id(original_id)
        if not live or live == send_id:
            # The id is dead and could not be healed: nothing was added, so don't
            # report success (MA would mark the track as in-library).
            return False
        retry_body = {"data": [{"type": resource_type, "id": live}]}
        retry = await self.api.write_jsonapi(
            "POST", f"{resource_name}/me/relationships/items", retry_body
        )
        retry_skipped = (retry.get("meta") or {}).get("skipped") or []
        return not any(
            s.get("id") == live and s.get("reason") == "NOT_FOUND" for s in retry_skipped
        )


def _set_date_added(media_item: MediaItemType, item: dict[str, Any]) -> None:
    """Set date_added from a userCollection linkage item's addedAt meta."""
    if added := (item.get("meta") or {}).get("addedAt"):
        with suppress(ValueError):
            media_item.date_added = datetime.fromisoformat(added)
