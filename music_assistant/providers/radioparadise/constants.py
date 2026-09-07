"""Constants for Radio Paradise provider."""

from typing import TypedDict

import aiohttp
from music_assistant_models.enums import ContentType

# Base URL for station icons
STATION_ICONS_BASE_URL = (
    "https://raw.githubusercontent.com/music-assistant/music-assistant.io/main/public/assets/icons"
)

# API base URLs
API_BASE_URL = "https://api.radioparadise.com/api"
PLAY_API_URL = f"{API_BASE_URL}/play?bitrate=4&info=true&chan="
NOWPLAYING_API_URL = f"{API_BASE_URL}/now_playing?chan="
COVER_BASE_URL = "https://img.radioparadise.com"

# How often the player queue calls back into _update_stream_metadata while a stream is active.
STREAM_METADATA_UPDATE_INTERVAL = 10

# Per-request timeout for Radio Paradise API calls.
API_TIMEOUT = aiohttp.ClientTimeout(total=10)


class RadioParadiseChannel(TypedDict):
    """Type definition for a Radio Paradise channel."""

    name: str
    description: str
    stream_url: str
    content_type: ContentType
    station_icon: str


# Channels 0-4 stream FLAC; channel 5 (Serenity) is AAC because Radio Paradise
# doesn't publish a FLAC variant for it. The "-flacm" mounts carry the same FLAC
# audio WITH ICY stream titles; the plain "-flac" mounts are a single continuous
# Ogg stream whose metadata never updates after connect.
RADIO_PARADISE_CHANNELS: dict[str, RadioParadiseChannel] = {
    "0": {
        "name": "Radio Paradise - Main Mix",
        "description": "Eclectic mix of music - hand-picked by real humans",
        "stream_url": "https://stream.radioparadise.com/flacm",
        "content_type": ContentType.FLAC,
        "station_icon": "radioparadise-logo-main.png",
    },
    "1": {
        "name": "Radio Paradise - Mellow Mix",
        "description": "A mellower selection from the RP music library",
        "stream_url": "https://stream.radioparadise.com/mellow-flacm",
        "content_type": ContentType.FLAC,
        "station_icon": "radioparadise-logo-mellow.png",
    },
    "2": {
        "name": "Radio Paradise - Rock Mix",
        "description": "Heavier selections from the RP music library",
        "stream_url": "https://stream.radioparadise.com/rock-flacm",
        "content_type": ContentType.FLAC,
        "station_icon": "radioparadise-logo-rock.png",
    },
    "3": {
        "name": "Radio Paradise - Global",
        "description": "Global music and experimental selections",
        "stream_url": "https://stream.radioparadise.com/global-flacm",
        "content_type": ContentType.FLAC,
        "station_icon": "radioparadise-logo-global.png",
    },
    "4": {
        "name": "Radio Paradise - Beyond",
        "description": "Exploring the frontiers of improvisational music",
        "stream_url": "https://stream.radioparadise.com/beyond-flacm",
        "content_type": ContentType.FLAC,
        "station_icon": "radioparadise-logo-beyond.png",
    },
    "5": {
        "name": "Radio Paradise - Serenity",
        "description": "Don't panic, and don't forget your towel",
        "stream_url": "https://stream.radioparadise.com/serenity",
        "content_type": ContentType.AAC,
        "station_icon": "radioparadise-logo-serenity.png",
    },
}
