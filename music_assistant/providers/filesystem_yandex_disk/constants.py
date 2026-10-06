"""Yandex Disk filesystem provider constants."""

from typing import Final

# Yandex Disk API root and default scan root (the REST API is path-addressed).
DISK_ROOT: Final[str] = "disk:/"
SUPPORTED_CONTENT_TYPES: Final[tuple[str, ...]] = ("music", "audiobooks", "podcasts")

# Yandex OAuth Device Flow endpoints and read-only Disk scope.
OAUTH_DEVICE_CODE_URL: Final[str] = "https://oauth.yandex.ru/device/code"
OAUTH_TOKEN_URL: Final[str] = "https://oauth.yandex.ru/token"
OAUTH_SCOPE: Final[str] = "cloud_api:disk.read"
