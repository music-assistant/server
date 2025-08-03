"""Constants for DLNA provider."""

from music_assistant.constants import (
    CONF_ENTRY_ENABLE_ICY_METADATA,
    CONF_ENTRY_FLOW_MODE_DEFAULT_ENABLED,
    CONF_ENTRY_HTTP_PROFILE,
    CONF_ENTRY_OUTPUT_CODEC,
    create_sample_rates_config_entry,
)

PLAYER_CONFIG_ENTRIES = [
    CONF_ENTRY_OUTPUT_CODEC,
    CONF_ENTRY_HTTP_PROFILE,
    CONF_ENTRY_ENABLE_ICY_METADATA,
    # enable flow mode by default because
    # most dlna players do not support enqueueing
    CONF_ENTRY_FLOW_MODE_DEFAULT_ENABLED,
    create_sample_rates_config_entry(max_sample_rate=192000, max_bit_depth=24),
]


CONF_NETWORK_SCAN = "network_scan"
