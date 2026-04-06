"""Constants for Mother Earth Radio provider."""

from typing import Any

from music_assistant_models.enums import ContentType

# Base URL for the AzuraCast API
AZURACAST_BASE_URL = "https://stream.motherearthradio.de"
NOWPLAYING_API_URL = f"{AZURACAST_BASE_URL}/api/nowplaying"


# Mother Earth Radio channel configurations
# Each station uses the FLAC 192 kHz stream as default (audiophile-first approach).
MER_CHANNELS: dict[str, dict[str, Any]] = {
    "motherearth": {
        "name": "Mother Earth Radio",
        "description": "Eclectic audiophile mix — vinyl, hi-res, CD — hand-picked by a human",
        "shortcode": "motherearth",
        "stream_url": f"{AZURACAST_BASE_URL}/listen/motherearth/motherearth",
        "content_type": ContentType.FLAC,
        "station_icon": "https://motherearthradio.de/wp-content/uploads/2025/12/mer-logo-cube-bold-1x-512.png",
    },
    "motherearth_instrumental": {
        "name": "Mother Earth Instrumental",
        "description": "Instrumental selections — jazz, electronic, acoustic, ambient",
        "shortcode": "motherearth_instrumental",
        "stream_url": f"{AZURACAST_BASE_URL}/listen/motherearth_instrumental/motherearth.instrumental",
        "content_type": ContentType.FLAC,
        "station_icon": "https://motherearthradio.de/wp-content/uploads/2025/12/mer-logo-cube-bold-1x-512.png",
    },
    "motherearth_klassik": {
        "name": "Mother Earth Klassik",
        "description": "Classical music — from vinyl and hi-res sources",
        "shortcode": "motherearth_klassik",
        "stream_url": f"{AZURACAST_BASE_URL}/listen/motherearth_klassik/motherearth.klassik",
        "content_type": ContentType.FLAC,
        "station_icon": "https://motherearthradio.de/wp-content/uploads/2025/12/mer-logo-cube-bold-1x-512.png",
    },
    "motherearth_jazz": {
        "name": "Mother Earth Jazz",
        "description": "Jazz, funk, soul — deep cuts and timeless classics",
        "shortcode": "motherearth_jazz",
        "stream_url": f"{AZURACAST_BASE_URL}/listen/motherearth_jazz/motherearth.jazz",
        "content_type": ContentType.FLAC,
        "station_icon": "https://motherearthradio.de/wp-content/uploads/2025/12/mer-logo-cube-bold-1x-512.png",
    },
}
