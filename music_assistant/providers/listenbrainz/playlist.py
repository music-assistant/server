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

    @property
    def logger(self) -> logging.Logger:
        """Return the provider's active logger."""
        return self.provider.logger

    async def refresh(self) -> None:
        """Refresh the active ListenBrainz playlists."""
        playlists = await self.api.get_createdfor_playlists(self.provider.username)
        refreshed = tuple(
            parse_playlist(item, self.provider.instance_id, self.provider.domain)
            for item in playlists
            if self._is_playlist_enabled(item)
        )
        refreshed_tracks: dict[str, tuple[Track, ...]] = {}

        for playlist in refreshed:
            playlist_data = await self.api.get_playlist(playlist.item_id)
            tracks: list[Track] = []
            for position, item in enumerate(playlist_data["track"], 1):
                if track := await self._parse_track(item, position):
                    tracks.append(track)
            refreshed_tracks[playlist.item_id] = tuple(tracks)
        self._playlists = refreshed
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

    async def _parse_track(
        self,
        item: dict[str, Any],
        position: int,
    ) -> Track | None:
        """Parse and resolve a playlist track."""
        track = parse_track(item, self.provider.instance_id, self.provider.domain)
        if resolved_track := await self._get_or_resolve_track(track):
            playlist_track = copy(resolved_track)
            playlist_track.position = position
            return playlist_track
        return None

    async def _get_or_resolve_track(self, track: Track) -> Track | None:
        """Return a cached, library, or streaming-provider match for a parsed track."""
        recording_mbid = track.get_external_id(ExternalID.MB_RECORDING)
        assert recording_mbid is not None
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
