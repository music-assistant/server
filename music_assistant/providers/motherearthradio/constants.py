"""Constants for Mother Earth Radio provider."""

from typing import TypedDict

from music_assistant_models.enums import ContentType

# Base URL for the AzuraCast API
AZURACAST_BASE_URL = "https://stream.motherearthradio.de"
NOWPLAYING_API_URL = f"{AZURACAST_BASE_URL}/api/nowplaying"

# Station icon URL
STATION_ICON_URL = "https://raw.githubusercontent.com/music-assistant/music-assistant.io/main/public/assets/icons/motherearthradio-icon.png"


class MerChannel(TypedDict):
    """Type definition for a Mother Earth Radio channel."""

    name: str
    description: str
    shortcode: str
    stream_url: str
    content_type: ContentType


# All Mother Earth Radio channels stream at the same format.
MER_SAMPLE_RATE = 192000
MER_BIT_DEPTH = 24


MER_CHANNELS: dict[str, MerChannel] = {
    "motherearth": {
        "name": "Mother Earth Radio",
        "description": "Eclectic audiophile mix — vinyl, hi-res, CD — hand-picked by a human",
        "shortcode": "motherearth",
        "stream_url": f"{AZURACAST_BASE_URL}/listen/motherearth/motherearth",
        "content_type": ContentType.FLAC,
    },
    "motherearth_instrumental": {
        "name": "Mother Earth Instrumental",
        "description": "Instrumental selections — jazz, electronic, acoustic, ambient",
        "shortcode": "motherearth_instrumental",
        "stream_url": f"{AZURACAST_BASE_URL}/listen/motherearth_instrumental/motherearth.instrumental",
        "content_type": ContentType.FLAC,
    },
    "motherearth_klassik": {
        "name": "Mother Earth Klassik",
        "description": "Classical music — from vinyl and hi-res sources",
        "shortcode": "motherearth_klassik",
        "stream_url": f"{AZURACAST_BASE_URL}/listen/motherearth_klassik/motherearth.klassik",
        "content_type": ContentType.FLAC,
    },
    "motherearth_jazz": {
        "name": "Mother Earth Jazz",
        "description": "Jazz, funk, soul — deep cuts and timeless classics",
        "shortcode": "motherearth_jazz",
        "stream_url": f"{AZURACAST_BASE_URL}/listen/motherearth_jazz/motherearth.jazz",
        "content_type": ContentType.FLAC,
    },
}
