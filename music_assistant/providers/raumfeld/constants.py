"""Constants for the Teufel Raumfeld player provider."""

from __future__ import annotations

import aiohttp
from music_assistant_models.config_entries import ConfigEntry

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_FLOW_MODE_SAMPLE_RATE,
)

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

# Raumfeld renderers are hi-res capable up to 24-bit/192kHz. Declaring the supported
# (sample_rate, bit_depth) pairs lets MA output each source at its native rate (so 24-bit
# lossless passes through) without the user having to enable rates by hand.
SUPPORTED_SAMPLE_RATES = [
    (sample_rate, bit_depth)
    for sample_rate in (44100, 48000, 88200, 96000, 176400, 192000)
    for bit_depth in (16, 24)
]

# Per-player config entries.
PLAYER_CONFIG_ENTRIES = [
    # Hide the flow-mode toggle and keep it forced off. Raumfeld renderers drop the
    # HTTP connection on pause and cannot resume a single continuous (flow) stream, so
    # enabling flow mode would break pause/resume. Providing these keys makes MA replace
    # its own (visible) default entries with these hidden ones.
    ConfigEntry.from_dict(
        {**CONF_ENTRY_FLOW_MODE.to_dict(), "default_value": False, "hidden": True}
    ),
    ConfigEntry.from_dict({**CONF_ENTRY_FLOW_MODE_SAMPLE_RATE.to_dict(), "hidden": True}),
]
