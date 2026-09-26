"""Constants for the Teufel Raumfeld player provider."""

from __future__ import annotations

import aiohttp

from music_assistant.constants import (
    CONF_ENTRY_HTTP_PROFILE_FORCED_1,
    CONF_ENTRY_ICY_METADATA_DEFAULT_FULL,
)

# Errors expected when the host is (temporarily) unreachable or a zone/room cannot be
# resolved by hassfeld; used to narrow the try/except around host communication.
HOST_ERRORS = (aiohttp.ClientError, OSError, TimeoutError, KeyError)

# Default port of the Raumfeld host webservice (the device running the "RaumfeldHost"
# service). Config keys reuse MA's CONF_IP_ADDRESS / CONF_PORT.
DEFAULT_PORT = 47365

# How long (seconds) to wait for hassfeld to complete its initial discovery
# of rooms/zones before giving up on a single connection attempt.
INITIAL_UPDATE_TIMEOUT = 30

# How long (seconds) to wait for a renderer's device description, which is read once per
# room to get the hardware serial the player_id is derived from.
DEVICE_INFO_TIMEOUT = 10

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

# Per-player config entries. Flow mode itself needs no entry: the player reports
# requires_flow_mode, so MA leaves the flow toggle out and adds the flow sample-rate entry.
PLAYER_CONFIG_ENTRIES = [
    # Chunked, hidden. The host's stream relay reconnects and replays the stream from the
    # start whenever the response carries a finite Content-Length (measured, and
    # forced_content_length reproduces it); a chunked response carries none, so the host
    # relays the flow as one continuous connection.
    CONF_ENTRY_HTTP_PROFILE_FORCED_1,
    # ICY on by default. The stream never changes - it is one flow for the whole queue - so
    # the ICY StreamTitle, which the host parses out of the relayed stream, is the only way
    # the Raumfeld app can follow the queue. It carries the title only: the app's clock and
    # duration do not follow along.
    CONF_ENTRY_ICY_METADATA_DEFAULT_FULL,
]
