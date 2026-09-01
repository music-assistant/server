"""Constants for the ABC Radio provider."""

from typing import TypedDict

import aiohttp

# Now-playing API, also used by ABC's own web players.
API_BASE_URL = "https://music.abcradio.net.au/api/v1"
NOW_PLAYING_URL = API_BASE_URL + "/plays/{station_id}/now.json"

# Live HLS streams as served by ABC's streaming CDN.
STREAM_BASE_URL = "https://streaming.abc-cdn.net.au/audio/hls"

# Station logos hosted on ABC's content CDN.
STATION_LOGO_BASE_URL = "https://live-production.wcms.abc-cdn.net.au"

# How often the player queue calls back into _update_stream_metadata while a stream is active.
STREAM_METADATA_UPDATE_INTERVAL = 30

# Per-request timeout for ABC Radio API calls.
API_TIMEOUT = aiohttp.ClientTimeout(total=10)


class ABCRadioStation(TypedDict):
    """Type definition for an ABC Radio station."""

    name: str
    description: str
    stream_url: str
    logo_url: str


# Station ids double as the service ids used by the ABC Radio now-playing API.
ABC_RADIO_STATIONS: dict[str, ABCRadioStation] = {
    "triplej": {
        "name": "triple j",
        "description": "New music and the best new and emerging artists",
        "stream_url": f"{STREAM_BASE_URL}/triplejnsw.m3u8",
        "logo_url": f"{STATION_LOGO_BASE_URL}/c170c3d8b69f13bf43865be27b027687",
    },
    "doublej": {
        "name": "Double J",
        "description": "Music news, interviews and new releases for grown-up music lovers",
        "stream_url": f"{STREAM_BASE_URL}/doublejnsw.m3u8",
        "logo_url": f"{STATION_LOGO_BASE_URL}/fe840de7a554fe27949bd9f6ee74c67d",
    },
    "unearthed": {
        "name": "triple j Unearthed",
        "description": "The best new and undiscovered Australian artists",
        "stream_url": f"{STREAM_BASE_URL}/triplejunearthed.m3u8",
        "logo_url": f"{STATION_LOGO_BASE_URL}/457cfcb2f56f45a89fe67c5e075ca91b",
    },
    "classic": {
        "name": "ABC Classic",
        "description": "Classical music to help you relax, focus and escape",
        "stream_url": f"{STREAM_BASE_URL}/classicnsw.m3u8",
        "logo_url": f"{STATION_LOGO_BASE_URL}/1613925515143520dff362b9f5bdf0cf",
    },
    "classic2": {
        "name": "ABC Classic 2",
        "description": "A deeper dive into the world of classical music",
        "stream_url": f"{STREAM_BASE_URL}/classic2.m3u8",
        "logo_url": f"{STATION_LOGO_BASE_URL}/1cf56a5051e18e63dbd3b43da5b9fb3e",
    },
    "jazz": {
        "name": "ABC Jazz",
        "description": "Jazz from Australia and around the world, 24/7",
        "stream_url": f"{STREAM_BASE_URL}/abcjazz.m3u8",
        "logo_url": f"{STATION_LOGO_BASE_URL}/1d3fcd8ee593c191730231c7981e23e2",
    },
    "country": {
        "name": "ABC Country",
        "description": "Country music from the classics to brand new releases",
        "stream_url": f"{STREAM_BASE_URL}/abccountry.m3u8",
        "logo_url": f"{STATION_LOGO_BASE_URL}/498b1d68a7fef49f03e8c8d476ea6583",
    },
    "kidslisten": {
        "name": "ABC Kids listen",
        "description": "Music and stories made for young children",
        "stream_url": f"{STREAM_BASE_URL}/abckids.m3u8",
        "logo_url": f"{STATION_LOGO_BASE_URL}/fca9b9528d2502f6ed38568557274e21",
    },
}
