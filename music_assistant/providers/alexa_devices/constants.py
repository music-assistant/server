"""Constants for the Alexa Devices player provider."""

from __future__ import annotations

from typing import Final

CONF_OTP_CODE: Final[str] = "otp_code"
# aioamazondevices session (tokens + cookies), stored as encrypted JSON string
CONF_LOGIN_DATA: Final[str] = "login_data"
CONF_API_URL: Final[str] = "api_url"
CONF_API_BASIC_AUTH_USERNAME: Final[str] = "api_username"
CONF_API_BASIC_AUTH_PASSWORD: Final[str] = "api_password"  # noqa: S105 - config key
CONF_ALEXA_LANGUAGE: Final[str] = "alexa_language"

DEFAULT_API_URL: Final[str] = "http://localhost:5000"
DEFAULT_LANGUAGE: Final[str] = "en-US"

# the capability the companion skill needs to stream on a device
CAPABILITY_MUSIC_SKILL: Final[str] = "MUSIC_SKILL"

# devices list/online state only changes rarely, push events cover media + volume
DEVICES_REFRESH_INTERVAL: Final[int] = 300

# voice command that opens the companion skill and makes it fetch the pushed stream url
ALEXA_LANGUAGE_COMMANDS: Final[dict[str, str]] = {
    "de-DE": "sag music assistant spiele audio",
    "en-CA": "ask music assistant to play audio",
    "en-US": "ask music assistant to play audio",
    "es-ES": "pídele a music assistant que reproduzca audio",
    "fr-CA": "music assistant",
    "fr-FR": "music assistant",
    "it-IT": "chiedi a music assistant di riprodurre audio",
    "pt-BR": "peça ao music assistant para reproduzir áudio",
    "nl-NL": "speel music assistant",
}
