"""Library management for Tidal."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aiohttp.client_exceptions import ClientError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError, ResourceTemporarilyUnavailable

from .constants import (
    FAVORITES_ALBUMS,
    FAVORITES_ARTISTS,
    FAVORITES_MIXES,
    FAVORITES_PLAYLISTS,
    FAVORITES_TRACKS,
)
from .parsers import parse_favorite_tracks_playlist
from .parsers_v2 import parse_album as parse_album_v2
from .parsers_v2 import parse_artist as parse_artist_v2
from .parsers_v2 import parse_playlist as parse_playlist_v2
from .parsers_v2 import parse_track as parse_track_v2

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
        ):
            for item in doc.data_list:
                if resource := doc.resolve(item):
                    track = parse_track_v2(self.provider, doc, resource)
                    _set_date_added(track, item)
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
        endpoint, data, is_mix = self._get_endpoint_data(item.item_id, item.media_type, "add")
        if not endpoint:
            return False

        try:
            if is_mix:
                await self.api.put(endpoint, data=data, as_form=True)
            else:
                await self.api.post(
                    f"users/{self.auth.user_id}/{endpoint}", data=data, as_form=True
                )
            return True
        except ClientError, MediaNotFoundError, ResourceTemporarilyUnavailable:
            return False

    async def remove_item(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from library."""
        endpoint, data, is_mix = self._get_endpoint_data(prov_item_id, media_type, "remove")
        if not endpoint:
            return False

        try:
            if is_mix:
                await self.api.put(endpoint, data=data, as_form=True)
            else:
                await self.api.delete(f"users/{self.auth.user_id}/{endpoint}")
            return True
        except ClientError, MediaNotFoundError, ResourceTemporarilyUnavailable:
            return False

    def _get_endpoint_data(
        self, item_id: str, media_type: MediaType, operation: str
    ) -> tuple[str | None, dict[str, Any], bool]:
        """Get endpoint and data for library operations."""
        if media_type == MediaType.PLAYLIST and item_id.startswith("mix_"):
            mix_id = item_id[4:]
            if operation == "add":
                return (
                    f"{FAVORITES_MIXES}/add",
                    {
                        "mixIds": mix_id,
                        "onArtifactNotFound": "FAIL",
                        "deviceType": "BROWSER",
                    },
                    True,
                )
            return (
                f"{FAVORITES_MIXES}/remove",
                {"mixIds": mix_id, "deviceType": "BROWSER"},
                True,
            )

        if media_type == MediaType.ARTIST:
            return (
                (FAVORITES_ARTISTS, {"artistId": item_id}, False)
                if operation == "add"
                else (f"{FAVORITES_ARTISTS}/{item_id}", {}, False)
            )
        if media_type == MediaType.ALBUM:
            return (
                (FAVORITES_ALBUMS, {"albumId": item_id}, False)
                if operation == "add"
                else (f"{FAVORITES_ALBUMS}/{item_id}", {}, False)
            )
        if media_type == MediaType.TRACK:
            return (
                (FAVORITES_TRACKS, {"trackId": item_id}, False)
                if operation == "add"
                else (f"{FAVORITES_TRACKS}/{item_id}", {}, False)
            )
        if media_type == MediaType.PLAYLIST:
            return (
                (FAVORITES_PLAYLISTS, {"uuids": item_id}, False)
                if operation == "add"
                else (f"{FAVORITES_PLAYLISTS}/{item_id}", {}, False)
            )

        return None, {}, False


def _set_date_added(media_item: MediaItemType, item: dict[str, Any]) -> None:
    """Set date_added from a userCollection linkage item's addedAt meta."""
    if added := (item.get("meta") or {}).get("addedAt"):
        with suppress(ValueError):
            media_item.date_added = datetime.fromisoformat(added)
