"""
nicovideo playlist mixin for Music Assistant.

In this section, "Mylist" on niconico is treated as a playlist.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import override

from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Playlist, Track  # noqa: TC002 - used in @use_cache

from music_assistant.controllers.cache import use_cache
from music_assistant.providers.nicovideo.provider_mixins.base import (
    NicovideoMusicProviderMixinBase,
)


class NicovideoMusicProviderPlaylistMixin(NicovideoMusicProviderMixinBase):
    """Mixin class for handling playlist-related operations in NicovideoMusicProvider."""

    @override
    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        playlist_with_tracks = await self.service_manager.mylist.get_mylist_or_own_mylist(
            prov_playlist_id, page_size=500
        )
        if not playlist_with_tracks:
            raise MediaNotFoundError(f"Playlist with id {prov_playlist_id} not found on nicovideo.")
        return playlist_with_tracks.playlist

    @override
    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_playlist_tracks(
        self,
        prov_playlist_id: str,
        page: int = 0,
    ) -> list[Track]:
        """Get all playlist tracks for given playlist id."""
        playlist_with_tracks = await self.service_manager.mylist.get_mylist_or_own_mylist(
            prov_playlist_id, page_size=500, page=page + 1
        )

        return playlist_with_tracks.tracks if playlist_with_tracks else []

    @override
    async def get_library_playlists(
        self,
    ) -> AsyncGenerator[Playlist, None]:
        """Retrieve library playlists from the provider."""
        # Get own mylists (editable playlists)
        own_mylists = await self.service_manager.mylist.get_own_mylists()
        for mylist in own_mylists:
            yield mylist
        # Following mylists are not included in simplified config
        return

    @override
    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        for track_id in prov_track_ids:
            success = await self.service_manager.mylist.add_mylist_item(prov_playlist_id, track_id)
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
        # Note: NicoNico's mylist does not allow duplicate entries of the same video_id
        # within a single playlist. Therefore, mapping from 1-based positions to
        # video_id is safe and uniquely identifies the target items.
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

        success = await self.service_manager.mylist.remove_mylist_items(
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
        create_result = await self.service_manager.mylist.create_mylist(
            name, description="Created by Music Assistant", is_public=False
        )

        if not create_result:
            raise MediaNotFoundError(f"Failed to create playlist '{name}' on nicovideo.")

        # Get the created mylist details
        mylist_id = str(create_result.mylist.id_)
        playlist_with_tracks = await self.service_manager.mylist.get_own_mylist(
            mylist_id, page_size=1
        )

        if not playlist_with_tracks:
            raise MediaNotFoundError(
                f"Failed to retrieve created playlist '{name}' from nicovideo."
            )

        self.logger.info("Successfully created playlist '%s' with ID %s", name, mylist_id)
        return playlist_with_tracks.playlist
