"""Constants for the Sunshine Live provider."""

from typing import Final

import aiohttp

# The public list of channels behind the player on the website of the station.
CHANNELS_URL: Final[str] = "https://www.sunshine-live.de/streams/api"
CHANNELS_CACHE_SECONDS: Final[int] = 3600
API_TIMEOUT: Final[aiohttp.ClientTimeout] = aiohttp.ClientTimeout(total=15)

# The picture of a channel that comes without its own.
FALLBACK_IMAGE_URL: Final[str] = (
    "https://images.sunshine-live.de/files/streams/hg/sunshine-sunshine-logo_bg.png"
)

STATION_NAME: Final[str] = "Sunshine Live"

# The station writes channel names in capitals, these words don't follow normal capitalization.
NAME_WORDS: Final[dict[str, str]] = {"BY": "by", "CH": "CH", "EDM": "EDM", "OF": "of"}
