"""Playlist management for the ListenBrainz provider."""

from __future__ import annotations

import logging
from copy import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mashumaro.exceptions import MissingField
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Playlist, Track

from music_assistant.providers.listenbrainz.constants import (
    CONF_ENABLE_WEEKLY_EXPLORATION,
    CONF_ENABLE_WEEKLY_JAMS,
    CONF_ENABLE_YEARLY_MISSED_TRACKS,
    CONF_ENABLE_YEARLY_TOP_DISCOVERIES,
    SOURCE_PATCH_WEEKLY_EXPLORATION,
    SOURCE_PATCH_WEEKLY_JAMS,
    SOURCE_PATCH_YEARLY_MISSED_TRACKS_PREFIX,
    SOURCE_PATCH_YEARLY_TOP_DISCOVERIES_PREFIX,
)
from music_assistant.providers.listenbrainz.parsers import (
    parse_playlist,
    parse_track,
)

if TYPE_CHECKING:
    from music_assistant.providers.listenbrainz import ListenBrainzProvider
    from music_assistant.providers.listenbrainz.models import ListenBrainzPlaylist

CACHE_CATEGORY_PLAYLISTS = 2
CACHE_EXPIRATION_SECONDS = 60 * 60 * 24 * 30


@dataclass(kw_only=True)
class PlaylistState:
    """Provider-owned playlist snapshot and source refresh state."""

    playlist: Playlist
    tracks: tuple[Track, ...]
    source_patch: str | None
    modified_at: str | None = None


class ListenBrainzPlaylistManager:
    """Manage ListenBrainz playlists with lazily resolved tracks."""

    def __init__(self, provider: ListenBrainzProvider) -> None:
        """Initialize the playlist manager."""
        self.provider = provider
        self.api = provider.api

        self._playlists: dict[str, PlaylistState] = {}

    @property
    def logger(self) -> logging.Logger:
        """Return the provider's active logger."""
        return self.provider.logger

    async def load_cache(self) -> None:
        """Restore the published playlists without fetching or resolving tracks."""
        cached = await self.provider.mass.cache.get(
            key="playlists",
            category=CACHE_CATEGORY_PLAYLISTS,
            provider=self.provider.instance_id,
            checksum=self.provider.username,
        )
        if not isinstance(cached, list):
            return
        try:
            playlists: dict[str, PlaylistState] = {}
            for item in cached:
                source_patch = item.get("source_patch")
                if not self._is_source_patch_enabled(source_patch):
                    continue
                playlist = Playlist.from_dict(item["playlist"])
                playlists[playlist.item_id] = PlaylistState(
                    playlist=playlist,
                    tracks=tuple(Track.from_dict(track) for track in item["tracks"]),
                    source_patch=source_patch,
                    modified_at=item.get("modified_at"),
                )
        except KeyError, TypeError, ValueError, MissingField:
            self.logger.warning("Ignoring invalid cached ListenBrainz playlists")
            return
        self._playlists = playlists

    async def refresh(self) -> None:
        """
        Refresh the active ListenBrainz playlists.

        Only playlists whose ``last_modified_at`` has changed are re-fetched.
        Track resolution is left to the core when a track is opened or played.
        """
        playlists = await self.api.get_createdfor_playlists(self.provider.username)
        refreshed: dict[str, PlaylistState] = {}

        for item in playlists:
            if not self._is_source_patch_enabled(item.source_patch):
                continue
            state = await self._refresh_playlist(item)
            refreshed[state.playlist.item_id] = state

        self._playlists = refreshed
        await self._store_cache()
        self.logger.debug(
            "Refreshed %d ListenBrainz playlists with %d tracks",
            len(refreshed),
            sum(len(state.tracks) for state in refreshed.values()),
        )

    async def browse(self) -> list[Playlist]:
        """Return the user's recommendation playlists."""
        return [state.playlist for state in self._playlists.values()]

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Return a ListenBrainz playlist."""
        if state := self._playlists.get(prov_playlist_id):
            return state.playlist
        raise MediaNotFoundError(f"ListenBrainz playlist {prov_playlist_id} is not available")

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Return all source tracks, retaining their metadata for URI-only play requests."""
        if page > 0:
            return []
        if state := self._playlists.get(prov_playlist_id):
            for track in state.tracks:
                listed_track = copy(track)
                listed_track.position = None
                await self.provider.mass.music.tracks.store_listed_external_item(listed_track)
            return list(state.tracks)
        return []

    async def _refresh_playlist(self, item: ListenBrainzPlaylist) -> PlaylistState:
        """Refresh and publish one playlist with all its source entries."""
        playlist = parse_playlist(item, self.provider.instance_id, self.provider.domain)
        last_modified = item.modified_at
        previous = self._playlists.get(playlist.item_id)
        if (
            previous is not None
            and last_modified is not None
            and previous.modified_at == last_modified
        ):
            tracks = previous.tracks
        else:
            playlist_data = await self.api.get_playlist(playlist.item_id)
            if playlist_data.tracks is None:
                raise ValueError("ListenBrainz playlist detail response has no track listing")
            parsed_tracks: list[Track] = []
            for position, track_data in enumerate(playlist_data.tracks, 1):
                track = parse_track(track_data, self.provider.instance_id, self.provider.domain)
                track.position = position
                parsed_tracks.append(track)
            tracks = tuple(parsed_tracks)

        state = PlaylistState(
            playlist=playlist,
            tracks=tracks,
            source_patch=item.source_patch,
            modified_at=last_modified,
        )

        # Publish each complete playlist without waiting for the remaining playlists.
        self._playlists[playlist.item_id] = state
        return state

    async def _store_cache(self) -> None:
        """Persist the complete state of the currently published playlists."""
        await self.provider.mass.cache.set(
            key="playlists",
            data=[
                {
                    "playlist": state.playlist.to_dict(),
                    "source_patch": state.source_patch,
                    "tracks": [track.to_dict() for track in state.tracks],
                    "modified_at": state.modified_at,
                }
                for state in self._playlists.values()
            ],
            category=CACHE_CATEGORY_PLAYLISTS,
            provider=self.provider.instance_id,
            checksum=self.provider.username,
            expiration=CACHE_EXPIRATION_SECONDS,
        )

    def _is_source_patch_enabled(self, source_patch: str | None) -> bool:
        """Return whether the ListenBrainz algorithm source patch is enabled."""
        if not isinstance(source_patch, str):
            return False
        config = {
            SOURCE_PATCH_WEEKLY_JAMS: (CONF_ENABLE_WEEKLY_JAMS, True),
            SOURCE_PATCH_WEEKLY_EXPLORATION: (CONF_ENABLE_WEEKLY_EXPLORATION, True),
        }.get(source_patch)
        if source_patch.startswith(SOURCE_PATCH_YEARLY_MISSED_TRACKS_PREFIX):
            config = (CONF_ENABLE_YEARLY_MISSED_TRACKS, False)
        elif source_patch.startswith(SOURCE_PATCH_YEARLY_TOP_DISCOVERIES_PREFIX):
            config = (CONF_ENABLE_YEARLY_TOP_DISCOVERIES, False)
        if config is None:
            return False
        key, default = config
        return bool(self.provider.config.get_value(key, default))
