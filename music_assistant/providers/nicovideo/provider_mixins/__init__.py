"""
nicovideo provider mixins package.

Provider Mixins Layer: Business logic
Implements Music Assistant provider interface methods.
Each mixin handles specific media types and provider capabilities.
"""

from __future__ import annotations

from .album import NicovideoMusicProviderAlbumMixin
from .artist import NicovideoMusicProviderArtistMixin
from .core import NicovideoMusicProviderCoreMixin
from .explorer import NicovideoMusicProviderExplorerMixin
from .playlist import NicovideoMusicProviderPlaylistMixin
from .track import NicovideoMusicProviderTrackMixin

# Defines the inheritance order for the NicovideoMusicProvider mixins.
NICOVIDEO_MIXINS = (
    NicovideoMusicProviderCoreMixin,
    NicovideoMusicProviderTrackMixin,
    NicovideoMusicProviderPlaylistMixin,
    NicovideoMusicProviderArtistMixin,
    NicovideoMusicProviderAlbumMixin,
    NicovideoMusicProviderExplorerMixin,
)

__all__ = [cls.__name__ for cls in NICOVIDEO_MIXINS]
