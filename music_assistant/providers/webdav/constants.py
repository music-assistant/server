"""WebDAV File System Provider constants."""

from typing import Final

# Only WebDAV-specific constants
CONF_URL: Final[str] = "url"
CONF_VERIFY_SSL: Final[str] = "verify_ssl"
CONF_CONTENT_TYPE: Final[str] = "content_type"
MAX_CONCURRENT_TASKS: Final[int] = 5
WEBDAV_TIMEOUT: Final[int] = 30
