"""Constants for the Web Radio Broadcast player provider."""

from __future__ import annotations

from typing import Final

from music_assistant_models.config_entries import ConfigEntry

from music_assistant.constants import CONF_ENTRY_ENABLE_ICY_METADATA

# Provider config key holding the list of station display names.
CONF_STATIONS: Final[str] = "stations"

# Prefix used when deriving an MA player_id from a station slug.
PLAYER_ID_PREFIX: Final[str] = "webradio_"

# Public URL path prefix under which each station is served.
URL_PATH_PREFIX: Final[str] = "/webradio/"

# Per-player config key for the read-only URL display entry.
CONF_STREAM_URL_LABEL: Final[str] = "stream_url"

# Override the platform default ("disabled") - stations exist to advertise
# titles. "basic" rather than "full" because "full" also enlarges the
# producer's chunk buffer to ~8 s, hurting skip latency; users whose clients
# render cover art can opt into "full" per-station.
CONF_ENTRY_ENABLE_ICY_METADATA_BASIC: Final[ConfigEntry] = ConfigEntry.from_dict(
    {**CONF_ENTRY_ENABLE_ICY_METADATA.to_dict(), "default_value": "basic"}
)
