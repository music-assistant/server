"""Constants for the Pandora music provider."""

# API Endpoints
API_BASE = "https://www.pandora.com/api/v1"
LOGIN_ENDPOINT = f"{API_BASE}/auth/login"
STATIONS_ENDPOINT = f"{API_BASE}/station/getStations"
PLAYLIST_FRAGMENT_ENDPOINT = f"{API_BASE}/playlist/getFragment"

# Pandora Error Code Categories
# Authentication and authorization failures
AUTH_ERRORS = {12, 13, 1001, 1002, 1003}
# Missing stations, tracks, or other media
NOT_FOUND_ERRORS = {4, 5, 1006}
# Temporary service unavailability
UNAVAILABLE_ERRORS = {1, 9, 10, 34, 1000}

# Pandora API Error Code Descriptions
PANDORA_ERROR_CODES = {
    0: "Internal error",
    1: "Maintenance mode",
    2: "URL parameter missing method",
    3: "URL parameter missing auth_token",
    4: "URL parameter missing partner_id",
    5: "URL parameter missing user_id",
    6: "Secure protocol required",
    7: "Certificate required",
    8: "Parameter type mismatch",
    9: "Parameter missing",
    10: "Parameter value invalid",
    11: "API version not supported",
    12: "Invalid username",
    13: "Invalid password",
    14: "Listener not authorized",
    15: "Partner not authorized",
    1000: "Read only mode",
    1001: "Invalid auth token",
    1002: "Invalid partner login",
    1003: "Listener not authorized",
    1004: "Partner not authorized",
    1005: "Station limit reached",
    1006: "Station does not exist",
    1009: "Device not found",
    1010: "Partner not authorized",
    1011: "Invalid username",
    1012: "Invalid password",
    1023: "Device model invalid",
    1035: "Explicit pin incorrect",
    1036: "Explicit pin malformed",
    1037: "Device already associated to account",
    1039: "Device not found",
}
