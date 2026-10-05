"""Constants for the Yandex Music provider."""

from __future__ import annotations

from typing import Final

# Configuration Keys
CONF_TOKEN: Final[str] = "token"
CONF_MANUAL_TOKEN: Final[str] = "manual_token"
CONF_QUALITY: Final[str] = "quality"
CONF_BASE_URL: Final[str] = "base_url"

# Actions
CONF_ACTION_CLEAR_AUTH: Final[str] = "clear_auth"

# Stored authentication credentials
CONF_X_TOKEN: Final[str] = "x_token"
CONF_REFRESH_TOKEN: Final[str] = "refresh_token"
CONF_REMEMBER_SESSION: Final[str] = "remember_session"

# Advanced toggle: enable a token-wide concurrency cap to keep MA below
# Yandex's per-token edge concurrency limit on datacenter / VPN IPs
# (probed at ~6 simultaneous in-flight before captcha trips). Off by
# default — residential users tolerate much higher concurrency.
CONF_RESTRICTIVE_RATE_LIMITS: Final[str] = "restrictive_rate_limits"

# API defaults
DEFAULT_LIMIT: Final[int] = 50
DEFAULT_BASE_URL: Final[str] = "https://api.music.yandex.net"
WEB_BASE_URL: Final[str] = "https://music.yandex.ru"

# Quality options (matching reference implementation)
QUALITY_EFFICIENT: Final[str] = "efficient"  # Low quality, efficient bandwidth (~64kbps AAC)
QUALITY_BALANCED: Final[str] = "balanced"  # Medium quality, balanced performance (~192kbps AAC)
QUALITY_HIGH: Final[str] = "high"  # High quality, lossy (~320kbps MP3)
QUALITY_SUPERB: Final[str] = "superb"  # Highest quality, lossless (FLAC)

# Transport modes for get-file-info API
CONF_TRANSPORT: Final[str] = "transport"
TRANSPORT_RAW: Final[str] = "raw"  # Direct unencrypted stream (default)
TRANSPORT_ENCRAW: Final[str] = "encraw"  # AES-CTR encrypted stream

# Custom codecs override (empty = use quality-based default)
CONF_CODECS: Final[str] = "codecs"

# Quality → get-file-info parameter mapping
# Codecs order determines API priority (first codec = preferred by server)
QUALITY_FILE_INFO_PARAMS: Final[dict[str, dict[str, str]]] = {
    QUALITY_SUPERB: {
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

# Configuration keys for My Wave behavior (kept)
CONF_MY_WAVE_MAX_TRACKS: Final[str] = "my_wave_max_tracks"

# Configuration keys for Liked Tracks behavior (kept)
CONF_LIKED_TRACKS_MAX_TRACKS: Final[str] = "liked_tracks_max_tracks"

# Hardcoded default values for removed config entries
MY_WAVE_BATCH_SIZE: Final[int] = 3
TRACK_BATCH_SIZE: Final[int] = 100
DISCOVERY_INITIAL_TRACKS: Final[int] = 20
BROWSE_INITIAL_TRACKS: Final[int] = 15

# Rate-limit / smart-captcha handling.
# Yandex's smart-captcha edge protection is per-endpoint-family. When it
# triggers (HTML body with smart-captcha markers), the corresponding
# throttler "kind" is put in a quarantine for a duration picked from
# CAPTCHA_COOLDOWN_LADDER_S based on how many captcha strikes that kind has
# accumulated inside CAPTCHA_STRIKE_RETENTION_S. The first strike is cheap
# (60s) so a transient burst during initial library sync does not stall the
# provider for 10 minutes; repeated strikes escalate to the original 600s.
# Plain 429 (no captcha markers) only signals backoff_time on the failing
# request — no kind-wide block, no escalation.
# Ladder tightened after empirical probing against Yandex's edge layer
# showed a tripped token actually recovers in ~15s; the previous
# (60, 300, 600) ladder left the provider blocked far beyond Yandex's
# real cooldown memory.
CAPTCHA_COOLDOWN_LADDER_S: Final[tuple[float, ...]] = (15.0, 60.0, 120.0)
CAPTCHA_STRIKE_RETENTION_S: Final[float] = 3600.0
RATE_LIMIT_COOLDOWN_S: Final[float] = 60.0

# Per-kind request budgets (requests per second). Tuned by endpoint cost:
# - file_info is signed + most aggressively rate-limited at Yandex's edge
# - rotor sits in the middle
# - metadata covers the artist/album refresh burst MA fires during initial
#   sync — kept low so it does not flood smart-captcha
# - everything else (likes, tracks, search, playlists, ...) shares default
#
# Defaults bumped after empirical probing showed Yandex tolerates ≥10
# sustained sequential RPS on both residential and datacenter IPs — the
# previous 3/2 RPS caps were over-conservative without measurable
# anti-scraper benefit.
THROTTLE_DEFAULT_RPS: Final[int] = 5
THROTTLE_METADATA_RPS: Final[int] = 3
THROTTLE_FILE_INFO_RPS: Final[int] = 2
THROTTLE_ROTOR_RPS: Final[int] = 3

# Restrictive-mode global concurrency cap. When the
# ``restrictive_rate_limits`` provider setting is enabled, every API
# request runs through an additional ``asyncio.Semaphore(N)`` so the
# total in-flight count across all kinds and endpoints can never exceed
# this value. Sized one below Yandex's observed datacenter-IP captcha
# threshold (N=8 trips, N≤6 clean) to keep a safety margin.
RESTRICTIVE_GLOBAL_CONCURRENCY: Final[int] = 5

# Initial-sync jitter: during the first INITIAL_SYNC_WINDOW_S after a
# successful connect(), add up to INITIAL_SYNC_JITTER_S of uniform random
# delay before acquiring the default/metadata throttlers. Smooths out the
# parallel metadata-refresh burst MA fires immediately after a fresh
# install + auth, which is what triggers smart-captcha in #146. After the
# window expires the helper is a no-op — no steady-state overhead.
INITIAL_SYNC_JITTER_S: Final[float] = 0.5
INITIAL_SYNC_WINDOW_S: Final[float] = 60.0

# get-file-info LRU cache. Bounded TTL so we never serve a URL after its CDN
# expiry (Yandex stream URLs live ~60s) but still absorb same-track replays
# from MA's streaming retry loop.
FILE_INFO_CACHE_TTL_S: Final[float] = 30.0
FILE_INFO_CACHE_MAX: Final[int] = 256

# Inter-batch jitter when hydrating large lists (liked tracks/albums).
# Spreads requests so a 5-batch burst looks like a human, not a bot.
LIKED_BATCH_JITTER_MIN_S: Final[float] = 0.15
LIKED_BATCH_JITTER_SPAN_S: Final[float] = 0.20

# Image sizes
IMAGE_SIZE_MEDIUM: Final[str] = "400x400"
IMAGE_SIZE_LARGE: Final[str] = "1000x1000"

# Locale-aware provider display names for owner normalization
PROVIDER_DISPLAY_NAME_RU: Final[str] = "Яндекс Музыка"
PROVIDER_DISPLAY_NAME_EN: Final[str] = "Yandex Music"

# Known API-returned system owner name variants (all locales/capitalizations)
# All entries are lowercase; compare with owner_name.lower() for case-insensitive lookup
YANDEX_SYSTEM_OWNER_NAMES: Final[frozenset[str]] = frozenset(
    {
        "яндекс музыка",
        "яндекс.музыка",
        "yandex.music",
        "yandexmusic",
        "yandex music",
    }
)

# ID separators
PLAYLIST_ID_SPLITTER: Final[str] = ":"

# Rotor (radio) station identifiers
ROTOR_STATION_MY_WAVE: Final[str] = "user:onyourwave"

# Virtual playlist ID for My Wave (used in get_playlist / get_playlist_tracks; not owner_id:kind)
MY_WAVE_PLAYLIST_ID: Final[str] = "my_wave"

# Virtual playlist ID for Liked Tracks
LIKED_TRACKS_PLAYLIST_ID: Final[str] = "liked_tracks"

# Composite item_id for My Wave tracks: track_id + separator + station_id (for rotor feedback)
RADIO_TRACK_ID_SEP: Final[str] = "@"

# Wave-mode suffix separator: station keys like "user:onyourwave#discover" identify
# a specific preset (diversity/moodEnergy/language) on top of the base My Wave station.
# Chosen because # is not part of any rotor station ID format.
WAVE_MODE_SEP: Final[str] = "#"

# Known wave-mode presets: preset key (suffix after WAVE_MODE_SEP) → rotor session
# settings dict. Names match the LMS YandexMusic plugin and the Desktop client UI.
MY_WAVE_MODES_FOLDER_ID: Final[str] = "my_wave_modes"
MY_WAVE_PRESETS_FOLDER_ID: Final[str] = "my_wave_presets"

# User-defined wave presets are now stored in a single hidden JSON config key.
# The UI shows a small "builder" (name + three dropdowns) + Save / Delete
# action buttons, so the user never has to edit JSON by hand but has no fixed
# upper bound on preset count either.

# Hidden JSON store. Shape: [{"name": str, "diversity"?: str,
#                             "moodEnergy"?: str, "language"?: str}, ...]
CONF_WAVE_PRESETS_DATA: Final[str] = "wave_presets_data"

# Visible "working preset" fields — filled in, then copied into the JSON list
# by the save action and cleared afterwards.
CONF_WAVE_PRESET_DRAFT_NAME: Final[str] = "wave_preset_draft_name"
CONF_WAVE_PRESET_DRAFT_DIVERSITY: Final[str] = "wave_preset_draft_diversity"
CONF_WAVE_PRESET_DRAFT_MOOD: Final[str] = "wave_preset_draft_mood"
CONF_WAVE_PRESET_DRAFT_LANGUAGE: Final[str] = "wave_preset_draft_language"

# Dropdown of saved preset names for the delete flow.
CONF_WAVE_PRESET_TO_DELETE: Final[str] = "wave_preset_to_delete"

# Action button ids.
CONF_ACTION_SAVE_WAVE_PRESET: Final[str] = "save_wave_preset"
CONF_ACTION_DELETE_WAVE_PRESET: Final[str] = "delete_wave_preset"

# Allowed per-dimension values (plus "" to mean "use wave default").
WAVE_PRESET_DIVERSITY_VALUES: Final[tuple[str, ...]] = (
    "",
    "discover",
    "favorite",
    "popular",
)
WAVE_PRESET_MOOD_VALUES: Final[tuple[str, ...]] = (
    "",
    "active",
    "fun",
    "calm",
    "sad",
)
WAVE_PRESET_LANGUAGE_VALUES: Final[tuple[str, ...]] = (
    "",
    "russian",
    "not-russian",
    "without-words",
)

WAVE_MODE_PRESETS: Final[dict[str, dict[str, str]]] = {
    "discover": {"diversity": "discover"},
    "favorite": {"diversity": "favorite"},
    "popular": {"diversity": "popular"},
    "calm": {"moodEnergy": "calm"},
    "active": {"moodEnergy": "active"},
    "fun": {"moodEnergy": "fun"},
    "sad": {"moodEnergy": "sad"},
    "russian": {"language": "russian"},
    "not_russian": {"language": "not-russian"},
    "without_words": {"language": "without-words"},
}

# Ordered list of preset keys for Browse display.
WAVE_MODE_ORDER: Final[tuple[str, ...]] = (
    "discover",
    "favorite",
    "popular",
    "calm",
    "active",
    "fun",
    "sad",
    "russian",
    "not_russian",
    "without_words",
)

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

# AI Wave Sets subfolder (from /landing-blocks/mixes-waves)
MY_WAVES_SET_FOLDER_ID: Final[str] = "my_waves_set"

# Featured Waves subfolder inside Radio (from /landing-blocks/waves)
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
