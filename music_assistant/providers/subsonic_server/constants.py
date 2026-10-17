"""Constants for the Subsonic API plugin."""

from __future__ import annotations

from enum import IntEnum
from typing import Final

API_VERSION: Final = "1.16.1"
SERVER_TYPE: Final = "music-assistant"
REST_PREFIX: Final = "/rest/"
XML_NAMESPACE: Final = "http://subsonic.org/restapi"

CONF_JUKEBOX_PLAYER: Final = "jukebox_player"
CONF_JUKEBOX_PLAYER_NONE: Final = "__none__"
CONF_APP_PASSWORD_PREFIX: Final = "app_password_"  # noqa: S105 - config key
CONF_APP_PASSWORD_TRANSLATION_KEY: Final = "app_password"  # noqa: S105 - translation key
CONF_ACTION_REGENERATE_PASSWORDS: Final = "regenerate_passwords"
SETUP_KEY_SECRET: Final = "app_password_secret"  # noqa: S105 - setup data key


class ErrorCode(IntEnum):
    """Error codes defined by the Subsonic API."""

    GENERIC = 0
    MISSING_PARAMETER = 10
    CLIENT_TOO_OLD = 20
    SERVER_TOO_OLD = 30
    WRONG_CREDENTIALS = 40
    TOKEN_AUTH_NOT_SUPPORTED = 41
    AUTH_MECHANISM_NOT_SUPPORTED = 42
    CONFLICTING_AUTH = 43
    INVALID_API_KEY = 44
    NOT_AUTHORIZED = 50
    NOT_FOUND = 70
