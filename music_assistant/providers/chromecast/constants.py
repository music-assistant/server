"""Constants for Chromecast Player provider."""

from __future__ import annotations

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_HTTP_PROFILE_DEFAULT_3,
    CONF_ENTRY_OUTPUT_CODEC,
    create_sample_rates_config_entry,
)

MASS_APP_ID = "C35B0678"
APP_MEDIA_RECEIVER = "CC1AD845"
SENDSPIN_CAST_APP_ID = "DD107DDB"
SENDSPIN_CAST_NAMESPACE = "urn:x-cast:sendspin"
CONF_USE_MASS_APP = "use_mass_app"
DASHBOARD_NAMESPACE = "urn:x-cast:io.music-assistant.cast"

# Interval (seconds) before an unavailable player is re-evaluated as a possible
# passive multichannel endpoint that should be removed from the setup.
MULTICHANNEL_RECHECK_INTERVAL = 600

# Devices known to not work with the Sendspin Cast bridge.
# Tuple of (manufacturer, model) where "*" is a wildcard.
# These devices will not get a Sendspin bridge, allowing other protocols
# (e.g. AirPlay bridge) to handle them instead.
SENDSPIN_CAST_BLOCKLIST: set[tuple[str, str]] = {
    ("Harman Luxury Audio", "*"),
    ("*", "HK OMNI ADAPT+AMP"),
}

CAST_PLAYER_CONFIG_ENTRIES = (
    CONF_ENTRY_OUTPUT_CODEC,
    CONF_ENTRY_HTTP_PROFILE_DEFAULT_3,
    # enable flow mode by default as cast devices handle a continuous
    # flow stream more reliably than enqueueing individual tracks
    ConfigEntry.from_dict({**CONF_ENTRY_FLOW_MODE.to_dict(), "default_value": True}),
    ConfigEntry(
        key=CONF_USE_MASS_APP,
        type=ConfigEntryType.BOOLEAN,
        default_value=True,
        advanced=True,
    ),
)

# originally/officially cast supports 96k sample rate (even for groups)
# but it seems a (recent?) update broke this ?!
# For now only set safe default values and let the user try out higher values
CONF_ENTRY_SAMPLE_RATES_CAST = create_sample_rates_config_entry(
    max_sample_rate=192000,
    max_bit_depth=24,
    safe_max_sample_rate=48000,
    safe_max_bit_depth=16,
)
CONF_ENTRY_SAMPLE_RATES_CAST_GROUP = create_sample_rates_config_entry(
    max_sample_rate=96000,
    max_bit_depth=24,
    safe_max_sample_rate=48000,
    safe_max_bit_depth=16,
)

# Measured defaults for known Cast models.
CAST_MODEL_STATIC_DELAY: dict[tuple[str, str], int] = {
    ("Google Inc.", "Google Home Mini"): 330,
    ("Google Inc.", "Google Nest Mini"): 427,
    ("Google Inc.", "Chromecast Audio"): 335,
    ("Google Inc.", "Google Nest Hub"): 188,
}
CAST_FALLBACK_STATIC_DELAY = 330


def get_cast_model_static_delay(manufacturer: str, model: str) -> int:
    """
    Look up the default static delay for a Cast device model.

    :param manufacturer: Device manufacturer (e.g., "Google Inc.").
    :param model: Device model name (e.g., "Google Nest Mini").
    """
    return CAST_MODEL_STATIC_DELAY.get((manufacturer, model), CAST_FALLBACK_STATIC_DELAY)
