"""Constants for the Teufel Raumfeld player provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

DOMAIN = "raumfeld"

# Errors expected when the host is (temporarily) unreachable or a zone/room cannot be
# resolved by hassfeld; used to narrow the try/except around host communication.
HOST_ERRORS = (aiohttp.ClientError, OSError, TimeoutError, KeyError)

# Default port of the Raumfeld host webservice (the device running the "RaumfeldHost"
# service). Config keys reuse MA's CONF_IP_ADDRESS / CONF_PORT.
DEFAULT_PORT = 47365

# How long (seconds) to wait for hassfeld to complete its initial discovery
# of rooms/zones before giving up on a single connection attempt.
INITIAL_UPDATE_TIMEOUT = 30

# How often (seconds) the connection supervisor retries a connect while the host is
# unreachable, and health-checks the host while connected.
RECONNECT_INTERVAL = 30

# Player id prefix so ids are namespaced and stable-ish per room.
PLAYER_ID_PREFIX = "raumfeld"

# Media-server container listing each device's analog Line-In input, and the MA
# source id used to expose a room's Line-In via PlayerFeature.SELECT_SOURCE.
LINE_IN_OBJECT_ID = "0/Line In"
SOURCE_LINE_IN = "line_in"

# Raumfeld renderers are hi-res capable up to 24-bit/192kHz. Declaring the supported
# (sample_rate, bit_depth) pairs lets MA output each source at its native rate (so 24-bit
# lossless passes through) without the user having to enable rates by hand.
SUPPORTED_SAMPLE_RATES = [
    (sample_rate, bit_depth)
    for sample_rate in (44100, 48000, 88200, 96000, 176400, 192000)
    for bit_depth in (16, 24)
]

# Per-player config entries. Empty: MA injects the standard (advanced, default-off)
# flow-mode toggle for HTTP-based players itself, so leaving this empty exposes flow
# mode as an opt-in choice - the same toggle Chromecast and other players get. Flow
# mode is off by default because Raumfeld treats pause as stop and resumes by
# re-streaming (see player.py): per-track streaming keeps that resume snappy, while
# flow mode trades some of that for gapless playback.
PLAYER_CONFIG_ENTRIES: list[ConfigEntry] = []
