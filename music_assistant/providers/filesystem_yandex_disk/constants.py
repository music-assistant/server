"""Yandex Disk filesystem provider constants."""

from typing import Final

# Config keys (mirrors the Google Drive provider: the user registers their own
# Yandex OAuth application and enters its credentials).
CONF_CLIENT_ID: Final[str] = "client_id"
CONF_CLIENT_SECRET: Final[str] = "client_secret"
CONF_REFRESH_TOKEN: Final[str] = "refresh_token"
CONF_ROOT_PATH: Final[str] = "root_path"
CONF_AUTH_STATUS_CONNECTED: Final[str] = "auth_status_connected"
CONF_AUTH_STATUS_NOT_CONNECTED: Final[str] = "auth_status_not_connected"

# Config action: run the official OAuth Device Flow.
CONF_ACTION_AUTH: Final[str] = "auth"

# Yandex Disk API root and default scan root (the REST API is path-addressed).
DISK_ROOT: Final[str] = "disk:/"

# Read-only Yandex Disk OAuth scope.
OAUTH_SCOPE: Final[str] = "cloud_api:disk.read"
