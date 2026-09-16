"""Constants for the Teufel Raumfeld player provider."""

from __future__ import annotations

from music_assistant_models.config_entries import ConfigEntry

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_FLOW_MODE_SAMPLE_RATE,
    create_sample_rates_config_entry,
)

DOMAIN = "raumfeld"

# Config entry keys
CONF_HOST = "host"
CONF_PORT = "port"

# Default port of the Raumfeld host webservice (raumfeld host / raumserver).
# The host is the device on the network running the "RaumfeldHost" service
# (a physical Raumfeld device or the Raumfeld app acting as host).
DEFAULT_PORT = 47365

# How long (seconds) to wait for hassfeld to complete its initial discovery
# of rooms/zones before giving up on a single connection attempt.
INITIAL_UPDATE_TIMEOUT = 30

# How often (seconds) the connection supervisor retries a connect while the host is
# unreachable, and health-checks the host while connected.
RECONNECT_INTERVAL = 30

# Player id prefix so ids are namespaced and stable-ish per room.
PLAYER_ID_PREFIX = "raumfeld"

# Per-player config entries. Raumfeld renderers support hi-res PCM up to 24-bit/192kHz;
# expose the same sample-rate/bit-depth options the DLNA provider does so users can opt
# into hi-res output (the entry defaults to a safe 48kHz/16-bit selection).
PLAYER_CONFIG_ENTRIES = [
    create_sample_rates_config_entry(max_sample_rate=192000, max_bit_depth=24),
    # Hide the flow-mode toggle and keep it forced off. Raumfeld renderers drop the
    # HTTP connection on pause and cannot resume a single continuous (flow) stream, so
    # enabling flow mode would break pause/resume. Providing these keys makes MA replace
    # its own (visible) default entries with these hidden ones.
    ConfigEntry.from_dict(
        {**CONF_ENTRY_FLOW_MODE.to_dict(), "default_value": False, "hidden": True}
    ),
    ConfigEntry.from_dict({**CONF_ENTRY_FLOW_MODE_SAMPLE_RATE.to_dict(), "hidden": True}),
]
