"""
nicovideo playlist mixin for Music Assistant.

In this section, "Mylist" on niconico is treated as a playlist.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, override

from music_assistant_models.enums import (
    ProviderFeature,
)
from music_assistant_models.errors import MediaNotFoundError

if TYPE_CHECKING:
    from music_assistant_models.media_items import Playlist, Track

from music_assistant.providers.nicovideo.provider_mixins.base import (
    NicovideoMusicProviderMixinBase,
)


class NicovideoMusicProviderPlaylistMixin(NicovideoMusicProviderMixinBase):
    """Mixin class for handling playlist-related operations in NicovideoMusicProvider."""

    _supported_features = {
        ProviderFeature.LIBRARY_PLAYLISTS,
        ProviderFeature.PLAYLIST_TRACKS_EDIT,
        ProviderFeature.PLAYLIST_CREATE,
    }

    @override
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        playlist_with_tracks = await self.service_hub.mylist.get_mylist(
            prov_playlist_id, page_size=500
        )
        if not playlist_with_tracks:
            playlist_with_tracks = await self.service_hub.mylist.get_own_mylist(
                prov_playlist_id, page_size=500
            )
        if not playlist_with_tracks:
            raise MediaNotFoundError(f"Playlist with id {prov_playlist_id} not found on nicovideo.")
        return playlist_with_tracks.playlist

    @override
    async def get_playlist_tracks(
        self,
        prov_playlist_id: str,
        page: int = 0,
    ) -> list[Track]:
        """Get all playlist tracks for given playlist id."""
        playlist_with_tracks = await self.service_hub.mylist.get_mylist(
            prov_playlist_id, page_size=500, page=page + 1
        )
        if not playlist_with_tracks:
            playlist_with_tracks = await self.service_hub.mylist.get_own_mylist(
                prov_playlist_id, page_size=500, page=page + 1
            )

        tracks = playlist_with_tracks.tracks if playlist_with_tracks else []

        # Ensure tracks have position set (1-based)
        for index, track in enumerate(tracks):
            track.position = index + 1

        return tracks

    @override
    async def get_library_playlists(
        self,
    ) -> AsyncGenerator[Playlist, None]:
        """Retrieve library playlists from the provider."""
        if not self.service_hub.auth.is_logged_in():
            return
        # Get user's own playlists (editable)
        own_playlists = await self.service_hub.mylist.get_own_mylists()
        for playlist in own_playlists:
            yield playlist

        # Include following mylists if enabled in config
        include_following = self.nicovideo_config.get_include_followed_mylists()
        if include_following:
            following_playlists = await self.service_hub.user.get_following_playlists()
            for playlist in following_playlists:
                yield playlist

    @override
    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        for track_id in prov_track_ids:
            success = await self.service_hub.mylist.add_mylist_item(prov_playlist_id, track_id)
            if success:
                self.logger.debug(
                    "Successfully added track %s to playlist %s",
                    track_id,
                    prov_playlist_id,
                )
            else:
                self.logger.warning(
                    "Failed to add track %s to playlist %s", track_id, prov_playlist_id
                )

    @override
    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        # Get current playlist tracks to find track IDs at the specified positions
        playlist_tracks = await self.get_playlist_tracks(prov_playlist_id)

        # Extract track IDs to remove based on positions
        # Note: positions_to_remove uses 1-based indexing, so convert to 0-based
        track_ids_to_remove = []
        for position in positions_to_remove:
            index = position - 1  # Convert from 1-based to 0-based indexing
            if 0 <= index < len(playlist_tracks):
                track_ids_to_remove.append(playlist_tracks[index].item_id)

        if not track_ids_to_remove:
            self.logger.warning(
                "No valid tracks found to remove from playlist %s", prov_playlist_id
            )
            return

        success = await self.service_hub.mylist.remove_mylist_items(
            prov_playlist_id, track_ids_to_remove
        )
        if success:
            self.logger.debug(
                "Successfully removed %d tracks from playlist %s",
                len(track_ids_to_remove),
                prov_playlist_id,
            )
        else:
            self.logger.warning("Failed to remove tracks from playlist %s", prov_playlist_id)

    @override
    async def create_playlist(self, name: str) -> Playlist:
        """Create a new playlist on provider with given name."""
        # Create a new mylist using niconico.py
        create_result = await self.service_hub.mylist.create_mylist(
            name, description="Created by Music Assistant", is_public=False
        )

        if not create_result or not hasattr(create_result, "mylist"):
            raise MediaNotFoundError(f"Failed to create playlist '{name}' on nicovideo.")

        # Get the created mylist details
        mylist_id = str(create_result.mylist.id_)
        playlist_with_tracks = await self.service_hub.mylist.get_own_mylist(mylist_id, page_size=1)

        if not playlist_with_tracks:
            raise MediaNotFoundError(
                f"Failed to retrieve created playlist '{name}' from nicovideo."
            )

        self.logger.info("Successfully created playlist '%s' with ID %s", name, mylist_id)
        return playlist_with_tracks.playlist
