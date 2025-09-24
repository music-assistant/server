"""Constants for the Internet Archive provider."""

from __future__ import annotations

# Internet Archive API endpoints
IA_SEARCH_URL = "https://archive.org/advancedsearch.php"
IA_METADATA_URL = "https://archive.org/metadata"
IA_DETAILS_URL = "https://archive.org/details"
IA_DOWNLOAD_URL = "https://archive.org/download"
IA_SERVE_URL = "https://archive.org/serve"

# Audio file formats supported by IA (normalized to lowercase for consistent comparison)
# IA API returns formats in inconsistent casing, so we normalize to lowercase internally
SUPPORTED_AUDIO_FORMATS = {
    "vbr mp3",
    "mp3",
    "128kbps mp3",
    "64kbps mp3",
    "flac",
    "ogg vorbis",
    "ogg",
    "aac",
    "m4a",
    "wav",
    "aiff",
}

# Preferred format order for audio quality (normalized to lowercase)
# Ordered from highest to lowest quality preference
PREFERRED_AUDIO_FORMATS = [
    "flac",
    "vbr mp3",
    "ogg vorbis",
    "mp3",
    "128kbps mp3",
    "64kbps mp3",
]

# Collections that should be treated as audiobooks (verified)
AUDIOBOOK_COLLECTIONS = {"librivoxaudio"}
