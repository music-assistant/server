"""Constants for Radio Paradise provider."""

from typing import Any

from music_assistant_models.enums import ContentType

# Base URL for station icons
STATION_ICONS_BASE_URL = (
    "https://raw.githubusercontent.com/music-assistant/music-assistant.io/main/docs/assets/icons"
)

# Radio Paradise channel configurations with hardcoded channels
RADIO_PARADISE_CHANNELS: dict[str, dict[str, Any]] = {
    "0": {
        "name": "Radio Paradise - Main Mix",
        "description": "Eclectic mix of music - hand-picked by real humans",
        "stream_url": "https://stream.radioparadise.com/flac",
        "content_type": ContentType.FLAC,
        "api_url": "https://api.radioparadise.com/api/now_playing",
        "station_icon": "radioparadise-logo-main.png",
    },
    "1": {
        "name": "Radio Paradise - Mellow Mix",
        "description": "A mellower selection from the RP music library",
        "stream_url": "https://stream.radioparadise.com/mellow-flac",
        "content_type": ContentType.FLAC,
        "api_url": "https://api.radioparadise.com/api/now_playing?chan=1",
        "station_icon": "radioparadise-logo-mellow.png",
    },
    "2": {
        "name": "Radio Paradise - Rock Mix",
        "description": "Heavier selections from the RP music library",
        "stream_url": "https://stream.radioparadise.com/rock-flac",
        "content_type": ContentType.FLAC,
        "api_url": "https://api.radioparadise.com/api/now_playing?chan=2",
        "station_icon": "radioparadise-logo-rock.png",
    },
    "3": {
        "name": "Radio Paradise - Global",
        "description": "Global music and experimental selections",
        "stream_url": "https://stream.radioparadise.com/global-flac",
        "content_type": ContentType.FLAC,
        "api_url": "https://api.radioparadise.com/api/now_playing?chan=3",
        "station_icon": "radioparadise-logo-global.png",
    },
    "4": {
        "name": "Radio Paradise - Beyond",
        "description": "Exploring the frontiers of improvisational music",
        "stream_url": "https://stream.radioparadise.com/beyond-flac",
        "content_type": ContentType.FLAC,
        "api_url": "https://api.radioparadise.com/api/now_playing?chan=4",
        "station_icon": "radioparadise-logo-beyond.png",
    },
    "5": {
        "name": "Radio Paradise - Serenity",
        "description": "Don't panic, and don't forget your towel",
        "stream_url": "https://stream.radioparadise.com/serenity",
        "content_type": ContentType.AAC,
        "api_url": "https://api.radioparadise.com/api/now_playing?chan=5",
        "station_icon": "radioparadise-logo-serenity.png",
    },
}
