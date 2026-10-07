"""Google Drive File System Provider constants."""

from typing import Final

# Google OAuth + Drive endpoints
OAUTH_AUTHORIZE_URL: Final[str] = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL: Final[str] = "https://oauth2.googleapis.com/token"
# read-only access to all files in the user's Drive
OAUTH_SCOPE: Final[str] = "https://www.googleapis.com/auth/drive.readonly"

# Google Drive marks folders with this mimeType
FOLDER_MIME_TYPE: Final[str] = "application/vnd.google-apps.folder"
