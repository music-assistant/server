"""Google Drive File System Provider constants."""

from typing import Final

# Config keys
CONF_CLIENT_ID: Final[str] = "client_id"
CONF_CLIENT_SECRET: Final[str] = "client_secret"
CONF_REFRESH_TOKEN: Final[str] = "refresh_token"
CONF_FOLDER_ID: Final[str] = "folder_id"

# Config actions
CONF_ACTION_AUTH: Final[str] = "auth"

# Google OAuth + Drive endpoints
OAUTH_AUTHORIZE_URL: Final[str] = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL: Final[str] = "https://oauth2.googleapis.com/token"
# fixed https redirect URI (registered in the user's Google OAuth client) that
# forwards to the local MA callback passed in the OAuth `state` param
CALLBACK_REDIRECT_URL: Final[str] = "https://music-assistant.io/callback"
# read-only access to all files in the user's Drive
OAUTH_SCOPE: Final[str] = "https://www.googleapis.com/auth/drive.readonly"

# Google Drive marks folders with this mimeType
FOLDER_MIME_TYPE: Final[str] = "application/vnd.google-apps.folder"
