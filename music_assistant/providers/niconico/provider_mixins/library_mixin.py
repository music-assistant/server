"""MixIn for NiconicoMusicProvider: library retrieval and edit methods."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from music_assistant.providers.niconico.provider_mixins.mixin_base import (
    NiconicoMusicProviderMixinBase,
)

if TYPE_CHECKING:
    from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Album, Artist, MediaItemType, Track

from music_assistant.providers.niconico.helpers import get_library_items


class NiconicoMusicProviderLibraryMixin(NiconicoMusicProviderMixinBase):
    """Library retrieval and edit methods for NiconicoMusicProvider (excluding playlist)."""

    _supported_features = {
        ProviderFeature.LIBRARY_TRACKS,
        ProviderFeature.LIBRARY_PLAYLISTS,
        ProviderFeature.LIBRARY_ARTISTS,
    }

    async def get_library_artists(
        self,
    ) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from the provider."""
        tracks = await get_library_items(
            self.provider,
            cache_key="track",
            query_table="tracks",
            query_method=self.provider.mass.music.tracks.library_items,
        )
        for track in tracks:
            for artist in track.artists:
                if isinstance(artist, Artist):
                    yield artist

    async def get_library_albums(
        self,
    ) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        yield  # type: ignore[misc]

    async def get_library_tracks(
        self,
    ) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        if not self.niconico_adapter.auth.is_logged_in():
            return
        playlists = await get_library_items(
            self.provider,
            cache_key="playlist",
            query_table="playlists",
            query_method=self.provider.mass.music.playlists.library_items,
        )
        for playlist in playlists:
            page = 0
            prov_map = next(iter(playlist.provider_mappings), None)
            if not prov_map:
                continue
            while True:
                playlist_tracks = await self.provider.get_playlist_tracks(prov_map.item_id, page)
                if not playlist_tracks:
                    break
                for track in playlist_tracks:
                    yield track
                page += 1

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to provider's library. Return true on success."""
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on success."""
        return True
