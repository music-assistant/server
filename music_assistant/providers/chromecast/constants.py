"""Constants for Chromecast Player provider."""

from __future__ import annotations

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import (
    CONF_ENTRY_HTTP_PROFILE,
    create_output_codec_config_entry,
    create_sample_rates_config_entry,
)

MASS_APP_ID = "C35B0678"
APP_MEDIA_RECEIVER = "CC1AD845"
SENDSPIN_CAST_APP_ID = "938CBF87"
SENDSPIN_CAST_NAMESPACE = "urn:x-cast:sendspin"
CONF_USE_MASS_APP = "use_mass_app"
CONF_USE_SENDSPIN_MODE = "use_sendspin_mode"
CONF_SENDSPIN_SYNC_DELAY = "sendspin_sync_delay"
CONF_SENDSPIN_CODEC = "sendspin_codec"
DEFAULT_SENDSPIN_SYNC_DELAY = -300
DEFAULT_SENDSPIN_CODEC = "flac"

CONF_ENTRY_OUTPUT_CODEC_CAST = create_output_codec_config_entry(default_value="flac")
CONF_ENTRY_OUTPUT_CODEC_CAST.options = [
    ConfigValueOption("WAV (lossless, uncompressed, supports range requests)", "wav")
    if opt.value == "wav"
    else opt
    for opt in (CONF_ENTRY_OUTPUT_CODEC_CAST.options or [])
]

CAST_PLAYER_CONFIG_ENTRIES = (
    CONF_ENTRY_OUTPUT_CODEC_CAST,
    CONF_ENTRY_HTTP_PROFILE,
    ConfigEntry(
        key=CONF_USE_MASS_APP,
        type=ConfigEntryType.BOOLEAN,
        label="Use Music Assistant Cast App",
        default_value=True,
        description="By default, Music Assistant will use a special Music Assistant "
        "Cast Receiver app to play media on cast devices. It is tweaked to provide "
        "better metadata and future expansion. \\n\\n"
        "If you want to use the official Google Cast Receiver app instead, disable this option, "
        "for example if your device has issues with the Music Assistant app.",
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
