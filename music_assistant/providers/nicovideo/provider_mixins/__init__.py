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

__all__ = [
    "NicovideoMusicProviderAlbumMixin",
    "NicovideoMusicProviderArtistMixin",
    "NicovideoMusicProviderCoreMixin",
    "NicovideoMusicProviderExplorerMixin",
    "NicovideoMusicProviderPlaylistMixin",
    "NicovideoMusicProviderTrackMixin",
]
