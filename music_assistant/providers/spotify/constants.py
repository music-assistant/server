"""Constants for the Spotify provider."""

from __future__ import annotations

# Configuration Keys
CONF_CLIENT_ID = "client_id"
CONF_ACTION_AUTH = "auth"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACTION_CLEAR_AUTH = "clear_auth"
CONF_SYNC_PODCAST_PROGRESS = "sync_podcast_progress"
CONF_SYNC_AUDIOBOOK_PROGRESS = "sync_audiobook_progress"

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
