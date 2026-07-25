"""Yandex Music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from .provider import YandexMusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
    ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT,
    ProviderFeature.BROWSE,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.SIMILAR_ARTISTS,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.LYRICS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YandexMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)
