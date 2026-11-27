"""Constants for the Web Broadcast player provider."""

from __future__ import annotations

from typing import Final

# Provider config key holding the list of station display names.
CONF_STATIONS: Final[str] = "stations"

# Prefix used when deriving a protocol id from a station slug.
PLAYER_ID_PREFIX: Final[str] = "webradio_"

# Public URL path prefix under which each station is served.
URL_PATH_PREFIX: Final[str] = "/webradio/"

# Per-player config key for the read-only URL display entry.
CONF_STREAM_URL_LABEL: Final[str] = "stream_url"
