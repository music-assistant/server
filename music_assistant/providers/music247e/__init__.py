"""
Shared base for 24-7 (247e) music providers (Telmore Musik, YouSee Musik, ...).

Not a loadable provider itself (no manifest.json); see provider.py for the
Music247eProvider class that concrete 247e providers subclass. Concrete providers
supply their own auth manager, API client (GraphQL endpoint) and config key.
"""

from __future__ import annotations

from music_assistant_models.enums import ProviderFeature

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_CREATE,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.LYRICS,
}
