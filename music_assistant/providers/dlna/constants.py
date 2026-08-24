"""Constants for DLNA provider."""

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_ENTRY_FLOW_MODE, create_sample_rates_config_entry

PLAYER_CONFIG_ENTRIES = [
    # enable flow mode by default because
    # most dlna players do not support enqueueing
    ConfigEntry.from_dict({**CONF_ENTRY_FLOW_MODE.to_dict(), "default_value": True}),
    create_sample_rates_config_entry(max_sample_rate=192000, max_bit_depth=24),
    # Replace Pause with Stop for legacy audiophile gear that does not support pause on unseekable streams (e.g., radio or chunked HTTP).
    ConfigEntry(
        key="replace_pause_with_stop",
        type=ConfigEntryType.BOOLEAN,
        default_value=False,
        advanced=True,
    ),
]
