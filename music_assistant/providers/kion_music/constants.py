"""Constants for the KION Music provider."""

from __future__ import annotations

from typing import Final

# Configuration Keys
CONF_TOKEN = "token"
CONF_QUALITY = "quality"
CONF_BASE_URL = "base_url"

# API defaults
DEFAULT_LIMIT: Final[int] = 50
DEFAULT_BASE_URL: Final[str] = "https://api.music.yandex.net"
WEB_BASE_URL: Final[str] = "https://music.yandex.ru"

# Quality options (matching reference implementation)
QUALITY_EFFICIENT = "efficient"  # Low quality, efficient bandwidth (~64kbps AAC)
QUALITY_BALANCED = "balanced"  # Medium quality, balanced performance (~192kbps AAC)
QUALITY_HIGH = "high"  # High quality, lossy (~320kbps MP3)
QUALITY_LOSSLESS = "superb"  # Highest quality, lossless (FLAC)

# Transport modes for get-file-info API
CONF_TRANSPORT = "transport"
TRANSPORT_RAW = "raw"  # Direct unencrypted stream (default)
TRANSPORT_ENCRAW = "encraw"  # AES-CTR encrypted stream

# Custom codecs override (empty = use quality-based default)
CONF_CODECS = "codecs"

# Quality → get-file-info parameter mapping
# Codecs order determines API priority (first codec = preferred by server)
QUALITY_FILE_INFO_PARAMS: Final[dict[str, dict[str, str]]] = {
    QUALITY_LOSSLESS: {
        "quality": "lossless",
        "codecs": "flac-mp4,flac,aac-mp4,aac,he-aac,mp3,he-aac-mp4",
    },
    QUALITY_HIGH: {
        "quality": "lossless",
        "codecs": "mp3",
    },
    QUALITY_BALANCED: {
        "quality": "nq",
        "codecs": "aac-mp4,aac,mp3,he-aac,he-aac-mp4",
    },
    QUALITY_EFFICIENT: {
        "quality": "lq",
        "codecs": "he-aac-mp4,he-aac,aac,mp3",
    },
}

# Configuration keys for My Mix behavior (kept)
CONF_MY_WAVE_MAX_TRACKS: Final[str] = "my_wave_max_tracks"

# Configuration keys for Liked Tracks behavior (kept)
CONF_LIKED_TRACKS_MAX_TRACKS: Final[str] = "liked_tracks_max_tracks"

# Hardcoded default values for removed config entries
MY_WAVE_BATCH_SIZE: Final[int] = 3
TRACK_BATCH_SIZE: Final[int] = 50
DISCOVERY_INITIAL_TRACKS: Final[int] = 20
BROWSE_INITIAL_TRACKS: Final[int] = 15

# Image sizes
IMAGE_SIZE_SMALL = "200x200"
IMAGE_SIZE_MEDIUM = "400x400"
IMAGE_SIZE_LARGE = "1000x1000"

# Locale-aware provider display names for owner normalization
PROVIDER_DISPLAY_NAME_RU: Final[str] = "KION Music"
PROVIDER_DISPLAY_NAME_EN: Final[str] = "KION Music"

# Known API-returned system owner name variants (all locales/capitalizations)
# All entries are lowercase; compare with owner_name.lower() for case-insensitive lookup
KION_SYSTEM_OWNER_NAMES: Final[frozenset[str]] = frozenset(
    {
        "кион музыка",
        "кион.музыка",
        "kion.music",
        "kionmusic",
        "kion music",
    }
)

# ID separators
PLAYLIST_ID_SPLITTER: Final[str] = ":"

# Rotor (radio) station identifiers
ROTOR_STATION_MY_MIX: Final[str] = "user:onyourwave"

# Virtual playlist ID for My Mix (used in get_playlist / get_playlist_tracks; not owner_id:kind)
MY_WAVE_PLAYLIST_ID: Final[str] = "my_wave"

# Virtual playlist ID for Liked Tracks
LIKED_TRACKS_PLAYLIST_ID: Final[str] = "liked_tracks"

# Composite item_id for My Mix tracks: track_id + separator + station_id (for rotor feedback)
RADIO_TRACK_ID_SEP: Final[str] = "@"

# Tag categories for Picks and Recommendations
# Used by _get_valid_tags_for_category to validate tags at runtime.
TAG_CATEGORY_MOOD: Final[list[str]] = [
    "chill",
    "sad",
    "romantic",
    "party",
    "relax",
    "in the mood",
]
TAG_CATEGORY_ACTIVITY: Final[list[str]] = [
    "workout",
    "focus",
    "morning",
    "evening",
    "driving",
    "background",
]
TAG_CATEGORY_ERA: Final[list[str]] = ["80s", "90s", "2000s", "retro"]
TAG_CATEGORY_GENRES: Final[list[str]] = [
    "rock",
    "jazz",
    "classical",
    "electronic",
    "rnb",
    "hiphop",
    "top",
    "newbies",
]

# Tag slug -> display category mapping
# Used to categorize dynamically discovered tags into browse folders.
# Tags not in this mapping default to "mood" category.
TAG_SLUG_CATEGORY: Final[dict[str, str]] = {
    # Mood
    "chill": "mood",
    "sad": "mood",
    "romantic": "mood",
    "party": "mood",
    "relax": "mood",
    "in the mood": "mood",
    # Activity
    "workout": "activity",
    "focus": "activity",
    "morning": "activity",
    "evening": "activity",
    "driving": "activity",
    "background": "activity",
    # Era
    "80s": "era",
    "90s": "era",
    "2000s": "era",
    "retro": "era",
    # Genres
    "rock": "genres",
    "jazz": "genres",
    "classical": "genres",
    "electronic": "genres",
    "rnb": "genres",
    "hiphop": "genres",
    "top": "genres",
    "newbies": "genres",
    # Seasonal (for mixes)
    "winter": "seasonal",
    "spring": "seasonal",
    "summer": "seasonal",
    "autumn": "seasonal",
    "newyear": "seasonal",
}

# Preferred tag order within categories (discovered tags sorted by this)
TAG_CATEGORY_ORDER: Final[dict[str, list[str]]] = {
    "mood": ["chill", "sad", "romantic", "party", "relax", "in the mood"],
    "activity": ["workout", "focus", "morning", "evening", "driving", "background"],
    "era": ["80s", "90s", "2000s", "retro"],
    "genres": ["rock", "jazz", "classical", "electronic", "rnb", "hiphop", "top", "newbies"],
}

# Seasonal tags mapped to months (month number -> tag)
TAG_SEASONAL_MAP: Final[dict[int, str]] = {
    1: "winter",  # January
    2: "winter",  # February
    3: "spring",  # March (validated at runtime; falls back to autumn if unavailable)
    4: "spring",  # April
    5: "spring",  # May
    6: "summer",  # June
    7: "summer",  # July
    8: "summer",  # August
    9: "autumn",  # September
    10: "autumn",  # October
    11: "autumn",  # November
    12: "winter",  # December
}

# Tags for Mixes (seasonal collections)
TAG_MIXES: Final[list[str]] = ["winter", "spring", "summer", "autumn", "newyear"]

# Waves by tag (rotor stations) — canonical ID is "waves", "radio" is an alias
WAVES_FOLDER_ID: Final[str] = "waves"
RADIO_FOLDER_ID: Final[str] = "radio"

# Personalized waves subfolder (rotor/stations/dashboard)
MY_WAVES_FOLDER_ID: Final[str] = "my_waves"

# AI Mix Sets subfolder (from /landing-blocks/mixes-waves)
MY_WAVES_SET_FOLDER_ID: Final[str] = "my_waves_set"

# Featured Mixes subfolder inside Radio (from /landing-blocks/waves)
WAVES_LANDING_FOLDER_ID: Final[str] = "waves_landing"

# Top-level browse group folders
FOR_YOU_FOLDER_ID: Final[str] = "for_you"
COLLECTION_FOLDER_ID: Final[str] = "collection"
PINNED_ITEMS_FOLDER_ID: Final[str] = "pinned"
LISTENING_HISTORY_FOLDER_ID: Final[str] = "history"

# Preferred display order for wave categories (rotor station types)
WAVE_CATEGORY_DISPLAY_ORDER: Final[list[str]] = [
    "genre",
    "mood",
    "activity",
    "epoch",
    "local",
]
