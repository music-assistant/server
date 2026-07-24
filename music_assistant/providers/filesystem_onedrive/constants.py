"""OneDrive File System Provider constants."""

from typing import Final

# Microsoft OAuth endpoints (consumer/personal accounts)
OAUTH_AUTHORIZE_URL: Final[str] = (
    "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
)
OAUTH_TOKEN_URL: Final[str] = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
# read access to all the user's files + a refresh token
OAUTH_SCOPE: Final[str] = "Files.Read.All offline_access"
# Microsoft Graph API base URL
GRAPH_BASE_URL: Final[str] = "https://graph.microsoft.com/v1.0"
