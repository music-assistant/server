"""Constants for Apple Music provider."""

from music_assistant_models.enums import ProviderFeature

from music_assistant.helpers.app_vars import app_var

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.SIMILAR_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.FAVORITE_ALBUMS_EDIT,
    ProviderFeature.FAVORITE_TRACKS_EDIT,
    ProviderFeature.FAVORITE_PLAYLISTS_EDIT,
    ProviderFeature.TRACK_BY_EXTERNAL_ID,
    ProviderFeature.ALBUM_BY_EXTERNAL_ID,
}

MUSIC_APP_TOKEN = app_var("apple_music_token")
WIDEVINE_BASE_PATH = "/usr/local/bin/widevine_cdm"
DECRYPT_CLIENT_ID_FILENAME = "client_id.bin"
DECRYPT_PRIVATE_KEY_FILENAME = "private_key.pem"
UNKNOWN_PLAYLIST_NAME = "Unknown Apple Music Playlist"
CONF_MUSIC_APP_TOKEN = "music_app_token"  # noqa: S105 - config key
CONF_MUSIC_USER_TOKEN = "music_user_token"  # noqa: S105 - config key
CONF_MUSIC_USER_MANUAL_TOKEN = "music_user_manual_token"  # noqa: S105 - config key
CONF_MUSIC_USER_TOKEN_TIMESTAMP = "music_user_token_timestamp"  # noqa: S105 - config key
CACHE_CATEGORY_DECRYPT_KEY = 1
# Apple rate limits per developer team and all installs share the bundled token,
# so the library syncs daily instead of every 12 hours
LIBRARY_SYNC_INTERVAL_HOURS = 24
# A full sync still runs this often to catch changes the sync marker misses, such as
# favourites set in the Apple Music app and an equal number of additions and removals.
LIBRARY_FULL_SYNC_INTERVAL = 7 * 24 * 3600
MAX_ARTWORK_DIMENSION = 1000
BLOBSTORE_DOMAIN = "blobstore.apple.com"
# blobstore artwork URLs are presigned with a ~24h expiry, so resolved artwork
# URLs may only be cached well below that lifetime
ARTWORK_CACHE_EXPIRATION = 12 * 60 * 60
# Bounds how often an opened playlist re-checks its change marker (matches the core listing cache)
PLAYLIST_LAST_MODIFIED_CACHE_EXPIRATION = 15 * 60
# Bump to re-parse cached api responses whenever the parsed representation changes
PARSED_ITEM_CACHE_CHECKSUM = "artist_fallback_v1"
