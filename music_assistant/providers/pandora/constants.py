"""Constants for the Pandora provider."""

from __future__ import annotations

# Configuration Keys
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_AUDIO_QUALITY = "audio_quality"

# API Endpoints
API_BASE_URL = "https://www.pandora.com/api/v1"
LOGIN_ENDPOINT = f"{API_BASE_URL}/auth/login"
STATIONS_ENDPOINT = f"{API_BASE_URL}/station/getStations"
STATION_DETAILS_ENDPOINT = f"{API_BASE_URL}/station/getStationDetails"
PLAYLIST_FRAGMENT_ENDPOINT = f"{API_BASE_URL}/playlist/getFragment"
PROFILE_ENDPOINT = f"{API_BASE_URL}/listener/getProfile"
SEARCH_ENDPOINT = f"{API_BASE_URL}/search/search"
TRACK_FEEDBACK_ENDPOINT = f"{API_BASE_URL}/station/addFeedback"

# Request Headers
DEFAULT_HEADERS = {
    "Content-Type": "application/json;charset=utf-8",
    "User-Agent": "Music Assistant Pandora Provider/1.0",
}

# API Limits
MAX_SEARCH_RESULTS = 50
MAX_STATION_TRACKS = 100
DEFAULT_PAGE_SIZE = 50

# Audio Quality Settings
AUDIO_QUALITIES = {
    "low": {"bitrate": 64, "format": "AAC+"},  # Free tier
    "medium": {"bitrate": 128, "format": "MP3"},  # In-home devices
    "high": {"bitrate": 192, "format": "AAC+"},  # Premium web/desktop
}

DEFAULT_AUDIO_QUALITY = "high"  # Assume premium subscription

# Error Codes from Pandora API
PANDORA_ERROR_CODES = {
    0: "INVALID_REQUEST",
    1: "INVALID_PARTNER",
    2: "LISTENER_NOT_AUTHORIZED",
    3: "USER_NOT_AUTHORIZED",
    4: "STATION_DOES_NOT_EXIST",
    5: "TRACK_NOT_FOUND",
    9: "PANDORA_NOT_AVAILABLE",
    10: "SYSTEM_NOT_AVAILABLE",
    11: "CALL_NOT_ALLOWED",
    12: "INVALID_USERNAME",
    13: "INVALID_PASSWORD",
    14: "DEVICE_NOT_FOUND",
    15: "PARTNER_NOT_AUTHORIZED",
    1000: "READ_ONLY_MODE",
    1001: "INVALID_AUTH_TOKEN",
    1002: "INVALID_LOGIN",
    1003: "LISTENER_NOT_AUTHORIZED",
    1004: "USER_ALREADY_EXISTS",
    1005: "DEVICE_ALREADY_ASSOCIATED_TO_ACCOUNT",
    1006: "UPGRADE_DEVICE_MODEL_INVALID",
    1009: "DEVICE_MODEL_INVALID",
    1010: "INVALID_SPONSOR",
    1018: "EXPLICIT_PIN_INCORRECT",
    1020: "EXPLICIT_PIN_MALFORMED",
    1023: "DEVICE_DISABLED",
    1024: "DAILY_TRIAL_LIMIT_REACHED",
    1025: "INVALID_SPONSOR_USERNAME",
    1026: "SPONSOR_CANNOT_SKIP_ADS",
    1027: "INSUFFICIENT_CONNECTIVITY",
    1034: "GEOLOCATION_REQUIRED",
}
