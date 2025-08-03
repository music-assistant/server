"""Niconico provider mixins package."""

from __future__ import annotations

from .album_mixin import NiconicoMusicProviderAlbumMixin
from .artist_mixin import NiconicoMusicProviderArtistMixin
from .core_mixin import NiconicoMusicProviderCoreMixin
from .explorer_mixin import NiconicoMusicProviderExplorerMixin
from .playlist_mixin import NiconicoMusicProviderPlaylistMixin
from .track_mixin import NiconicoMusicProviderTrackMixin

__all__ = [
    "NiconicoMusicProviderAlbumMixin",
    "NiconicoMusicProviderArtistMixin",
    "NiconicoMusicProviderCoreMixin",
    "NiconicoMusicProviderExplorerMixin",
    "NiconicoMusicProviderPlaylistMixin",
    "NiconicoMusicProviderTrackMixin",
]
