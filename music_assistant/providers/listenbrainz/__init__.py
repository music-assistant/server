"""Minimal ListenBrainz playlist provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ExternalID, ProviderFeature
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import Artist, Playlist, ProviderMapping, Track, UniqueList

from music_assistant.constants import CONF_USERNAME
from music_assistant.helpers.compare import compare_strings
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.listenbrainz.api_client import ListenBrainzAPIClient
from music_assistant.providers.listenbrainz.parsers import parse_track

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {ProviderFeature.BROWSE}
CACHE_CATEGORY_RESOLVED_TRACKS = 1
CACHE_EXPIRATION_SECONDS = 60 * 60 * 24 * 30


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the ListenBrainz provider."""
    return ListenBrainzProvider(mass, manifest, config, SUPPORTED_FEATURES)


class ListenBrainzProvider(PluginProvider):
    """Expose ListenBrainz recommendation playlists."""

    async def handle_async_init(self) -> None:
        """Initialize the ListenBrainz API client."""
        self.api = ListenBrainzAPIClient(self)
        self.username = str(self.config.get_value(CONF_USERNAME) or "")

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize the ListenBrainz provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._resolved_tracks: dict[str, Track] = {}

    async def browse(self, path: str) -> list[Playlist]:
        """
        Return the user's recommendation playlists.

        :param path: Provider browse path.
        """
        playlists = await self.api.get_recommendation_playlists(self.username)
        return [self._parse_playlist(item) for item in playlists]

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Return a ListenBrainz playlist."""
        playlist = await self.api.get_playlist(prov_playlist_id)
        return self._parse_playlist(playlist)

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Return metadata-only tracks from a ListenBrainz playlist."""
        if page > 0:
            return []
        playlist = await self.api.get_playlist(prov_playlist_id)
        tracks: list[Track] = []
        artist_cache: dict[str, Artist] = {}
        for position, item in enumerate(playlist["track"], 1):
            tracks.append(await self._parse_track(item, position, artist_cache))
        return tracks

    def _parse_playlist(self, playlist: dict[str, Any]) -> Playlist:
        """Parse a ListenBrainz JSPF playlist."""
        playlist_id = playlist["identifier"].rsplit("/", 1)[-1]
        return Playlist(
            item_id=playlist_id,
            provider=self.instance_id,
            name=playlist["title"],
            owner="listenbrainz",
            is_editable=False,
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    is_unique=True,
                )
            },
        )

    async def _parse_track(
        self,
        item: dict[str, Any],
        position: int,
        artist_cache: dict[str, Artist] | None = None,
    ) -> Track:
        """Parse and resolve a playlist track."""
        track = parse_track(item, self.instance_id, self.domain)
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
        if library_track := await self.mass.music.tracks.get_library_item_by_external_id(
            recording_mbid, ExternalID.MB_RECORDING
        ):
            self._resolved_tracks[recording_mbid] = library_track
            return library_track
        cached_track = await self.mass.cache.get(
            key=recording_mbid,
            category=CACHE_CATEGORY_RESOLVED_TRACKS,
            provider=self.instance_id,
            base_class=Track,
        )
        if isinstance(cached_track, Track):
            self._resolved_tracks[recording_mbid] = cached_track
            return cached_track
        artist_name = track.artists[0].name if track.artists else None
        if provider_track := await self._search_track(track.name, artist_name):
            await self.mass.cache.set(
                key=recording_mbid,
                data=provider_track.to_dict(),
                category=CACHE_CATEGORY_RESOLVED_TRACKS,
                provider=self.instance_id,
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
        for music_provider in self.mass.music.providers:
            if not music_provider.available or not music_provider.is_streaming_provider:
                continue
            try:
                candidates = await self.mass.music.tracks.search(
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
        if library_artist := await self.mass.music.artists.get_library_item_by_external_id(
            mbid, ExternalID.MB_ARTIST
        ):
            artist_cache[mbid] = library_artist
            return library_artist
        return artist
