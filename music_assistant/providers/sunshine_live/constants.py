"""Constants for the Sunshine Live provider."""

from typing import Final

import aiohttp

# The public list of channels behind the player on the website of the station.
CHANNELS_URL: Final[str] = "https://www.sunshine-live.de/streams/api"
# The list also holds what each channel plays, so it can't stay in the cache for long.
CHANNELS_CACHE_SECONDS: Final[int] = 60
STREAM_METADATA_UPDATE_INTERVAL: Final[int] = 30
API_TIMEOUT: Final[aiohttp.ClientTimeout] = aiohttp.ClientTimeout(total=15)
API_RATE_LIMIT: Final[int] = 1
API_RATE_PERIOD: Final[float] = 5.0

# The picture of a channel that comes without its own.
FALLBACK_IMAGE_URL: Final[str] = (
    "https://images.sunshine-live.de/files/streams/hg/sunshine-sunshine-logo_bg.png"
)

STATION_NAME: Final[str] = "Sunshine Live"

# The station writes channel names in capitals, these words don't follow normal capitalization.
NAME_WORDS: Final[dict[str, str]] = {"BY": "by", "CH": "CH", "EDM": "EDM", "OF": "of"}
