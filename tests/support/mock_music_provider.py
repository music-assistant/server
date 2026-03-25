"""Mock music provider for use in tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.media_items import (
    Album,
    Artist,
    Playlist,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

MOCK_PROVIDER_DOMAIN = "mock_music_provider"


class MockMusicProvider(MusicProvider):
    """Configurable music provider for test scenarios."""

    def __init__(
        self,
        mass: MusicAssistant,
        instance_id: str = "mock_music_provider_1",
        tracks: list[Track] | None = None,
        albums: list[Album] | None = None,
        artists: list[Artist] | None = None,
        playlists: list[Playlist] | None = None,
        fail_stream: bool = False,
    ) -> None:
        """Initialize the mock music provider.

        :param mass: MusicAssistant instance (or MagicMock).
        :param instance_id: Unique provider instance identifier.
        :param tracks: Tracks this provider will return from its library.
        :param albums: Albums this provider will return from its library.
        :param artists: Artists this provider will return from its library.
        :param playlists: Playlists this provider will return from its library.
        :param fail_stream: If True, get_stream_details always returns None.
        """
        self._tracks = tracks or []
        self._albums = albums or []
        self._artists = artists or []
        self._playlists = playlists or []
        self._fail_stream = fail_stream
        # Build minimal manifest and config mocks
        manifest = MagicMock()
        manifest.name = "Mock Music Provider"
        manifest.domain = MOCK_PROVIDER_DOMAIN
        manifest.mdns_discovery = []
        manifest.upnp_discovery = []
        config = MagicMock()
        config.instance_id = instance_id
        config.get_value = MagicMock(return_value="GLOBAL")
        super().__init__(mass, manifest, config)

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return supported features."""
        return {ProviderFeature.LIBRARY_TRACKS, ProviderFeature.SEARCH}

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 10
    ) -> SearchResults:
        """Search mock library — simple name substring match."""
        query = search_query.lower()
        tracks = (
            [t for t in self._tracks if query in t.name.lower()]
            if MediaType.TRACK in media_types
            else []
        )
        albums = (
            [a for a in self._albums if query in a.name.lower()]
            if MediaType.ALBUM in media_types
            else []
        )
        artists = (
            [a for a in self._artists if query in a.name.lower()]
            if MediaType.ARTIST in media_types
            else []
        )
        return SearchResults(tracks=tracks, albums=albums, artists=artists)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Yield configured tracks."""
        for track in self._tracks:
            yield track

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Yield configured albums."""
        for album in self._albums:
            yield album

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Yield configured artists."""
        for artist in self._artists:
            yield artist

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Yield configured playlists."""
        for playlist in self._playlists:
            yield playlist

    async def get_stream_details(  # type: ignore[override]
        self, item_id: str, media_type: MediaType = MediaType.TRACK
    ) -> StreamDetails | None:
        """Return stream details for a track, or None if not found or fail_stream=True."""
        if self._fail_stream:
            return None
        track = next((t for t in self._tracks if t.item_id == item_id), None)
        if track is None:
            return None
        return StreamDetails(
            provider=self.domain,
            item_id=item_id,
            audio_format=MagicMock(),
        )
