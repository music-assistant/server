"""nicovideo provider mixins package."""

from __future__ import annotations

from .album_mixin import NicovideoMusicProviderAlbumMixin
from .artist_mixin import NicovideoMusicProviderArtistMixin
from .core_mixin import NicovideoMusicProviderCoreMixin
from .explorer_mixin import NicovideoMusicProviderExplorerMixin
from .playlist_mixin import NicovideoMusicProviderPlaylistMixin
from .track_mixin import NicovideoMusicProviderTrackMixin

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
