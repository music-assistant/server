"""Native, read-only FeiNiu Music library integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from .provider import FeiNiuProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.SEARCH,
    ProviderFeature.LYRICS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> FeiNiuProvider:
    """Construct a provider with the verified read-only features."""
    return FeiNiuProvider(mass, manifest, config, SUPPORTED_FEATURES)
