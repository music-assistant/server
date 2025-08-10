"""Constants for Spotify music provider."""

from typing import TypedDict

from music_assistant_models.enums import ProviderFeature

# Configuration constants
CONF_CLIENT_ID = "client_id"
CONF_ACTION_AUTH = "auth"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACTION_CLEAR_AUTH = "clear_auth"
CONF_ENABLE_PODCASTS = "enable_podcasts"
CONF_SYNC_PLAYED_STATUS = "sync_played_status"
CONF_PLAYED_THRESHOLD = "played_threshold"

# Cache categories - following pattern from other providers
CACHE_CATEGORY_PODCASTS = 0
CACHE_CATEGORY_EPISODES = 1
CACHE_CATEGORY_RECOMMENDATIONS = 2
CACHE_CATEGORY_OTHER = 3

# Cache keys
CACHE_KEY_PODCAST_PREFIX = "podcast_"
CACHE_KEY_EPISODES_PREFIX = "episodes_"
CACHE_KEY_USER_INFO = "user_info"

# Enhanced scope to include podcast permissions
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
LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX = "liked_songs"

# Consolidated supported features
BASE_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_CREATE,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
}

PODCAST_FEATURES = {
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
}


class LibrespotProfile(TypedDict):
    """Type definition for librespot profile configuration."""

    bitrate: str
    args: list[str]


# Librespot configuration profiles
LIBRESPOT_PROFILES: dict[str, LibrespotProfile] = {
    "track": {"bitrate": "320", "args": ["--disable-audio-cache", "--dither", "none"]},
    "episode_1": {"bitrate": "160", "args": ["--dither", "none"]},
    "episode_2": {"bitrate": "160", "args": ["--disable-audio-cache"]},
    "episode_3": {"bitrate": "96", "args": ["--disable-audio-cache", "--dither", "none"]},
}

# Media type to search type mapping
MEDIA_TYPE_TO_SEARCH = {
    "ARTIST": "artist",
    "ALBUM": "album",
    "TRACK": "track",
    "PLAYLIST": "playlist",
    "PODCAST": "show",
}

# Media type to library endpoint mapping
LIBRARY_ENDPOINTS = {
    "ARTIST": ("me/following", "type", "artist"),
    "ALBUM": ("me/albums", None, None),
    "TRACK": ("me/tracks", None, None),
    "PLAYLIST": ("playlists/{}/followers", None, None),
    "PODCAST": ("me/shows", None, None),
}
