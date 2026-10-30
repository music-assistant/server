"""Playlist management for the ListenBrainz provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ExternalID
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant_models.media_items import Artist, Playlist, Track, UniqueList

from music_assistant.helpers.compare import compare_strings
from music_assistant.providers.listenbrainz.parsers import parse_playlist, parse_track

if TYPE_CHECKING:
    from music_assistant.providers.listenbrainz import ListenBrainzProvider

CACHE_CATEGORY_RESOLVED_TRACKS = 1
CACHE_EXPIRATION_SECONDS = 60 * 60 * 24 * 30


class ListenBrainzPlaylistManager:
    """Manage ListenBrainz playlists and resolve their tracks."""

    def __init__(self, provider: ListenBrainzProvider) -> None:
        """Initialize the playlist manager."""
        self.provider = provider
        self._playlists: tuple[Playlist, ...] = ()
        self._playlist_tracks: dict[str, tuple[Track, ...]] = {}
        self._resolved_tracks: dict[str, Track] = {}

    async def refresh(self) -> None:
        """Refresh the active ListenBrainz playlists."""
        playlists = await self.provider.api.get_recommendation_playlists(self.provider.username)
        refreshed = tuple(
            parse_playlist(item, self.provider.instance_id, self.provider.domain)
            for item in playlists
        )
        self._playlists = refreshed
        self._playlist_tracks = {playlist.item_id: () for playlist in refreshed}

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
        artist_cache: dict[str, Artist] | None = None,
    ) -> Track:
        """Parse and resolve a playlist track."""
        track = parse_track(item, self.provider.instance_id, self.provider.domain)
        resolved_track = await self._get_or_resolve_track(
            track, {} if artist_cache is None else artist_cache
        )
        resolved_track.position = position
        return resolved_track

    async def _get_or_resolve_track(self, track: Track, artist_cache: dict[str, Artist]) -> Track:
        """Return a cached, library, or streaming-provider match for a parsed track."""
        recording_mbid = track.get_external_id(ExternalID.MB_RECORDING)
        assert recording_mbid is not None
        if cached_track := self._resolved_tracks.get(recording_mbid):
            return cached_track
        if library_track := await self.provider.mass.music.tracks.get_library_item_by_external_id(
            recording_mbid, ExternalID.MB_RECORDING
        ):
            self._resolved_tracks[recording_mbid] = library_track
            return library_track
        cached_track = await self.provider.mass.cache.get(
            key=recording_mbid,
            category=CACHE_CATEGORY_RESOLVED_TRACKS,
            provider=self.provider.instance_id,
            base_class=Track,
        )
        if isinstance(cached_track, Track):
            self._resolved_tracks[recording_mbid] = cached_track
            return cached_track
        artist_name = track.artists[0].name if track.artists else None
        if provider_track := await self._search_track(track.name, artist_name):
            await self.provider.mass.cache.set(
                key=recording_mbid,
                data=provider_track.to_dict(),
                category=CACHE_CATEGORY_RESOLVED_TRACKS,
                provider=self.provider.instance_id,
                expiration=CACHE_EXPIRATION_SECONDS,
            )
            self._resolved_tracks[recording_mbid] = provider_track
            return provider_track
        resolved_artists: list[Artist] = []
        for artist in track.artists:
            assert isinstance(artist, Artist)
            resolved_artists.append(await self._resolve_artist(artist, artist_cache))
        track.artists = UniqueList(resolved_artists)
        return track

    async def _search_track(self, title: str, artist_name: str | None) -> Track | None:
        """Search streaming providers and return the first matching track."""
        search_query = f"{artist_name} - {title}" if artist_name else title
        for music_provider in self.provider.mass.music.providers:
            if not music_provider.available or not music_provider.is_streaming_provider:
                continue
            try:
                candidates = await self.provider.mass.music.tracks.search(
                    search_query, music_provider.instance_id, limit=10
                )
            except MusicAssistantError:
                continue
            for candidate in candidates:
                if not compare_strings(title, candidate.name, strict=False):
                    continue
                if (
                    artist_name
                    and candidate.artists
                    and not any(
                        compare_strings(artist_name, artist.name, strict=False)
                        for artist in candidate.artists
                    )
                ):
                    continue
                return candidate
        return None

    async def _resolve_artist(self, artist: Artist, artist_cache: dict[str, Artist]) -> Artist:
        """Return a playlist-cached library artist or the unavailable parsed artist."""
        mbid = artist.get_external_id(ExternalID.MB_ARTIST)
        assert mbid is not None
        if cached_artist := artist_cache.get(mbid):
            return cached_artist
        if library_artist := (
            await self.provider.mass.music.artists.get_library_item_by_external_id(
                mbid, ExternalID.MB_ARTIST
            )
        ):
            artist_cache[mbid] = library_artist
            return library_artist
        return artist
