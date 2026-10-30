"""Playlist management for the ListenBrainz provider."""

from __future__ import annotations

import logging
from copy import copy
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from music_assistant_models.enums import ExternalID, MediaType
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant_models.media_items import Playlist, Track

from music_assistant.controllers.music.helpers import provider_mappings_from_urls
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
    parse_playlist_last_modified,
    parse_playlist_source_patch,
    parse_track,
)
from music_assistant.providers.musicbrainz.provider import relation_urls

if TYPE_CHECKING:
    from music_assistant.providers.listenbrainz import ListenBrainzProvider
    from music_assistant.providers.musicbrainz.provider import MusicbrainzProvider

CACHE_CATEGORY_RESOLVED_TRACKS = 1
CACHE_EXPIRATION_SECONDS = 60 * 60 * 24 * 30


class ListenBrainzPlaylistManager:
    """Manage ListenBrainz playlists and resolve their tracks."""

    def __init__(self, provider: ListenBrainzProvider) -> None:
        """Initialize the playlist manager."""
        self.provider = provider
        self.api = provider.api

        self._playlists: tuple[Playlist, ...] = ()
        self._playlist_tracks: dict[str, tuple[Track, ...]] = {}
        self._resolved_tracks: dict[str, Track] = {}
        self._raw_tracks: dict[str, tuple[Track, ...]] = {}
        self._playlist_modified_at: dict[str, str | None] = {}

    @property
    def logger(self) -> logging.Logger:
        """Return the provider's active logger."""
        return self.provider.logger

    async def refresh(self) -> None:
        """
        Refresh the active ListenBrainz playlists.

        Only playlists whose ``last_modified_at`` has changed are re-fetched
        from the API. Tracks in unchanged playlists are still re-resolved
        (fast via the resolution cache) so matches discovered late — or after a
        cache expiry — are picked up without an extra API call.
        """
        playlists = await self.api.get_createdfor_playlists(self.provider.username)
        refreshed: list[Playlist] = []
        refreshed_tracks: dict[str, tuple[Track, ...]] = {}

        for item in playlists:
            if not self._is_playlist_enabled(item):
                continue
            playlist = parse_playlist(item, self.provider.instance_id, self.provider.domain)
            refreshed.append(playlist)

            last_modified = parse_playlist_last_modified(item)
            if last_modified is not None and self._is_playlist_unchanged(
                playlist.item_id, last_modified
            ):
                # Unchanged: reuse stored raw tracks, re-resolve (no API call)
                raw_tracks = self._raw_tracks.get(playlist.item_id, ())
            else:
                # Changed or new: fetch full playlist from API
                playlist_data = await self.api.get_playlist(playlist.item_id)
                raw_tracks = tuple(
                    parse_track(tr, self.provider.instance_id, self.provider.domain)
                    for tr in playlist_data["track"]
                )
                self._raw_tracks[playlist.item_id] = raw_tracks
                self._playlist_modified_at[playlist.item_id] = last_modified

            # Resolve (or re-resolve) tracks — cache hits for already-resolved,
            # fresh attempts for previously unresolved
            tracks: list[Track] = []
            for position, raw_track in enumerate(raw_tracks, 1):
                if resolved := await self._get_or_resolve_track(raw_track):
                    playlist_track = copy(resolved)
                    playlist_track.position = position
                    tracks.append(playlist_track)
            refreshed_tracks[playlist.item_id] = tuple(tracks)

        # Evict playlists that no longer appear in the created-for list
        active_ids = {p.item_id for p in refreshed}
        for mbid in list(self._raw_tracks):
            if mbid not in active_ids:
                del self._raw_tracks[mbid]
                self._playlist_modified_at.pop(mbid, None)

        self._playlists = tuple(refreshed)
        self._playlist_tracks = refreshed_tracks
        self.logger.debug(
            "Refreshed %d ListenBrainz playlists with %d playable tracks",
            len(refreshed),
            sum(len(tracks) for tracks in refreshed_tracks.values()),
        )

    async def browse(self) -> list[Playlist]:
        """Return the user's recommendation playlists."""
        return list(self._playlists)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Return a ListenBrainz playlist."""
        for playlist in self._playlists:
            if playlist.item_id == prov_playlist_id:
                return playlist
        raise MediaNotFoundError(f"ListenBrainz playlist {prov_playlist_id} is not available")

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Return resolved tracks from a ListenBrainz playlist."""
        if page > 0:
            return []
        return list(self._playlist_tracks.get(prov_playlist_id, ()))

    def _is_playlist_unchanged(self, playlist_mbid: str, last_modified: str) -> bool:
        """Return whether the playlist's modification time matches the last refresh."""
        return self._playlist_modified_at.get(playlist_mbid) == last_modified

    async def _get_or_resolve_track(self, track: Track) -> Track | None:
        """Return a cached, library, or streaming-provider match for a parsed track."""
        recording_mbid = track.get_external_id(ExternalID.MB_RECORDING)
        if recording_mbid is None:
            self.logger.debug(
                "Cannot resolve ListenBrainz track %s (no recording MBID)", track.name
            )
            return None
        self.logger.debug(
            "Resolving ListenBrainz track %s (%s)",
            track.name,
            recording_mbid,
        )
        if cached_track := self._resolved_tracks.get(recording_mbid):
            return cached_track

        cached_track = await self.provider.mass.cache.get(
            key=recording_mbid,
            category=CACHE_CATEGORY_RESOLVED_TRACKS,
            provider=self.provider.instance_id,
            base_class=Track,
        )
        if isinstance(cached_track, Track):
            self._resolved_tracks[recording_mbid] = cached_track
            return cached_track
        resolved_track = await self._resolve_track(track, recording_mbid)
        if resolved_track is not None:
            self._resolved_tracks[recording_mbid] = resolved_track
            await self.provider.mass.cache.set(
                key=recording_mbid,
                data=resolved_track.to_dict(),
                category=CACHE_CATEGORY_RESOLVED_TRACKS,
                provider=self.provider.instance_id,
                expiration=CACHE_EXPIRATION_SECONDS,
            )
            return resolved_track
        return None

    async def _resolve_track(self, track: Track, recording_mbid: str) -> Track | None:
        """Resolve a track from the library or via MusicBrainz when it is not cached."""
        if library_track := await self.provider.mass.music.tracks.get_library_item_by_external_id(
            recording_mbid, ExternalID.MB_RECORDING
        ):
            return library_track
        if provider_track := await self._resolve_track_via_musicbrainz(track, recording_mbid):
            return provider_track
        artist_name = track.artists[0].name if getattr(track, "artists", None) else ""
        query = f"{artist_name} {track.name}".strip()
        if query:
            try:
                results = await self.provider.mass.music.search(query, [MediaType.TRACK])
            except Exception:
                results = None
            tracks = getattr(results, "tracks", None) if results is not None else None
            if tracks:
                return cast("Track", tracks[0])
        return None

    async def _resolve_track_via_musicbrainz(
        self, track: Track, recording_mbid: str
    ) -> Track | None:
        """Add playable provider mappings from a MusicBrainz recording's streaming links."""
        musicbrainz = cast(
            "MusicbrainzProvider | None", self.provider.mass.get_provider("musicbrainz")
        )
        if musicbrainz is None:
            return None
        try:
            recording = await musicbrainz.get_recording_details(recording_mbid)
        except MusicAssistantError, aiohttp.ClientError, TimeoutError:
            return None
        mappings = await provider_mappings_from_urls(
            self.provider.mass,
            relation_urls(recording.relations),
            MediaType.TRACK,
            exclude_domains=set(),
        )
        if not mappings:
            return None
        track.provider_mappings = set(mappings)
        return track

    def _is_playlist_enabled(self, playlist_data: dict[str, Any]) -> bool:
        """Return whether the ListenBrainz playlist type is enabled."""
        source_patch = parse_playlist_source_patch(playlist_data)
        if source_patch is None:
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
