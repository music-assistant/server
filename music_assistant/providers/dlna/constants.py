"""Constants for DLNA provider."""

from music_assistant_models.config_entries import ConfigEntry

from music_assistant.constants import CONF_ENTRY_FLOW_MODE, create_sample_rates_config_entry

PLAYER_CONFIG_ENTRIES = [
    # enable flow mode by default because
    # most dlna players do not support enqueueing
    ConfigEntry.from_dict({**CONF_ENTRY_FLOW_MODE.to_dict(), "default_value": True}),
    create_sample_rates_config_entry(max_sample_rate=192000, max_bit_depth=24),
]


CONF_NETWORK_SCAN = "network_scan"
