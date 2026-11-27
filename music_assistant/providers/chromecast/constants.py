"""Constants for Chromecast Player provider."""

from __future__ import annotations

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_OUTPUT_CODEC,
    CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3,
    CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED,
    create_sample_rates_config_entry,
)

MASS_APP_ID = "C35B0678"
APP_MEDIA_RECEIVER = "CC1AD845"
SENDSPIN_CAST_APP_ID = "DD107DDB"
SENDSPIN_CAST_NAMESPACE = "urn:x-cast:sendspin"
CONF_USE_MASS_APP = "use_mass_app"
CONF_DEFER_IDLE_VOLUME = "defer_idle_volume"
DASHBOARD_NAMESPACE = "urn:x-cast:io.music-assistant.cast"

# Seconds to wait for a Cast receiver to acknowledge an app launch.
APP_LAUNCH_TIMEOUT = 30.0

# Seconds the receiver app is kept running after playback stopped, so a
# follow-up command can reuse the Cast session instead of starting a new one.
APP_QUIT_DELAY = 10.0

# Seconds a playing app of another party may report nothing playing, as it does while it
# moves to another track, before that is taken as the end of its playback.
APP_TRACK_CHANGE_TIME = 5.0

# keepalive media the cast receiver plays while showing a dashboard
DASHBOARD_KEEPALIVE_SUFFIXES = ("/dashboard-keepalive.mp4", "/keepalive.png")

# Interval (seconds) before an unavailable player is re-evaluated as a possible
# passive multichannel endpoint that should be removed from the setup.
MULTICHANNEL_RECHECK_INTERVAL = 600

# Translation key (owned by the Sendspin provider, which renders it) of the warning
# shown on the Sendspin output of a Cast device. Its presence also makes that output
# default to disabled, so Sendspin over Cast is opt-in per device.
SENDSPIN_CAST_EXPERIMENTAL_NOTE = "sendspin_cast_experimental"

# Marker on a Cast player whose receiver reported it cannot run the Sendspin client.
# Kept on the Cast player rather than on the bridge, which is removed for good once set.
CONF_SENDSPIN_UNSUPPORTED = "sendspin_unsupported"


# Seconds to wait for a Cast device to report which app runs, before playback over
# Sendspin goes ahead on the last status it reported.
CAST_STATUS_REFRESH_TIMEOUT = 3.0

# Seconds a connected Sendspin Cast app gets to report its clock synchronized, before
# it counts as ready without that report.
CAST_APP_SYNC_TIMEOUT = 3.0

# Devices known to not work with the Sendspin Cast bridge.
# Tuple of (manufacturer, model) where "*" is a wildcard.
# These devices will not get a Sendspin bridge, allowing other protocols
# (e.g. AirPlay bridge) to handle them instead.
SENDSPIN_CAST_BLOCKLIST: set[tuple[str, str]] = {
    ("Harman Luxury Audio", "*"),
    ("*", "HK OMNI ADAPT+AMP"),
}

CAST_PLAYER_CONFIG_ENTRIES = (
    CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED,
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

# some receivers accept a volume set while idle but keep playing at the old level,
# so the user can opt in to have it sent at playback start instead
CONF_ENTRY_DEFER_IDLE_VOLUME = ConfigEntry(
    key=CONF_DEFER_IDLE_VOLUME,
    type=ConfigEntryType.BOOLEAN,
    default_value=False,
    advanced=True,
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
    ("LG Electronics", "LG SPx"): 370,
}
CAST_FALLBACK_STATIC_DELAY = 330

# Cast models that played lossless (FLAC) flow streams for hours without a dropout. Every other
# Cast device streams MP3 by default: several receivers (Samsung's Cast Lite speakers, Google
# Nest Mini and Nest Audio) stop playing a long lossless stream after a while.
CAST_LOSSLESS_MODELS: set[str] = {"Chromecast Audio", "Google Nest Hub"}


def get_cast_model_static_delay(manufacturer: str, model: str) -> int:
    """
    Look up the default static delay for a Cast device model.

    :param manufacturer: Device manufacturer (e.g., "Google Inc.").
    :param model: Device model name (e.g., "Google Nest Mini").
    """
    return CAST_MODEL_STATIC_DELAY.get((manufacturer, model), CAST_FALLBACK_STATIC_DELAY)


def get_cast_output_codec_entry(model: str) -> ConfigEntry:
    """
    Return the output codec config entry for a Cast device model, with its default codec.

    :param model: Device model name (e.g., "Google Nest Mini").
    """
    if model in CAST_LOSSLESS_MODELS:
        return CONF_ENTRY_OUTPUT_CODEC
    return CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3
