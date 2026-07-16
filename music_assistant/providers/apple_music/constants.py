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
}

MUSIC_APP_TOKEN = app_var("apple_music_token")
WIDEVINE_BASE_PATH = "/usr/local/bin/widevine_cdm"
DECRYPT_CLIENT_ID_FILENAME = "client_id.bin"
DECRYPT_PRIVATE_KEY_FILENAME = "private_key.pem"
UNKNOWN_PLAYLIST_NAME = "Unknown Apple Music Playlist"
CONF_MUSIC_APP_TOKEN = "music_app_token"
CONF_MUSIC_USER_TOKEN = "music_user_token"
CONF_MUSIC_USER_MANUAL_TOKEN = "music_user_manual_token"
CONF_MUSIC_USER_TOKEN_TIMESTAMP = "music_user_token_timestamp"
CACHE_CATEGORY_DECRYPT_KEY = 1
MAX_ARTWORK_DIMENSION = 1000
ARTWORK_CACHE_EXPIRATION = 12 * 60 * 60
ARTWORK_CACHE_CHECKSUM = "artwork_v2"
BLOBSTORE_DOMAIN = "blobstore.apple.com"
