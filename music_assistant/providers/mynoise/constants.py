"""Constants for the myNoise provider."""

from __future__ import annotations

from typing import Final

# the website where the machine pages (and cover images) live
WEBSITE_URL: Final[str] = "https://mynoise.net"
# the CDN that actually serves the (loopable) audio files for each machine
CDN_URL: Final[str] = "https://mynoise.world"
# the index page listing all (public) noise machines, grouped by category
INDEX_URL: Final[str] = f"{WEBSITE_URL}/noiseMachines.php"
# where the patron (privileged access) login form posts the email to; a
# successful login redirects to AUTH_OK_PATH and sets a session cookie, which
# unlocks the patron-only generator pages (the CDN itself is public)
AUTH_URL: Final[str] = f"{WEBSITE_URL}/authentication_script.php"
AUTH_OK_PATH: Final[str] = "/authentication_OK.php"

USER_AGENT: Final[str] = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

# config entry keys
CONF_REFRESH_CATALOG: Final[str] = "refresh_catalog"
CONF_PATRON_EMAIL: Final[str] = "patron_email"
CONF_MASTER_GAIN_DB: Final[str] = "master_gain_db"
CONF_USE_VARIANT_B: Final[str] = "use_variant_b"

# cache categories
CACHE_CATEGORY_CATALOG: Final[int] = 1
CACHE_CATEGORY_MACHINE: Final[int] = 2

# catalog refresh interval (30 days)
CATALOG_REFRESH_INTERVAL: Final[int] = 3600 * 24 * 30
# machine definition (layer files) cache interval (1 year, these are static)
MACHINE_REFRESH_INTERVAL: Final[int] = 3600 * 24 * 365

# audio file extension to request from the CDN (the site uses .ogg when the
# browser supports it; mp3 is the universally supported fallback)
AUDIO_EXTENSION: Final[str] = ".mp3"

# subdirectory (under the provider cache dir) holding downloaded layer audio
# files, so the endless ffmpeg mix loops local files instead of re-fetching
# from the CDN on every loop pass
AUDIO_CACHE_SUBDIR: Final[str] = "audio"

# output format of the custom (mixed) stream
OUTPUT_SAMPLE_RATE: Final[int] = 44100
OUTPUT_CHANNELS: Final[int] = 2
OUTPUT_BIT_DEPTH: Final[int] = 16
# chunk size (bytes) read from the ffmpeg process
STREAM_CHUNK_SIZE: Final[int] = 65536
