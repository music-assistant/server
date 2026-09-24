"""Constants for the Teufel Raumfeld player provider."""

from __future__ import annotations

import aiohttp
from music_assistant_models.config_entries import ConfigEntry

from music_assistant.constants import (
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_FLOW_MODE_SAMPLE_RATE,
    CONF_ENTRY_HTTP_PROFILE,
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

# Per-player config entries. The three forced ones below are how this provider plays;
# providing these keys makes MA replace its own (visible) default entries with these.
PLAYER_CONFIG_ENTRIES = [
    # Force flow mode on. The whole queue is streamed to the zone as one continuous
    # stream that the host relays to the zone (and to every grouped room), so it crosses
    # track boundaries without tearing the transport down: gapless, and gapless in groups.
    # Hidden and locked because the provider's playback strategy depends on it.
    ConfigEntry.from_dict(
        {**CONF_ENTRY_FLOW_MODE.to_dict(), "default_value": True, "value": True, "hidden": True}
    ),
    # Force the chunked HTTP profile. The host's stream-relay reconnects and replays the
    # stream from the start whenever it advertises a finite Content-Length (measured, and
    # forced_content_length reproduces it); a chunked response carries none, so the host
    # relays it as one continuous connection instead. Hidden and locked.
    ConfigEntry.from_dict(
        {
            **CONF_ENTRY_HTTP_PROFILE.to_dict(),
            "default_value": "chunked",
            "value": "chunked",
            "hidden": True,
        }
    ),
    # Force ICY metadata on. The host parses the ICY StreamTitle out of the relayed stream
    # and, on each change, updates the room's now-playing title and resets its per-track
    # clock - which is how the Raumfeld app follows the queue across the gapless boundaries
    # (the one thing it cannot get any other way: the stream itself never changes). Hidden
    # and locked.
    ConfigEntry.from_dict(
        {
            **CONF_ENTRY_ENABLE_ICY_METADATA.to_dict(),
            "default_value": "full",
            "value": "full",
            "hidden": True,
        }
    ),
    # Left visible: how the flow handles tracks of differing sample rates. 'smart' (the
    # default) briefly re-opens the stream at a rate change; a fixed rate resamples every
    # track and never re-opens, trading native rates for a stream that never breaks.
    CONF_ENTRY_FLOW_MODE_SAMPLE_RATE,
]
