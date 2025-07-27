"""
Niconico playlist mixin for Music Assistant.

In this section, "Mylist" on NicoNico is treated as a playlist.
"""

from collections.abc import AsyncGenerator

from music_assistant_models.enums import (
    ProviderFeature,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Playlist, Track

from music_assistant.providers.niconico.helpers import get_library_items
from music_assistant.providers.niconico.provider_mixins.mixin_base import (
    NiconicoMusicProviderMixinBase,
)


class NiconicoMusicProviderPlaylistMixin(NiconicoMusicProviderMixinBase):
    """Mixin class for handling playlist-related operations in NiconicoMusicProvider."""

    _supported_features = {
        ProviderFeature.LIBRARY_PLAYLISTS,
    }

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        playlist_with_tracks = await self.niconico_adapter.mylist.get_mylist(
            prov_playlist_id, page_size=500
        )
        if not playlist_with_tracks:
            playlist_with_tracks = await self.niconico_adapter.mylist.get_own_mylist(
                prov_playlist_id, page_size=500
            )
        if not playlist_with_tracks:
            raise MediaNotFoundError(f"Playlist with id {prov_playlist_id} not found on Niconico.")
        return playlist_with_tracks.playlist

    async def get_playlist_tracks(
        self,
        prov_playlist_id: str,
        page: int = 0,
    ) -> list[Track]:
        """Get all playlist tracks for given playlist id."""
        playlist_with_tracks = await self.niconico_adapter.mylist.get_mylist(
            prov_playlist_id, page_size=500, page=page + 1
        )
        if not playlist_with_tracks:
            playlist_with_tracks = await self.niconico_adapter.mylist.get_own_mylist(
                prov_playlist_id, page_size=500, page=page + 1
            )

        return playlist_with_tracks.tracks if playlist_with_tracks else []

    async def get_library_playlists(
        self,
    ) -> AsyncGenerator[Playlist, None]:
        """Retrieve library playlists from the provider."""
        playlists = await get_library_items(
            self.provider,
            cache_key="playlist",
            query_table="playlists",
            query_method=self.provider.mass.music.playlists.library_items,
        )
        for playlist in playlists:
            yield playlist

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        # Add track(s) to a playlist.
        # This is only called if the provider supports the PLAYLIST_TRACKS_EDIT feature.

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        # Remove track(s) from a playlist.
        # This is only called if the provider supports the PLAYLIST_TRACKS_EDIT feature.

    async def create_playlist(self, name: str) -> Playlist:  # type: ignore[empty-body]
        """Create a new playlist on provider with given name."""
        # Create a new playlist on the provider.
        # This is only called if the provider supports the PLAYLIST_CREATE feature.
