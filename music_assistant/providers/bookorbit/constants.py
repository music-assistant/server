"""Constants for the BookOrbit provider."""

from enum import StrEnum

from aiohttp.client import ClientTimeout

AIOHTTP_TIMEOUT = ClientTimeout(total=10 * 60, sock_connect=60)

CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"  # noqa: S105
CONF_API_TOKEN = "api_token"  # reserved; unused in v1  # noqa: S105
CONF_VERIFY_SSL = "verify_ssl"

AUDIO_FORMATS = frozenset({"m4b", "mp3", "m4a", "opus", "ogg", "flac"})

API_PREFIX = "/api/v1"


class BrowsePaths(StrEnum):
    """Browse path prefixes."""

    LIBRARIES = "lb"
    AUTHORS = "a"
    NARRATORS = "n"
    SERIES = "s"
    AUDIOBOOKS = "b"
