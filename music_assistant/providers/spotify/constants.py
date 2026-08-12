"""Constants for the Spotify provider."""

from __future__ import annotations

# Configuration Keys
CONF_CLIENT_ID = "client_id"
CONF_ACTION_AUTH = "auth"
CONF_ACTION_AUTH_DEV = "auth_dev"
CONF_REFRESH_TOKEN_DEPRECATED = "refresh_token"  # Legacy key for migration, will be removed
CONF_REFRESH_TOKEN_GLOBAL = "refresh_token_global"  # Token authenticated with MA's client ID
CONF_REFRESH_TOKEN_DEV = "refresh_token_dev"  # Token authenticated with user's custom client ID
CONF_ACTION_CLEAR_AUTH = "clear_auth"
CONF_ACTION_CLEAR_AUTH_DEV = "clear_auth_dev"
CONF_SYNC_PODCAST_PROGRESS = "sync_podcast_progress"
CONF_SYNC_AUDIOBOOK_PROGRESS = "sync_audiobook_progress"
CONF_LIBRESPOT_CREDENTIALS = "librespot_credentials"  # librespot's reusable stored credential
CONF_ACTION_AUTH_PLAYBACK = "auth_playback"
CONF_ACTION_AUTH_PLAYBACK_BROWSER = "auth_playback_browser"
CONF_ACTION_AUTH_PLAYBACK_SUBMIT = "auth_playback_submit"
CONF_ACTION_CLEAR_AUTH_PLAYBACK = "clear_auth_playback"
CONF_PLAYBACK_CALLBACK_URL = "playback_callback_url"

# Librespot playback authorization
#
# Spotify's login5 endpoint only accepts a stored credential that was minted with the same
# client id librespot presents, which is always Spotify's own "keymaster" id. A credential
# minted with MA's (or a user's) client id is rejected, so the playback credential has to be
# obtained separately from the Web API tokens.
KEYMASTER_CLIENT_ID = "65b708073fc0480ea92a077233ca87bd"
LIBRESPOT_SCOPE = ["streaming"]
# keymaster accepts loopback redirect URIs only, so the browser flow cannot use MA's hosted
# callback. Music Assistant serves the loopback target itself, which works when the browser
# runs on the same host; otherwise the browser lands on a dead URL that the user pastes back.
LIBRESPOT_REDIRECT_PORT = 5588
LIBRESPOT_REDIRECT_PATH = "/login"
LIBRESPOT_REDIRECT_URI = f"http://127.0.0.1:{LIBRESPOT_REDIRECT_PORT}{LIBRESPOT_REDIRECT_PATH}"
LOOPBACK_WAIT_TIMEOUT = 30  # seconds before offering the manual paste instead
# name advertised to the Spotify app while pairing; also shown in the instructions.
# Must differ from the Spotify Connect player's own "Music Assistant" name, or picking the
# existing player in the Spotify app leaves the playback credential empty.
PAIRING_DEVICE_NAME = "Music Assistant Pairing"
PAIRING_TIMEOUT = 120  # seconds the pairing action waits for the user to pick the device
CHECK_AUTH_TIMEOUT = 30  # seconds
CREDENTIALS_FILE = "credentials.json"
PLAYBACK_AUTH_REQUIRED_ERROR = (
    "Spotify playback authorization required: open the Spotify settings in Music Assistant "
    "and authorize playback."
)

# OAuth Settings
SCOPE = [
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
    "user-read-playback-position",
    "user-read-recently-played",
]

CALLBACK_REDIRECT_URL = "https://music-assistant.io/callback"

# Other Constants
LIKED_SONGS_FAKE_PLAYLIST_ID_PREFIX = "liked_songs"
