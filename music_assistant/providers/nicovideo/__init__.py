"""nicovideo support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.providers.nicovideo.provider import NicovideoMusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

# Supported features collected from all mixins
SUPPORTED_FEATURES = {
    # Artist mixin
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.LIBRARY_ARTISTS,
    # Playlist mixin
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_CREATE,
    # Explorer mixin
    ProviderFeature.SEARCH,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.SIMILAR_TRACKS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return NicovideoMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)
