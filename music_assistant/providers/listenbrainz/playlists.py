"""Playlist management for the ListenBrainz provider."""

from __future__ import annotations

import logging
from copy import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from mashumaro.exceptions import MissingField
from music_assistant_models.enums import ExternalID, MediaType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant_models.media_items import Playlist, Track

from music_assistant.controllers.music.helpers import provider_mappings_from_urls
from music_assistant.helpers.compare import compare_strings
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
CACHE_CATEGORY_PLAYLISTS = 2
CACHE_EXPIRATION_SECONDS = 60 * 60 * 24 * 30
# Per-provider search limit (mirrors Last.fm's PROVIDER_SEARCH_LIMIT).
SEARCH_PROVIDER_LIMIT = 5


def _search_result_matches(searched_track: Track, result: Track, artist_name: str) -> bool:
    """
    Return True if a search result matches the searched track by name and artist.

    Mirrors the Last.fm recommendations provider's ``_is_matching_result``: a
    name match alone is not enough when the artist is known, because the same
    title often exists as a cover, karaoke, or translation by a different artist.
    """
    if not compare_strings(searched_track.name, result.name, strict=False):
        return False
    # When we searched with an artist name, require an artist match too.
    result_artists = getattr(result, "artists", None)
    if artist_name and result_artists:
        return any(
            compare_strings(artist_name, result_artist.name, strict=False)
            for result_artist in result_artists
        )
    return True


@dataclass(kw_only=True)
class PlaylistState:
    """Provider-owned playlist snapshot and source refresh state."""

    playlist: Playlist
    tracks: tuple[Track, ...]
    source_patch: str | None
    raw_tracks: tuple[Track, ...] | None = None
    modified_at: str | None = None


class ListenBrainzPlaylistManager:
    """Manage ListenBrainz playlists and resolve their tracks."""

    def __init__(self, provider: ListenBrainzProvider) -> None:
        """Initialize the playlist manager."""
        self.provider = provider
        self.api = provider.api

        self._playlists: dict[str, PlaylistState] = {}
        self._resolved_tracks: dict[str, Track] = {}

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
                raw_tracks = item.get("raw_tracks")
                playlists[playlist.item_id] = PlaylistState(
                    playlist=playlist,
                    tracks=tuple(Track.from_dict(track) for track in item["tracks"]),
                    source_patch=source_patch,
                    raw_tracks=(
                        tuple(Track.from_dict(track) for track in raw_tracks)
                        if raw_tracks is not None
                        else None
                    ),
                    modified_at=item.get("modified_at"),
                )
        except KeyError, TypeError, ValueError, MissingField:
            self.logger.warning("Ignoring invalid cached ListenBrainz playlists")
            return
        self._playlists = playlists

    async def refresh(self) -> None:
        """
        Refresh the active ListenBrainz playlists.

        Only playlists whose ``last_modified_at`` has changed are re-fetched
        from the API. Tracks in unchanged playlists are still re-resolved
        (fast via the resolution cache) so matches discovered late — or after a
        cache expiry — are picked up without an extra API call.
        """
        playlists = await self.api.get_createdfor_playlists(self.provider.username)
        refreshed: dict[str, PlaylistState] = {}

        for item in playlists:
            if not self._is_playlist_enabled(item):
                continue
            state = await self._refresh_playlist(item)
            refreshed[state.playlist.item_id] = state

        self._playlists = refreshed
        await self._store_cache()
        self.logger.debug(
            "Refreshed %d ListenBrainz playlists with %d playable tracks",
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
        """Return resolved tracks from a ListenBrainz playlist."""
        if page > 0:
            return []
        if state := self._playlists.get(prov_playlist_id):
            return list(state.tracks)
        return []

    async def _refresh_playlist(self, item: dict[str, Any]) -> PlaylistState:
        """Refresh and publish one playlist and its resolved tracks."""
        playlist = parse_playlist(item, self.provider.instance_id, self.provider.domain)
        last_modified = parse_playlist_last_modified(item)
        previous = self._playlists.get(playlist.item_id)
        if (
            previous is not None
            and previous.raw_tracks is not None
            and last_modified is not None
            and previous.modified_at == last_modified
        ):
            # Unchanged: reuse stored raw tracks, re-resolve (no API call)
            raw_tracks = previous.raw_tracks
        else:
            # Changed or new: fetch full playlist from API
            playlist_data = await self.api.get_playlist(playlist.item_id)
            raw_tracks = tuple(
                parse_track(tr, self.provider.instance_id, self.provider.domain)
                for tr in playlist_data["track"]
            )

        # Resolve (or re-resolve) tracks — cache hits for already-resolved,
        # fresh attempts for previously unresolved
        tracks: list[Track] = []
        for position, raw_track in enumerate(raw_tracks, 1):
            if resolved := await self._get_or_resolve_track(raw_track):
                playlist_track = copy(resolved)
                playlist_track.position = position
                tracks.append(playlist_track)

        state = PlaylistState(
            playlist=playlist,
            tracks=tuple(tracks),
            source_patch=parse_playlist_source_patch(item),
            raw_tracks=raw_tracks,
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
                    "raw_tracks": (
                        [track.to_dict() for track in state.raw_tracks]
                        if state.raw_tracks is not None
                        else None
                    ),
                    "modified_at": state.modified_at,
                }
                for state in self._playlists.values()
            ],
            category=CACHE_CATEGORY_PLAYLISTS,
            provider=self.provider.instance_id,
            checksum=self.provider.username,
            expiration=CACHE_EXPIRATION_SECONDS,
        )

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
            cached_track.add_external_id(ExternalID.MB_RECORDING, recording_mbid)
            self._resolved_tracks[recording_mbid] = cached_track
            return cached_track
        resolved_track = await self._resolve_track(track, recording_mbid)
        if resolved_track is not None:
            resolved_track.add_external_id(ExternalID.MB_RECORDING, recording_mbid)
            self._resolved_tracks[recording_mbid] = resolved_track
            await self.provider.mass.cache.set(
                key=recording_mbid,
                data=resolved_track.to_dict(),
                category=CACHE_CATEGORY_RESOLVED_TRACKS,
                provider=self.provider.instance_id,
                expiration=CACHE_EXPIRATION_SECONDS,
            )
            return resolved_track
        self.logger.debug(
            "Could not resolve ListenBrainz track %s (%s) — no library, MusicBrainz, or search match found",
            track.name,
            recording_mbid,
        )
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
        query = f"{artist_name} - {track.name}".strip(" -")
        if query:
            # Search streaming providers one by one (mirrors Last.fm's
            # _search_providers) so a hit on an earlier provider spares the
            # rest — mass.music.search() would fan out to every provider at
            # once, potentially hammering their APIs.
            for streaming_provider in self._streaming_track_providers():
                if not streaming_provider.available:
                    continue
                try:
                    tracks = await self.provider.mass.music.tracks.search(
                        query,
                        streaming_provider.instance_id,
                        limit=SEARCH_PROVIDER_LIMIT,
                    )
                except MusicAssistantError:
                    self.logger.debug(
                        "Provider %s search failed for %s (%s)",
                        streaming_provider.name,
                        track.name,
                        recording_mbid,
                    )
                    continue
                if not streaming_provider.available:
                    continue
                for candidate in tracks:
                    if _search_result_matches(track, candidate, artist_name):
                        return candidate
            self.logger.debug(
                "Search for '%s' returned no matching tracks for %s (%s)",
                query,
                track.name,
                recording_mbid,
            )
        else:
            self.logger.debug(
                "No searchable artist/track name for %s (%s)",
                track.name,
                recording_mbid,
            )
        return None

    def _streaming_track_providers(self) -> list[Any]:
        """
        Return streaming music providers that can search tracks, excluding self.

        Mirrors Last.fm's ``_get_streaming_providers``: only providers that
        advertise ``LIBRARY_TRACKS`` support are included, so each one can
        answer a track search request.
        """
        return [
            p
            for p in self.provider.mass.music.providers
            if p.instance_id != self.provider.instance_id
            and p.is_streaming_provider
            and ProviderFeature.LIBRARY_TRACKS in p.supported_features
        ]

    async def _resolve_track_via_musicbrainz(
        self, track: Track, recording_mbid: str
    ) -> Track | None:
        """Add playable provider mappings from a MusicBrainz recording's streaming links."""
        musicbrainz = cast(
            "MusicbrainzProvider | None", self.provider.mass.get_provider("musicbrainz")
        )
        if musicbrainz is None:
            self.logger.debug(
                "MusicBrainz provider not loaded — cannot resolve %s (%s) via relations",
                track.name,
                recording_mbid,
            )
            return None
        try:
            recording = await musicbrainz.get_recording_details(recording_mbid)
        except (MusicAssistantError, aiohttp.ClientError, TimeoutError) as err:
            self.logger.debug(
                "MusicBrainz get_recording_details failed for %s (%s): %s",
                track.name,
                recording_mbid,
                err,
            )
            return None
        recording_relation_urls = list(relation_urls(recording.relations))
        self.logger.debug(
            "MusicBrainz recording %s (%s) has %d relation URLs: %s",
            track.name,
            recording_mbid,
            len(recording_relation_urls),
            recording_relation_urls,
        )
        mappings = await provider_mappings_from_urls(
            self.provider.mass,
            recording_relation_urls,
            MediaType.TRACK,
            exclude_domains=set(),
        )
        if not mappings:
            self.logger.debug(
                "provider_mappings_from_urls returned no playable mappings for %s (%s)",
                track.name,
                recording_mbid,
            )
            return None
        # MusicBrainz links are authoritative. Fetch the actual playable track from
        # the first linked provider (no fallback) so the UI gets the provider's real
        # metadata and item_id rather than the ListenBrainz source placeholder.
        linked_mapping = mappings[0]
        try:
            provider_track = await self.provider.mass.music.tracks.get_provider_item(
                linked_mapping.item_id,
                linked_mapping.provider_instance,
                allow_fallback=False,
                strict_provider_instance=True,
            )
        except MediaNotFoundError:
            self.logger.debug(
                "%s no longer has the track MusicBrainz links to for %s (%s)",
                linked_mapping.provider_domain,
                track.name,
                recording_mbid,
            )
            return None
        except MusicAssistantError as err:
            self.logger.debug(
                "Linked %s on %s could not be fetched for %s (%s): %s",
                linked_mapping.provider_domain,
                linked_mapping.provider_instance,
                track.name,
                recording_mbid,
                err,
            )
            return None
        # Keep the streaming provider's track data and attach the MusicBrainz link
        # mappings so the MA core can trace the link back.
        provider_track.provider_mappings = set(mappings) | provider_track.provider_mappings
        return provider_track

    def _is_playlist_enabled(self, playlist_data: dict[str, Any]) -> bool:
        """Return whether the ListenBrainz playlist type is enabled."""
        return self._is_source_patch_enabled(parse_playlist_source_patch(playlist_data))

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
