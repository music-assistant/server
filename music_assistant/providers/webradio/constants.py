"""Constants for the Web Broadcast player provider."""

from __future__ import annotations

from typing import Final

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption

from music_assistant.constants import CONF_ENTRY_OUTPUT_CODEC

# Provider config key holding the list of station display names.
CONF_STATIONS: Final[str] = "stations"

# Prefix used when deriving a protocol id from a station slug.
PLAYER_ID_PREFIX: Final[str] = "webradio_"

# Public URL path prefix under which each station is served.
URL_PATH_PREFIX: Final[str] = "/webradio/"

# Per-player config key for the read-only URL display entry.
CONF_STREAM_URL_LABEL: Final[str] = "stream_url"

# Restrict to frame-synchronising codecs. MP3 and AAC can be joined
# mid-stream and survive a producer restart. FLAC and WAV depend on a
# header (STREAMINFO / RIFF) at the start of the stream that mid-stream
# listeners cannot recover and that each restart re-emits.
CONF_ENTRY_OUTPUT_CODEC_WEBRADIO: Final[ConfigEntry] = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_OUTPUT_CODEC.to_dict(),
        "default_value": "mp3",
        "options": [
            ConfigValueOption("mp3").to_dict(),
            ConfigValueOption("aac").to_dict(),
        ],
    }
)
