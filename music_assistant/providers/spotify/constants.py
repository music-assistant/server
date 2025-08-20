"""Constants for the Spotify provider."""

from __future__ import annotations

from music_assistant_models.enums import ProviderFeature

# Configuration Keys
CONF_CLIENT_ID = "client_id"
CONF_ACTION_AUTH = "auth"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACTION_CLEAR_AUTH = "clear_auth"

# OAuth Settings
SCOPE = [
    "playlist-read",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-follow-modify",
    "user-follow-read",
    "user-library-read",
    "user-library-modify",
    "user-read-private",
    "user-read-email",
    "user-top-read",
    "app-remote-control",
    "streaming",
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-modify-private",
    "user-modify",
    "user-read-playback-position",
    "user-read-recently-played",
]

CALLBACK_REDIRECT_URL = "https://music-assistant.io/callback"

# Other Constants
LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX = "liked_songs"

# Base Features
SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
}
