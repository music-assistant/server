"""Library management for Tidal."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiohttp.client_exceptions import ClientError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError, ResourceTemporarilyUnavailable

from .constants import (
    BASE_URL_V2,
    FAVORITES_ALBUMS,
    FAVORITES_ARTISTS,
    FAVORITES_MIXES,
    FAVORITES_PLAYLISTS,
    FAVORITES_TRACKS,
)
from .parsers import (
    parse_album,
    parse_artist,
    parse_favorite_tracks_playlist,
    parse_playlist,
    parse_track,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.media_items import Album, Artist, MediaItemType, Playlist, Track

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
        path = f"users/{self.auth.user_id}/{FAVORITES_ARTISTS}"
        async for item in self.api.paginate(path):
            if item and item.get("item") and item["item"].get("id"):
                yield parse_artist(self.provider, item)

    async def get_albums(self) -> AsyncGenerator[Album]:
        """Retrieve library albums."""
        path = f"users/{self.auth.user_id}/{FAVORITES_ALBUMS}"
        async for item in self.api.paginate(path):
            if item and item.get("item") and item["item"].get("id"):
                yield parse_album(self.provider, item)

    async def get_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve library tracks."""
        path = f"users/{self.auth.user_id}/{FAVORITES_TRACKS}"
        async for item in self.api.paginate(path):
            if item and item.get("item") and item["item"].get("id"):
                yield parse_track(self.provider, item)

    async def get_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve library playlists."""
        # 1. Get favorite mixes
        async for item in self.api.paginate(
            FAVORITES_MIXES, item_key="items", base_url=BASE_URL_V2, cursor_based=True
        ):
            if item and item.get("id"):
                yield parse_playlist(self.provider, item, is_mix=True)

        # 2. Get user playlists
        path = f"users/{self.auth.user_id}/playlistsAndFavoritePlaylists"
        async for item in self.api.paginate(path):
            if item and item.get("playlist") and item["playlist"].get("uuid"):
                yield parse_playlist(self.provider, item)

        # 3. Get the virtual favorite tracks playlist
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
        except (ClientError, MediaNotFoundError, ResourceTemporarilyUnavailable):
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
        except (ClientError, MediaNotFoundError, ResourceTemporarilyUnavailable):
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
