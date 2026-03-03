"""Constants for the Yandex Music provider."""

from __future__ import annotations

from typing import Final

# Configuration Keys
CONF_TOKEN = "token"
CONF_QUALITY = "quality"
CONF_BASE_URL = "base_url"

# Actions
CONF_ACTION_AUTH = "auth"
CONF_ACTION_CLEAR_AUTH = "clear_auth"

# Labels
LABEL_TOKEN = "token_label"
LABEL_AUTH_INSTRUCTIONS = "auth_instructions_label"

# API defaults
DEFAULT_LIMIT: Final[int] = 50
DEFAULT_BASE_URL: Final[str] = "https://api.music.yandex.net"

# Quality options (matching reference implementation)
QUALITY_EFFICIENT = "efficient"  # Low quality, efficient bandwidth (~64kbps AAC)
QUALITY_BALANCED = "balanced"  # Medium quality, balanced performance (~192kbps AAC)
QUALITY_HIGH = "high"  # High quality, lossy (~320kbps MP3)
QUALITY_SUPERB = "superb"  # Highest quality, lossless (FLAC)

# Configuration keys for My Wave behavior (kept)
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

# Browse folder names by locale (item_id -> display name)
BROWSE_NAMES_RU: Final[dict[str, str]] = {
    "my_wave": "Моя волна",
    "artists": "Мои исполнители",
    "albums": "Мои альбомы",
    "tracks": "Мне нравится",
    "playlists": "Мои плейлисты",
    "feed": "Для вас",
    "chart": "Чарт",
    "new_releases": "Новинки",
    "new_playlists": "Новые плейлисты",
    # Picks & Mixes
    "picks": "Подборки",
    "mixes": "Миксы",
    "mood": "Настроение",
    "activity": "Активность",
    "era": "Эпоха",
    "genres": "Жанры",
    # Mood tags
    "chill": "Расслабляющее",
    "sad": "Грустное",
    "romantic": "Романтическое",
    "party": "Вечеринка",
    "relax": "Релакс",
    # Activity tags
    "workout": "Тренировка",
    "focus": "Концентрация",
    "morning": "Утро",
    "evening": "Вечер",
    "driving": "В дороге",  # noqa: RUF001
    # Era tags
    "80s": "80-е",  # noqa: RUF001
    "90s": "90-е",  # noqa: RUF001
    "2000s": "2000-е",  # noqa: RUF001
    "retro": "Ретро",
    # Genre tags
    "rock": "Рок",
    "jazz": "Джаз",
    "classical": "Классика",
    "electronic": "Электроника",
    "rnb": "R&B",
    "hiphop": "Хип-хоп",
    "top": "Топ",
    "newbies": "По жанру",
    # Landing-discovered tags
    "in the mood": "В настроение",  # noqa: RUF001
    "background": "Послушать фоном",
    # Seasonal tags
    "winter": "Зима",
    "summer": "Лето",
    "autumn": "Осень",
    "spring": "Весна",
    "newyear": "Новый год",
    # Liked Tracks
    "liked_tracks": "Мне нравится",
    # Discovery
    "top_picks": "Топ подборки",
    "mood_mix": "Настроение",
    "activity_mix": "Активность",
    "seasonal_mix": "Сезонное",
    # Top-level browse groups
    "for_you": "Для вас",
    "collection": "Коллекция",
    # Waves / Radio (rotor station categories)
    "waves": "Радио",
    "radio": "Радио",
    "my_waves": "Персональные",
    "my_waves_set": "AI Сеты",
    "waves_landing": "Избранные волны",
    "genre": "Жанры",
    "epoch": "Эпоха",
    "local": "Местное",
}
BROWSE_NAMES_EN: Final[dict[str, str]] = {
    "my_wave": "My Wave",
    "artists": "My Artists",
    "albums": "My Albums",
    "tracks": "My Favorites",
    "playlists": "My Playlists",
    "feed": "Made for You",
    "chart": "Chart",
    "new_releases": "New Releases",
    "new_playlists": "New Playlists",
    # Picks & Mixes
    "picks": "Picks",
    "mixes": "Mixes",
    "mood": "Mood",
    "activity": "Activity",
    "era": "Era",
    "genres": "Genres",
    # Mood tags
    "chill": "Chill",
    "sad": "Sad",
    "romantic": "Romantic",
    "party": "Party",
    "relax": "Relax",
    # Activity tags
    "workout": "Workout",
    "focus": "Focus",
    "morning": "Morning",
    "evening": "Evening",
    "driving": "Driving",
    # Era tags
    "80s": "80s",
    "90s": "90s",
    "2000s": "2000s",
    "retro": "Retro",
    # Genre tags
    "rock": "Rock",
    "jazz": "Jazz",
    "classical": "Classical",
    "electronic": "Electronic",
    "rnb": "R&B",
    "hiphop": "Hip-Hop",
    "top": "Top",
    "newbies": "By Genre",
    # Landing-discovered tags
    "in the mood": "In the Mood",
    "background": "Background",
    # Seasonal tags
    "winter": "Winter",
    "summer": "Summer",
    "autumn": "Autumn",
    "spring": "Spring",
    "newyear": "New Year",
    # Liked Tracks
    "liked_tracks": "My Favorites",
    # Discovery
    "top_picks": "Top Picks",
    "mood_mix": "Mood Mix",
    "activity_mix": "Activity Mix",
    "seasonal_mix": "Seasonal",
    # Top-level browse groups
    "for_you": "For You",
    "collection": "Collection",
    # Waves / Radio (rotor station categories)
    "waves": "Radio",
    "radio": "Radio",
    "my_waves": "Personal",
    "my_waves_set": "AI Wave Sets",
    "waves_landing": "Featured Waves",
    "genre": "Genres",
    "epoch": "Era",
    "local": "Local",
}

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

# Preferred display order for wave categories (rotor station types)
WAVE_CATEGORY_DISPLAY_ORDER: Final[list[str]] = [
    "genre",
    "mood",
    "activity",
    "epoch",
    "local",
]
