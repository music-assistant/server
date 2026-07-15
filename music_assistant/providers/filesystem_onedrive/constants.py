"""OneDrive File System Provider constants."""

from typing import Final

# Config keys
CONF_CLIENT_ID: Final[str] = "client_id"
CONF_CLIENT_SECRET: Final[str] = "client_secret"
CONF_REFRESH_TOKEN: Final[str] = "refresh_token"
CONF_FOLDER_ID: Final[str] = "folder_id"
# Config actions
CONF_ACTION_AUTH: Final[str] = "auth"
# Microsoft OAuth endpoints (consumer/personal accounts)
OAUTH_AUTHORIZE_URL: Final[str] = (
    "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
)
OAUTH_TOKEN_URL: Final[str] = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
# fixed https redirect URI (registered in the user's Azure app) that forwards
# to the local MA callback passed in the OAuth `state` param
CALLBACK_REDIRECT_URL: Final[str] = "https://music-assistant.io/callback"
# read access to all the user's files + a refresh token
OAUTH_SCOPE: Final[str] = "Files.Read.All offline_access"
# Microsoft Graph API base URL
GRAPH_BASE_URL: Final[str] = "https://graph.microsoft.com/v1.0"
